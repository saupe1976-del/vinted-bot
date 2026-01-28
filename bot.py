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
    raise RuntimeError("DISCORD_TOKEN is missing in Railway environment variables")

CHANNEL_ID = 1466099001743900674

KEYWORDS = [
    "womens clothes bundle",
    "mens clothes bundle",
    "womens bundle",
    "mens bundle",
    "job lot womens clothes",
    "job lot mens clothes",
    "wardrobe bundle",
    "reseller bundle clothes",
]

MAX_PRICE = 20
SCAN_INTERVAL = 300  # seconds

BASE_URL = "https://www.vinted.co.uk/catalog"
BASE_SITE = "https://www.vinted.co.uk"
HEADERS = {"User-Agent": "Mozilla/5.0"}

seen_items = set()
paused = False  # pause/resume flag

# ============ FILTERING ==================

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

# =========================================

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = discord.app_commands.CommandTree(client)

async def get_post_channel():
    return await client.fetch_channel(CHANNEL_ID)

async def post_items(channel: discord.abc.Messageable, keyword: str, items: list[dict], limit: int = 8):
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
    try:
        await tree.sync()
        channel = await get_post_channel()
        await channel.send("✅ Vinted bot is live. Use /pause /resume /search_now")
    except Exception as e:
        print(f"❌ Startup error: {e}", flush=True)

    client.loop.create_task(scan_loop())

# ============ SLASH COMMANDS ============

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

@tree.command(name="add_keyword", description="Add a keyword to search for.")
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

@tree.command(name="search_now", description="Run a one-off search immediately.")
async def search_now_cmd(interaction: discord.Interaction, keyword: str, max_price: int = 20):
    await interaction.response.defer(ephemeral=True)

    kw = keyword.strip()
    if not kw:
        return await interaction.followup.send("Give me a keyword.", ephemeral=True)

    # one-off fetch (doesn't change global MAX_PRICE)
    items = await asyncio.to_thread(fetch_items, kw, max_price)

    channel = await get_post_channel()
    if not items:
        return await interaction.followup.send(f"Nothing found for `{kw}` up to £{max_price}.", ephemeral=True)

    sent = await post_items(channel, kw, items, limit=8)
    await interaction.followup.send(f"✅ Posted {sent} result(s) for `{kw}` (≤ £{max_price}).", ephemeral=True)

# =========================================

if __name__ == "__main__":
    client.run(DISCORD_TOKEN)
