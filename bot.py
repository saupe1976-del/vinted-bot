import discord
import requests
import asyncio
import os
import re
from bs4 import BeautifulSoup
from urllib.parse import urljoin

# ================= CONFIG =================

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
if not DISCORD_TOKEN:
    raise RuntimeError("DISCORD_TOKEN missing")

CHANNEL_ID = 1466099001743900674

# Set this in Railway Variables for instant slash commands:
# GUILD_ID = your server ID (right click server -> Copy ID)
GUILD_ID_ENV = os.getenv("GUILD_ID")
GUILD_ID = int(GUILD_ID_ENV) if GUILD_ID_ENV and GUILD_ID_ENV.isdigit() else None

KEYWORDS = [
    "womens clothes bundle",
    "mens clothes bundle",
    "job lot womens clothes",
    "job lot mens clothes",
    "wardrobe bundle womens",
    "wardrobe bundle mens",
]

MAX_PRICE = 20
SCAN_INTERVAL = 300  # seconds (change anytime via /set_interval)

# Profit detection (change anytime via slash commands)
MIN_EST_PROFIT_GBP = 15   # only alert if estimated profit >= this
MIN_CONFIDENCE = 3        # 1-6 (higher = fewer alerts, more strict)

BASE_URL = "https://www.vinted.co.uk/catalog"
BASE_SITE = "https://www.vinted.co.uk"
HEADERS = {"User-Agent": "Mozilla/5.0"}

paused = False
seen_items = set()

# ============ FILTERING (women + men clothes only) ============

WOMENS_MENS_TERMS = [
    "women", "womens", "woman", "ladies", "lady",
    "men", "mens", "man", "gents", "gent",
    "unisex"
]

CLOTHING_TERMS = [
    "clothes", "clothing", "wardrobe",
    "top", "tops", "tshirt", "t-shirt", "tee",
    "hoodie", "jumper", "sweater",
    "jeans", "trousers", "pants", "shorts",
    "leggings", "dress", "skirt",
    "coat", "jacket", "shirt", "shirts", "blouse",
    "tracksuit", "joggers", "bundle", "job lot", "joblot", "lot",
    "size"
]

BANNED_TERMS = [
    "kids", "kid", "girls", "boys", "baby", "toddler",
    "toy", "toys", "lego",
    "game", "games", "ps4", "ps5", "xbox", "switch",
    "book", "books", "dvd", "blu-ray",
    "phone", "iphone", "ipad", "tablet", "laptop",
    "makeup", "skincare", "perfume",
    "mug", "home", "kitchen"
]

def looks_like_womens_or_mens_clothes(title: str) -> bool:
    t = (title or "").lower()
    if any(bad in t for bad in BANNED_TERMS):
        return False
    if not any(word in t for word in CLOTHING_TERMS):
        return False
    return any(word in t for word in WOMENS_MENS_TERMS)

# ============ PROFIT HEURISTICS ============

BRAND_BONUS = {
    "nike": 6,
    "adidas": 5,
    "zara": 3,
    "uniqlo": 3,
    "carhartt": 8,
    "north face": 9,
    "the north face": 9,
    "ralph lauren": 8,
    "tommy hilfiger": 6,
    "levi": 7,
    "levis": 7,
}

VALUE_HINTS = {
    "reseller": 4,
    "resell": 4,
    "vintage": 4,
    "designer": 6,
    "new with tags": 6,
    "bnwt": 6,
    "nwt": 6,
}

def parse_price_gbp(text: str) -> float | None:
    if not text:
        return None
    m = re.search(r"(\d+(?:\.\d{1,2})?)", text.replace(",", ""))
    return float(m.group(1)) if m else None

def estimate_item_count(title: str) -> int | None:
    t = (title or "").lower()
    patterns = [
        r"\b(\d{1,3})\s*(?:items|item|pcs|pc|pieces|piece)\b",
        r"\bx\s*(\d{1,3})\b",
        r"\b(\d{1,3})\s*x\b",
    ]
    for p in patterns:
        m = re.search(p, t)
        if m:
            n = int(m.group(1))
            if 2 <= n <= 200:
                return n
    return None

def score_title_value(title: str) -> tuple[int, list[str]]:
    t = (title or "").lower()
    score = 0
    reasons = []
    for k, v in BRAND_BONUS.items():
        if k in t:
            score += v
            reasons.append(k)
    for k, v in VALUE_HINTS.items():
        if k in t:
            score += v
            reasons.append(k)
    return score, reasons

def estimate_resale_value_gbp(title: str, bundle_price: float) -> tuple[float, int, str]:
    """
    Returns (estimated_resale_value, confidence 1-6, explanation string)
    """
    t = (title or "").lower()
    count = estimate_item_count(title)
    score, reasons = score_title_value(title)

    confidence = 1
    explanation_bits = []

    base_per_item = 3.0
    if "designer" in t:
        base_per_item = 8.0
    if "vintage" in t:
        base_per_item = max(base_per_item, 6.0)

    if score >= 6:
        base_per_item += 2.0
        confidence += 1
    if score >= 12:
        base_per_item += 3.0
        confidence += 1

    if count:
        confidence += 2
        explanation_bits.append(f"{count} items")
        est = count * base_per_item
    else:
        est = 5 * base_per_item
        explanation_bits.append("count unknown (~5)")

    if bundle_price <= 10:
        est *= 1.15
        confidence += 1
        explanation_bits.append("low price boost")

    if reasons:
        explanation_bits.append("signals: " + ", ".join(reasons[:5]))
        confidence += 1

    confidence = max(1, min(6, confidence))
    return est, confidence, " • ".join(explanation_bits)

# =========================================

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = discord.app_commands.CommandTree(client)

def build_search_url(keyword: str, price_to: int) -> str:
    return f"{BASE_URL}?search_text={keyword.replace(' ', '+')}&price_to={price_to}"

def fetch_items(keyword: str, price_to: int):
    url = build_search_url(keyword, price_to)

    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
    except Exception as e:
        print(f"❌ Request failed for '{keyword}': {e}", flush=True)
        return []

    soup = BeautifulSoup(r.text, "html.parser")
    items = soup.select("div.feed-grid__item")
    print(f"🌐 {keyword} -> status {r.status_code}, items: {len(items)}", flush=True)

    results = []
    for item in items:
        link_tag = item.find("a", href=True)
        if not link_tag:
            continue

        href = (link_tag.get("href") or "").strip()
        link = urljoin(BASE_SITE, href)

        if not link.startswith("http"):
            continue
        if "/items/" not in link:
            continue
        if link in seen_items:
            continue

        title = item.get("title") or link_tag.get_text(strip=True) or "New Listing"
        if not looks_like_womens_or_mens_clothes(title):
            continue

        price_tag = item.select_one("span[data-testid='price']")
        price_text = price_tag.get_text(strip=True) if price_tag else ""
        bundle_price = parse_price_gbp(price_text)
        if bundle_price is None or bundle_price > price_to:
            continue

        # Profit detection
        est_value, confidence, explain = estimate_resale_value_gbp(title, bundle_price)
        est_profit = est_value - bundle_price

        if confidence < MIN_CONFIDENCE:
            continue
        if est_profit < MIN_EST_PROFIT_GBP:
            continue

        image_tag = item.find("img")
        image = image_tag.get("src") if image_tag else None

        results.append({
            "title": title[:256],
            "price_text": price_text or f"£{bundle_price:.2f}",
            "price_num": bundle_price,
            "link": link,
            "image": image,
            "est_value": est_value,
            "est_profit": est_profit,
            "confidence": confidence,
            "explain": explain
        })

        seen_items.add(link)

    return results

async def get_post_channel():
    return await client.fetch_channel(CHANNEL_ID)

async def post_items(channel, keyword: str, items: list[dict], limit: int = 8):
    sent = 0
    for item in items[:limit]:
        embed = discord.Embed(
            title=item["title"],
            url=item["link"],
            description=(
                f"💷 {item['price_text']}\n"
                f"📈 Est value: £{item['est_value']:.0f}\n"
                f"💰 Est profit: £{item['est_profit']:.0f}\n"
                f"🎯 Confidence: {item['confidence']}/6"
            ),
            color=0x2ecc71
        )
        if item.get("image"):
            embed.set_thumbnail(url=item["image"])
        embed.set_footer(text=f"{keyword} | {item['explain'][:180]}")
        await channel.send(embed=embed)
        sent += 1
    return sent

async def scan_loop():
    await client.wait_until_ready()
    channel = await get_post_channel()
    print(f"✅ Posting to channel: {channel} ({CHANNEL_ID})", flush=True)

    global paused
    while not client.is_closed():
        if paused:
            await asyncio.sleep(5)
            continue

        for keyword in list(KEYWORDS):
            items = await asyncio.to_thread(fetch_items, keyword, MAX_PRICE)
            print(f"🔎 {keyword}: profitable items {len(items)}", flush=True)
            if items:
                await post_items(channel, keyword, items, limit=8)

        await asyncio.sleep(SCAN_INTERVAL)

@client.event
async def on_ready():
    print(f"Logged in as {client.user}", flush=True)

    try:
        if GUILD_ID:
            guild_obj = discord.Object(id=GUILD_ID)
            tree.copy_global_to(guild=guild_obj)
            await tree.sync(guild=guild_obj)
            print(f"✅ Slash commands synced to guild {GUILD_ID}", flush=True)
        else:
            await tree.sync()
            print("✅ Slash commands synced globally (may take time to appear)", flush=True)

        channel = await get_post_channel()
        await channel.send(
            f"✅ Vinted bot live. Alerts: profit ≥ £{MIN_EST_PROFIT_GBP}, confidence ≥ {MIN_CONFIDENCE}/6. "
            f"Use /set_profit_min /set_confidence_min."
        )
    except Exception as e:
        print(f"❌ Startup error: {e}", flush=True)

    asyncio.create_task(scan_loop())

# ================= SLASH COMMANDS =================

@tree.command(name="pause", description="Pause the auto-scanner.")
async def pause_cmd(interaction: discord.Interaction):
    global paused
    paused = True
    await interaction.response.send_message("⏸️ Paused scanning.", ephemeral=True)

@tree.command(name="resume", description="Resume the auto-scanner.")
async def resume_cmd(interaction: discord.Interaction):
    global paused
    paused = False
    await interaction.response.send_message("▶️ Resumed scanning.", ephemeral=True)

@tree.command(name="status", description="Show current bot settings.")
async def status_cmd(interaction: discord.Interaction):
    await interaction.response.send_message(
        f"Paused: **{paused}**\n"
        f"Max price: **£{MAX_PRICE}**\n"
        f"Scan interval: **{SCAN_INTERVAL}s**\n"
        f"Keywords: **{len(KEYWORDS)}**\n"
        f"Min est profit: **£{MIN_EST_PROFIT_GBP}**\n"
        f"Min confidence: **{MIN_CONFIDENCE}/6**",
        ephemeral=True
    )

@tree.command(name="set_interval", description="Set scan interval in seconds (15-3600).")
async def set_interval_cmd(interaction: discord.Interaction, seconds: int):
    global SCAN_INTERVAL
    if seconds < 15 or seconds > 3600:
        return await interaction.response.send_message("Pick between 15 and 3600 seconds.", ephemeral=True)
    SCAN_INTERVAL = seconds
    await interaction.response.send_message(f"✅ Scan interval set to {SCAN_INTERVAL}s.", ephemeral=True)

@tree.command(name="set_price", description="Set max price in £ (1-500).")
async def set_price_cmd(interaction: discord.Interaction, pounds: int):
    global MAX_PRICE
    if pounds < 1 or pounds > 500:
        return await interaction.response.send_message("Pick a price between £1 and £500.", ephemeral=True)
    MAX_PRICE = pounds
    await interaction.response.send_message(f"✅ Max price set to £{MAX_PRICE}.", ephemeral=True)

@tree.command(name="set_profit_min", description="Set minimum estimated profit in £ (0-200).")
async def set_profit_min_cmd(interaction: discord.Interaction, pounds: int):
    global MIN_EST_PROFIT_GBP
    if pounds < 0 or pounds > 200:
        return await interaction.response.send_message("Pick between £0 and £200.", ephemeral=True)
    MIN_EST_PROFIT_GBP = pounds
    await interaction.response.send_message(f"✅ Min estimated profit set to £{MIN_EST_PROFIT_GBP}.", ephemeral=True)

@tree.command(name="set_confidence_min", description="Set minimum confidence (1-6).")
async def set_confidence_min_cmd(interaction: discord.Interaction, level: int):
    global MIN_CONFIDENCE
    if level < 1 or level > 6:
        return await interaction.response.send_message("Pick a level between 1 and 6.", ephemeral=True)
    MIN_CONFIDENCE = level
    await interaction.response.send_message(f"✅ Min confidence set to {MIN_CONFIDENCE}/6.", ephemeral=True)

@tree.command(name="keywords", description="List current keywords.")
async def keywords_cmd(interaction: discord.Interaction):
    if not KEYWORDS:
        return await interaction.response.send_message("No keywords set.", ephemeral=True)
    text = "\n".join(f"- {k}" for k in KEYWORDS[:40])
    if len(KEYWORDS) > 40:
        text += f"\n… and {len(KEYWORDS)-40} more"
    await interaction.response.send_message(text, ephemeral=True)

@tree.command(name="add_keyword", description="Add a keyword to the search list.")
async def add_keyword_cmd(interaction: discord.Interaction, keyword: str):
    kw = keyword.strip()
    if not kw:
        return await interaction.response.send_message("Give me a keyword.", ephemeral=True)
    if kw in KEYWORDS:
        return await interaction.response.send_message("That keyword is already in the list.", ephemeral=True)
    KEYWORDS.append(kw)
    await interaction.response.send_message(f"✅ Added keyword: `{kw}`", ephemeral=True)

@tree.command(name="remove_keyword", description="Remove a keyword from the search list.")
async def remove_keyword_cmd(interaction: discord.Interaction, keyword: str):
    kw = keyword.strip()
    if kw not in KEYWORDS:
        return await interaction.response.send_message("That keyword isn’t in the list.", ephemeral=True)
    KEYWORDS.remove(kw)
    await interaction.response.send_message(f"🗑️ Removed keyword: `{kw}`", ephemeral=True)

@tree.command(name="clear_keywords", description="Clear all keywords.")
async def clear_keywords_cmd(interaction: discord.Interaction):
    KEYWORDS.clear()
    await interaction.response.send_message("🧹 Cleared all keywords.", ephemeral=True)

@tree.command(name="search_now", description="Run a one-off profitable search now and post results.")
async def search_now_cmd(interaction: discord.Interaction, keyword: str, max_price: int = 20):
    await interaction.response.defer(ephemeral=True)

    kw = keyword.strip()
    if not kw:
        return await interaction.followup.send("Give me a keyword.", ephemeral=True)

    if max_price < 1 or max_price > 500:
        return await interaction.followup.send("max_price must be between 1 and 500.", ephemeral=True)

    items = await asyncio.to_thread(fetch_items, kw, max_price)
    channel = await get_post_channel()

    if not items:
        return await interaction.followup.send(
            f"No profitable results for `{kw}` up to £{max_price} "
            f"(profit ≥ £{MIN_EST_PROFIT_GBP}, confidence ≥ {MIN_CONFIDENCE}/6).",
            ephemeral=True
        )

    sent = await post_items(channel, kw, items, limit=8)
    await interaction.followup.send(f"✅ Posted {sent} profitable result(s) for `{kw}`.", ephemeral=True)

# =================================================

if __name__ == "__main__":
    client.run(DISCORD_TOKEN)
