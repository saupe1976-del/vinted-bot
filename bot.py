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
    "clothes bundle",
    "clothing bundle",
    "reseller bundle",
    "wardrobe bundle",
    "job lot clothes",
    "joblot clothes",
    "bundle items",
    "kg clothes",
]

MAX_PRICE = 20
SCAN_INTERVAL = 300  # seconds (change anytime via /set_interval)

BASE_URL = "https://www.vinted.co.uk/catalog"
BASE_SITE = "https://www.vinted.co.uk"
HEADERS = {"User-Agent": "Mozilla/5.0"}

paused = False
adult_only = True   # default: try to avoid kids bundles
seen_items = set()

# ============ FILTERING (clothes-focused, not too strict) ============

CLOTHING_TERMS = [
    # generic bundle terms often used for clothes bundles
    "bundle", "set", "job lot", "joblot", "lot", "wardrobe",
    "reseller", "resellers",
    # weight-based bundles
    "kg", "kilo",
    # clothing words
    "clothes", "clothing",
    "top", "tops", "tshirt", "t-shirt", "tee",
    "hoodie", "jumper", "sweater",
    "jeans", "trousers", "pants", "shorts",
    "leggings", "dress", "skirt",
    "coat", "jacket", "shirt", "shirts", "blouse",
    "tracksuit", "joggers",
    # condition signals common in titles
    "very good", "new with tags", "bnwt", "nwt",
    # sizing signals (very common in clothes bundle titles)
    "size", "uk", "xs", "s", "m", "l", "xl", "xxl",
    "uk 4", "uk 6", "uk 8", "uk 10", "uk 12", "uk 14", "uk 16", "uk 18", "uk 20",
    "uk 4-6", "uk 6-8", "uk 8-10", "uk 10-12", "uk 12-14", "uk 14-16",
]

# IMPORTANT: removed kids/girls/boys/baby/toddler from banned terms
# because it was filtering out almost all bundle listings.
BANNED_TERMS = [
    "toy", "toys", "lego",
    "game", "games", "ps4", "ps5", "xbox", "switch", "nintendo",
    "book", "books", "dvd", "blu-ray", "cd",
    "phone", "iphone", "ipad", "tablet", "laptop",
    "makeup", "skincare", "perfume",
    "mug", "home", "kitchen",
]

# When adult_only is on, we skip listings that look clearly child-age based
KIDS_AGE_PATTERNS = [
    r"\b\d{1,2}\s*-\s*\d{1,2}\s*(?:years|yrs)\b",   # "6-7 years"
    r"\b(?:age|ages)\s*\d{1,2}\b",                  # "age 6"
    r"\b(?:years|yrs)\s*\d{1,2}\b",                 # "years 6"
]
KIDS_WORDS = ["kids", "kid", "baby", "toddler", "girls", "boys"]

def looks_like_clothes(title: str) -> bool:
    t = (title or "").lower()

    if any(bad in t for bad in BANNED_TERMS):
        return False

    if adult_only:
        # If it explicitly mentions kids words OR age patterns, skip
        if any(w in t for w in KIDS_WORDS):
            return False
        if any(re.search(p, t) for p in KIDS_AGE_PATTERNS):
            return False

    # STRONG bundle indicators - at least one MUST be present
    STRONG_BUNDLE_TERMS = ["bundle", "job lot", "joblot", "reseller", "kg", "kilo"]
    has_strong_bundle = any(term in t for term in STRONG_BUNDLE_TERMS)
    
    # Quantity indicators
    has_quantity = bool(re.search(r'\b\d+\s*(items?|pieces?|kg|kilo)', t))
    
    # Words that suggest it's a single item (AUTO-REJECT)
    SINGLE_ITEM_WORDS = ["bnwt", "new with tags", "nwt", "brand new", "never worn", 
                         "worn once", "excellent condition", "perfect condition"]
    if any(word in t for word in SINGLE_ITEM_WORDS):
        # Only reject if there's NO strong bundle term
        if not has_strong_bundle and not has_quantity:
            return False
    
    # Must have EITHER strong bundle term OR quantity
    if not has_strong_bundle and not has_quantity:
        return False

    # If it has bundle term + clothing words, accept
    CLOTHING_WORDS = [
        "clothes", "clothing", "top", "tops", "tshirt", "t-shirt", "tee",
        "hoodie", "jumper", "sweater", "jeans", "trousers", "pants", "shorts",
        "leggings", "dress", "skirt", "coat", "jacket", "shirt", "shirts",
        "blouse", "tracksuit", "joggers", "wardrobe"
    ]
    
    has_clothing_word = any(word in t for word in CLOTHING_WORDS)
    
    # STRICT: Must have clothing word OR weight OR be explicitly a reseller bundle
    if has_clothing_word:
        return True
    
    # Weight-based bundles (usually clothes)
    if "kg" in t or "kilo" in t:
        return True
    
    # Reseller + quantity is usually a bundle
    if "reseller" in t and has_quantity:
        return True
    
    # Bundle + quantity + not banned = likely clothes
    if has_strong_bundle and has_quantity:
        return True

    return False

def parse_price_gbp(text: str) -> float | None:
    if not text:
        return None
    m = re.search(r"(\d+(?:\.\d{1,2})?)", text.replace(",", ""))
    return float(m.group(1)) if m else None

def build_search_url(query: str, price_to: int) -> str:
    q = (query or "").strip()
    return f"{BASE_URL}?search_text={q.replace(' ', '+')}&price_to={price_to}&order=newest_first"

def fetch_items(query: str, price_to: int, ignore_seen: bool = False, apply_filter: bool = True):
    """
    Returns (items, meta)
    meta includes: url, status, page_items, passed, error
    """
    url = build_search_url(query, price_to)

    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
    except Exception as e:
        print(f"❌ Request failed for '{query}': {e}", flush=True)
        return [], {"url": url, "status": None, "page_items": 0, "passed": 0, "error": str(e)}

    soup = BeautifulSoup(r.text, "html.parser")

    # primary + fallback selector (Vinted markup can change)
    items = soup.select("div.feed-grid__item")
    if not items:
        items = soup.select('[data-testid="feed-item"]')

    print(f"DEBUG: Found {len(items)} items on page for query '{query}'", flush=True)

    results = []
    passed = 0
    debug_count = 0

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

        if (not ignore_seen) and (link in seen_items):
            continue

        # Try multiple ways to get the title (Vinted changes these frequently)
        title = (
            item.get("title") or
            item.get("aria-label") or
            link_tag.get("title") or
            link_tag.get("aria-label")
        )
        
        # If still no title, try text content from specific elements
        if not title:
            title_element = (
                item.select_one("p[class*='Text']") or
                item.select_one("div[class*='title']") or
                item.select_one("h3") or
                item.select_one("span[class*='title']")
            )
            if title_element:
                title = title_element.get_text(strip=True)
        
        # Last resort: use link text
        if not title:
            title = link_tag.get_text(strip=True)
        
        # Final fallback
        if not title:
            title = "New Listing"

        # DEBUG: Print first few items regardless of filtering
        if debug_count < 3:
            print(f"DEBUG item {debug_count}: title='{title[:80]}'", flush=True)
            debug_count += 1

        if apply_filter and (not looks_like_clothes(title)):
            if debug_count <= 3:
                print(f"  -> FILTERED OUT by looks_like_clothes()", flush=True)
            continue

        # Try multiple price selectors (Vinted changes these frequently)
        price_tag = (
            item.select_one("span[data-testid='price']") or
            item.select_one(".web_ui__Text__text.web_ui__Text__subtitle") or
            item.select_one("h3[class*='Text']") or
            item.select_one("span[class*='price']") or
            item.select_one("div[class*='price']")
        )
        price_text = price_tag.get_text(strip=True) if price_tag else ""
        
        # Also try to find price in the item's text content as fallback
        if not price_text:
            all_text = item.get_text()
            price_match = re.search(r'£\s*(\d+(?:\.\d{2})?)', all_text)
            if price_match:
                price_text = f"£{price_match.group(1)}"
        
        price_num = parse_price_gbp(price_text)
        
        if debug_count <= 3:
            print(f"  price_text='{price_text}', price_num={price_num}, max={price_to}", flush=True)
        
        if price_num is None or price_num > price_to:
            if debug_count <= 3:
                print(f"  -> FILTERED OUT by price (None or > {price_to})", flush=True)
            continue

        image_tag = item.find("img")
        image = image_tag.get("src") if image_tag else None

        if debug_count <= 3:
            print(f"  -> PASSED ALL FILTERS!", flush=True)

        results.append({
            "title": title[:256],
            "price": price_text or f"£{price_num:.2f}",
            "link": link,
            "image": image
        })
        passed += 1

        if not ignore_seen:
            seen_items.add(link)

    meta = {"url": url, "status": r.status_code, "page_items": len(items), "passed": passed, "error": None}
    print(f"🌐 {query} -> status {r.status_code}, page_items {len(items)}, passed {passed}", flush=True)

    return results, meta

# ================= DISCORD =================

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = discord.app_commands.CommandTree(client)

async def get_post_channel():
    return await client.fetch_channel(CHANNEL_ID)

async def post_items(channel, query: str, items: list[dict], limit: int = 8):
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
        embed.set_footer(text=f"Search: {query}")
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

        for query in list(KEYWORDS):
            items, _meta = await asyncio.to_thread(fetch_items, query, MAX_PRICE, False, True)
            print(f"🔎 {query}: new items {len(items)}", flush=True)
            if items:
                await post_items(channel, query, items, limit=8)

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
        await channel.send("✅ Vinted bot live. Use /search_now (diagnostic enabled).")
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

@tree.command(name="adult_only", description="Toggle skipping kids listings (true/false).")
async def adult_only_cmd(interaction: discord.Interaction, enabled: bool):
    global adult_only
    adult_only = enabled
    await interaction.response.send_message(f"✅ adult_only set to **{adult_only}**", ephemeral=True)

@tree.command(name="status", description="Show current bot settings.")
async def status_cmd(interaction: discord.Interaction):
    await interaction.response.send_message(
        f"Paused: **{paused}**\n"
        f"adult_only: **{adult_only}**\n"
        f"Max price: **£{MAX_PRICE}**\n"
        f"Scan interval: **{SCAN_INTERVAL}s**\n"
        f"Keywords: **{len(KEYWORDS)}**\n"
        f"Seen items: **{len(seen_items)}**",
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
        return await interaction.response.send_message("That keyword isn't in the list.", ephemeral=True)
    KEYWORDS.remove(kw)
    await interaction.response.send_message(f"🗑️ Removed keyword: `{kw}`", ephemeral=True)

@tree.command(name="clear_keywords", description="Clear all keywords.")
async def clear_keywords_cmd(interaction: discord.Interaction):
    KEYWORDS.clear()
    await interaction.response.send_message("🧹 Cleared all keywords.", ephemeral=True)

@tree.command(name="reset_seen", description="Clear seen items so listings can be posted again.")
async def reset_seen_cmd(interaction: discord.Interaction):
    seen_items.clear()
    await interaction.response.send_message("✅ Cleared seen items.", ephemeral=True)

@tree.command(name="search_now", description="Run a one-off search now and post results (diagnostic).")
async def search_now_cmd(interaction: discord.Interaction, keyword: str, max_price: int = 20, bypass_filter: bool = False):
    await interaction.response.defer(ephemeral=True)

    kw = keyword.strip()
    if not kw:
        return await interaction.followup.send("Give me a keyword.", ephemeral=True)

    if max_price < 1 or max_price > 500:
        return await interaction.followup.send("max_price must be between 1 and 500.", ephemeral=True)

    items, meta = await asyncio.to_thread(fetch_items, kw, max_price, True, not bypass_filter)
    channel = await get_post_channel()

    diag = (
        f"Status: {meta['status']}\n"
        f"Page items: {meta['page_items']}\n"
        f"Passed filter: {meta['passed']}\n"
        f"Bypass filter: {bypass_filter}\n"
        f"adult_only: {adult_only}\n"
        f"Check Railway logs for detailed debug output!\n"
    )

    if not items:
        return await interaction.followup.send(
            f"No results for `{kw}` up to £{max_price}.\n\n{diag}",
            ephemeral=True
        )

    sent = await post_items(channel, kw, items, limit=8)
    await interaction.followup.send(
        f"✅ Posted {sent} result(s) for `{kw}` (≤ £{max_price}).\n\n{diag}",
        ephemeral=True
    )

# =================================================

if __name__ == "__main__":
    client.run(DISCORD_TOKEN)
