import discord
import requests
import asyncio
import os
from bs4 import BeautifulSoup

# ================= CONFIG =================

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
if not DISCORD_TOKEN:
    raise RuntimeError("DISCORD_TOKEN is missing in Railway environment variables")

CHANNEL_ID = 1466099001743900674  # your channel id

KEYWORDS = [
    "bundle",
    "clothes bundle",
    "job lot",
    "joblot",
    "lot",
    "clothing lot",
    "wardrobe bundle",
    "mystery bundle",
    "reseller bundle",
]

MAX_PRICE = 50
SCAN_INTERVAL = 30  # seconds (testing)

BASE_URL = "https://www.vinted.co.uk/catalog"

HEADERS = {
    "User-Agent": "Mozilla/5.0"
}

seen_items = set()

# =========================================

intents = discord.Intents.default()
client = discord.Client(intents=intents)


def build_search_url(keyword: str) -> str:
    return f"{BASE_URL}?search_text={keyword.replace(' ', '+')}&price_to={MAX_PRICE}"


def fetch_items(keyword: str):
    url = build_search_url(keyword)
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
    except Exception as e:
        print(f"❌ Request failed for '{keyword}': {e}", flush=True)
        return []

    soup = BeautifulSoup(r.text, "html.parser")
    items = soup.select("div.feed-grid__item")

    print(f"🌐 {keyword} -> status {r.status_code}, items in page: {len(items)}", flush=True)

    results = []

    for item in items:
        link_tag = item.find("a", href=True)
        if not link_tag:
            continue

        link = "https://www.vinted.co.uk" + link_tag["href"]

        if link in seen_items:
            continue

        title = item.get("title") or "New Listing"
        price_tag = item.select_one("span[data-testid='price']")
        image_tag = item.find("img")

        price = price_tag.text.strip() if price_tag else "£?"
        image = image_tag["src"] if (image_tag and image_tag.get("src")) else None

        results.append({
            "title": title,
            "price": price,
            "link": link,
            "image": image
        })

        seen_items.add(link)

    return results


async def scan_loop():
    await client.wait_until_ready()

    try:
        channel = await client.fetch_channel(CHANNEL_ID)
        print(f"✅ Posting to channel: {channel} ({CHANNEL_ID})", flush=True)
    except Exception as e:
        print(f"❌ Channel fetch failed: {e}", flush=True)
        return

    while not client.is_closed():
        for keyword in KEYWORDS:
            items = await asyncio.to_thread(fetch_items, keyword)
            print(f"🔎 {keyword}: NEW items found {len(items)}", flush=True)

            for item in items:
                embed = discord.Embed(
                    title=item["title"],
                    url=item["link"],
                    description=f"💷 {item['price']}",
                    color=0x2ecc71
                )
                if item["image"]:
                    embed.set_thumbnail(url=item["image"])

                embed.set_footer(text=f"Keyword: {keyword}")

                try:
                    await channel.send(embed=embed)
                except Exception as e:
                    print(f"❌ Failed to send message: {e}", flush=True)

        await asyncio.sleep(SCAN_INTERVAL)


@client.event
async def on_ready():
    print(f"Logged in as {client.user}", flush=True)

    try:
        channel = await client.fetch_channel(CHANNEL_ID)
        await channel.send("✅ Vinted bot started and can post here.")
        print("✅ Startup message sent", flush=True)
    except Exception as e:
        print(f"❌ Startup send failed: {e}", flush=True)

    client.loop.create_task(scan_loop())


if __name__ == "__main__":
    client.run(DISCORD_TOKEN)
