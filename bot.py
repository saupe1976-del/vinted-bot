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

def parse_price_gbp(text: str) -> float | None:
    if not text:
        return None
    m = re.search(r"(\d+(?:\.\d{1,2})?)", text.replace(",", ""))
    return float(m.group(1)) if m else None

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

        # keep only real item links
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
        price_num = parse_price_gbp(price_text)
        if price_num is None or price_num > price_to:
            continue

        image_tag = item.find("img")
        image = image_tag.get("src") if image_tag else None

        results.append({
            "title": title[:256],
            "price": price_text or f"£{price_num:.2f}",
            "link": link,
            "image": image
        })

        seen_items.add(link)

    return results

# ================= DISCORD =================

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = discord.app_commands.CommandTree(client)

async def get_post_channel():
    return await client.fetch_channel(CHANNEL_ID)

async def post_items(channel, keyword: str, items: list[dict], limit: int = 8):
    sent = 0
    for item in items[:limit]:
        embed = discord.Embed(
            title=item["title"],
            url=item["link"],
            description=f"💷 {item['price']}",
            color=0x2ecc71
        )
        if item.get("image"):
            embed.set_thumbnail(url=item["image"])
        embed.set_footer(text=f"Search: {keyword}")
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
            print(f"🔎 {keyword}: new items {len(items)}", flush=True)
            if items:
                await post_items(channel, keyword, items, limit=8)

        await asyncio.sleep(SCAN_INTERVAL)

@client.event
async def on_ready():
    print(f"Logged in as {client.user}", flush=True)

    # Sync commands to your server so they appear immediately
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
        await channel.send("✅ Vinted bot is live. Use /pause /resume /search_now")
    except Exception as e:
        print(f"❌ Startup error: {e}", flush=True)

    # ✅ FIX: must call the coroutine with ()
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
        f"Keywords: **{len(KEYWORDS)}**",
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

@tree.command(name="search_now", description="Run a one-off search now and post results.")
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
        return await interaction.followup.send(f"Nothing found for `{kw}` up to £{max_price}.", ephemeral=True)

    sent = await post_items(channel, kw, items, limit=8)
    await interaction.followup.send(f"✅ Posted {sent} result(s) for `{kw}` (≤ £{max_price}).", ephemeral=True)

# =================================================

if __name__ == "__main__":
    client.run(DISCORD_TOKEN)
