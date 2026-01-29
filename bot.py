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
]

MAX_PRICE = 20
SCAN_INTERVAL = 600  # seconds (10 minutes - less aggressive to avoid blocks)

# Profitability settings
MIN_ITEMS_FOR_PROFIT = 5  # Minimum items in bundle to be considered profitable
MAX_PRICE_PER_ITEM = 4.0  # Maximum price per item (£20 / 5 items = £4 per item)

# New member settings (free postage)
PREFER_NEW_MEMBERS = True  # Prioritize listings from new members (free postage)
NEW_MEMBER_INDICATORS = ["new member", "just joined", "first listing"]

BASE_URL = "https://www.vinted.co.uk/catalog"
BASE_SITE = "https://www.vinted.co.uk"

# Rotate through different user agents to avoid detection
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.1 Safari/605.1.15",
]

def get_headers():
    """Get realistic browser headers with rotating user agent"""
    import random
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "en-GB,en;q=0.5",
        "Accept-Encoding": "gzip, deflate, br",
        "DNT": "1",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Cache-Control": "max-age=0",
    }

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
    "nappy", "nappies", "diaper", "diapers", "bib", "bibs",
    "dummy", "dummies", "pacifier", "bottle", "bottles",
    "pram", "pushchair", "stroller", "buggy",
    "ring", "rings", "necklace", "bracelet", "earring", "earrings",
    "jewellery", "jewelry", "brooch", "pendant", "charm", "charms",
    "watch", "watches",
]

# When adult_only is on, we skip listings that look clearly child-age based
KIDS_AGE_PATTERNS = [
    r"\b\d{1,2}\s*-\s*\d{1,2}\s*(?:years|yrs|year|yr)\b",   # "6-7 years", "6-7 year"
    r"\b(?:age|ages)\s*\d{1,2}\b",                           # "age 6"
    r"\b(?:years|yrs|year|yr)\s*\d{1,2}\b",                  # "years 6"
    r"\b\d{1,2}\s*(?:years|yrs|year|yr)\s*old\b",            # "6 years old"
    r"\b\d{1,2}\s*month",                                     # "12 months", "18 month"
]
KIDS_WORDS = ["kids", "kid", "baby", "toddler", "girls", "boys", "children", "child", 
              "girl", "boy", "junior", "infant", "newborn", "teenage", "teen "]

def looks_like_clothes(title: str) -> bool:
    t = (title or "").lower()

    if any(bad in t for bad in BANNED_TERMS):
        return False

    if adult_only:
        # Check for kids words - be more thorough
        for word in KIDS_WORDS:
            if word in t:
                return False
        
        # Check for age patterns
        for pattern in KIDS_AGE_PATTERNS:
            if re.search(pattern, t):
                return False
        
        # Additional check: single digit ages (age 5, 5 years, etc.)
        # But exclude adult sizes like "size 5" or "uk 5"
        if re.search(r'(?<!size\s)(?<!uk\s)\b[0-9]\s*(?:years|yrs|year|yr)\b', t):
            return False

    # STRONG bundle indicators - at least one MUST be present
    STRONG_BUNDLE_TERMS = ["bundle", "job lot", "joblot", "reseller"]
    has_strong_bundle = any(term in t for term in STRONG_BUNDLE_TERMS)
    
    # Quantity indicators - check for actual numbers before kg/kilo
    has_weight = bool(re.search(r'\b\d+\s*(?:kg|kilo)', t))
    has_quantity = bool(re.search(r'\b\d+\s*(?:items?|pieces?)', t))
    
    # "KG" as a brand (like Kurt Geiger, Mini Miss KG) should NOT count
    # Only accept kg if it's preceded by a number
    if not has_weight:
        # Remove kg/kilo from consideration if no number before it
        pass
    
    # Words that suggest it's a single item (AUTO-REJECT)
    SINGLE_ITEM_WORDS = ["bnwt", "new with tags", "nwt", "brand new", "never worn", 
                         "worn once", "excellent condition", "perfect condition"]
    if any(word in t for word in SINGLE_ITEM_WORDS):
        # Only reject if there's NO strong bundle term and NO quantity/weight
        if not has_strong_bundle and not has_quantity and not has_weight:
            return False
    
    # Must have EITHER strong bundle term OR quantity OR weight
    if not has_strong_bundle and not has_quantity and not has_weight:
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
    
    # Weight-based bundles (with actual numbers like "5kg")
    if has_weight:
        return True
    
    # Reseller + quantity is usually a bundle
    if "reseller" in t and (has_quantity or has_weight):
        return True
    
    # Bundle + quantity + not banned = likely clothes
    if has_strong_bundle and (has_quantity or has_weight):
        return True

    return False

def parse_price_gbp(text: str) -> float | None:
    if not text:
        return None
    m = re.search(r"(\d+(?:\.\d{1,2})?)", text.replace(",", ""))
    return float(m.group(1)) if m else None

def calculate_profitability_score(title: str, price: float) -> dict:
    """
    Calculate a profitability score based on:
    - Number of items in the bundle
    - Price per item
    - Keywords that suggest good resale value
    
    Returns: {
        'score': int (0-100),
        'items_count': int or None,
        'price_per_item': float or None,
        'profit_indicators': list of strings
    }
    """
    t = title.lower()
    indicators = []
    
    # Try to extract number of items
    items_match = re.search(r'(\d+)\s*(?:items?|pieces?|pc)', t)
    items_count = int(items_match.group(1)) if items_match else None
    
    # Check for weight-based bundles (estimate items)
    weight_match = re.search(r'(\d+)\s*(?:kg|kilo)', t)
    if weight_match and not items_count:
        kg = int(weight_match.group(1))
        items_count = kg * 3  # Rough estimate: 3 items per kg
        indicators.append(f"~{items_count} items (est. from {kg}kg)")
    
    # Calculate price per item
    price_per_item = None
    if items_count and items_count > 0:
        price_per_item = price / items_count
        indicators.append(f"£{price_per_item:.2f} per item")
    
    # Check for high-value keywords
    HIGH_VALUE_TERMS = [
        "branded", "designer", "next", "zara", "h&m", "primark",
        "new with tags", "bnwt", "nwt", "unworn", "new",
        "reseller", "resale"
    ]
    
    for term in HIGH_VALUE_TERMS:
        if term in t:
            indicators.append(f"Has '{term}'")
    
    # Calculate score (0-100)
    score = 0
    
    # Base score for having items count
    if items_count:
        if items_count >= MIN_ITEMS_FOR_PROFIT:
            score += 40
        elif items_count >= 3:
            score += 20
    
    # Score based on price per item
    if price_per_item:
        if price_per_item <= 2.0:
            score += 40  # Excellent deal
        elif price_per_item <= 3.0:
            score += 30  # Good deal
        elif price_per_item <= MAX_PRICE_PER_ITEM:
            score += 20  # Decent deal
        else:
            score -= 10  # Might not be profitable
    
    # Bonus for high-value indicators
    score += min(len(indicators) * 5, 20)
    
    # Cap score at 100
    score = min(max(score, 0), 100)
    
    return {
        'score': score,
        'items_count': items_count,
        'price_per_item': price_per_item,
        'profit_indicators': indicators
    }

def build_search_url(query: str, price_to: int) -> str:
    q = (query or "").strip()
    return f"{BASE_URL}?search_text={q.replace(' ', '+')}&price_to={price_to}&order=newest_first"

def fetch_items(query: str, price_to: int, ignore_seen: bool = False, apply_filter: bool = True):
    """
    Returns (items, meta)
    meta includes: url, status, page_items, passed, error
    """
    import random
    import time
    
    # Add random delay to appear more human (1-3 seconds)
    time.sleep(random.uniform(1, 3))
    
    url = build_search_url(query, price_to)

    try:
        r = requests.get(url, headers=get_headers(), timeout=15)
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

        # Check for new member badge (free postage)
        is_new_member = False
        member_badge = item.select_one('[class*="badge"]') or item.select_one('[class*="member"]')
        if member_badge:
            badge_text = member_badge.get_text(strip=True).lower()
            is_new_member = any(indicator in badge_text for indicator in NEW_MEMBER_INDICATORS)
        
        # Calculate profitability
        profit_info = calculate_profitability_score(title, price_num)

        if debug_count <= 3:
            print(f"  -> PASSED ALL FILTERS! Profit score: {profit_info['score']}/100", flush=True)
            if is_new_member:
                print(f"  -> 🆕 NEW MEMBER (free postage!)", flush=True)

        results.append({
            "title": title[:256],
            "price": price_text or f"£{price_num:.2f}",
            "link": link,
            "image": image,
            "profit_score": profit_info['score'],
            "items_count": profit_info['items_count'],
            "price_per_item": profit_info['price_per_item'],
            "profit_indicators": profit_info['profit_indicators'],
            "is_new_member": is_new_member
        })
        passed += 1

        if not ignore_seen:
            seen_items.add(link)

    meta = {"url": url, "status": r.status_code, "page_items": len(items), "passed": passed, "error": None}
    print(f"🌐 {query} -> status {r.status_code}, page_items {len(items)}, passed {passed}", flush=True)

    # Sort by profitability score (highest first), then prioritize new members
    results.sort(key=lambda x: (x['is_new_member'], x['profit_score']), reverse=True)

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
        # Build description with profit info
        desc_parts = [f"💷 {item['price']}"]
        
        # Add profit score
        score = item.get('profit_score', 0)
        if score >= 70:
            desc_parts.append(f"🔥 **GREAT DEAL** (Score: {score}/100)")
        elif score >= 50:
            desc_parts.append(f"✅ Good value (Score: {score}/100)")
        elif score > 0:
            desc_parts.append(f"📊 Score: {score}/100")
        
        # Add item count and price per item
        if item.get('items_count'):
            desc_parts.append(f"📦 {item['items_count']} items")
        if item.get('price_per_item'):
            desc_parts.append(f"💰 £{item['price_per_item']:.2f} per item")
        
        # Add profit indicators
        if item.get('profit_indicators'):
            desc_parts.append("\n" + " • ".join(item['profit_indicators'][:3]))
        
        # New member badge
        if item.get('is_new_member'):
            desc_parts.append("\n🆕 **NEW MEMBER - FREE POSTAGE!**")
        
        description = "\n".join(desc_parts)
        
        # Color based on profit score
        if score >= 70:
            color = 0xFFD700  # Gold
        elif score >= 50:
            color = 0x2ecc71  # Green
        else:
            color = 0x3498db  # Blue
        
        embed = discord.Embed(
            title=item["title"],
            url=item["link"],
            description=description,
            color=color
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
