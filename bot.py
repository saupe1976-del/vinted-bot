import discord
import requests
import asyncio
import os
from bs4 import BeautifulSoup

# ================= CONFIG =================

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

CHANNEL_ID = 123456789012345678  # <-- replace later

KEYWORDS = [
    "zara coat",
    "nike hoodie"
]

MAX_PRICE = 50
SCAN_INTERVAL = 600  # 10 minutes

BASE_URL = "https://www.vinted.co.uk/catalog"

HEADERS = {
    "User-Agent": "Mozilla/5.0"
}

seen_items = set()

# =========================================

intents = discord.Intents.default()
client = discord.Client(intents=intents)

def build_search_url(keyword):
    return f"{BASE_URL}?search_text={keyword.replace(' ', '+')}&price_to={MAX_PRICE}"

def fetch_items(keyword):
    url = build_search_url(keyword)
    r = requests.get(url, headers=HEADERS, timeout=15)

    soup = BeautifulSoup(r.text, "html.parser")
    items = soup.select("div.feed-grid__item")

    results = []

    for item in items:
        link_tag = item.find("a", href=True)
        if not link_tag:
            continue

        link = "https://www.vinted.co.uk" + link_tag["href"]

        if link in seen_items:
            continue

        title = item.get("title", "New Listing")
        price_tag = item.select_one("span[data-testid='price']")
        image_tag = item.find("img")

        price = price_tag.text if price_tag else "£?"
        image = image_tag["src"] if image_tag else None

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
    channel = client.get_channel(CHANNEL_ID)

    while not client.is_closed():
        for keyword in KEYWORDS:
            items = fetch_items(keyword)
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
                await channel.send(embed=embed)

        await asyncio.sleep(SCAN_INTERVAL)

@client.event
async def on_ready():
    print(f"Logged in as {client.user}")
    client.loop.create_task(scan_loop())

if __name__ == "__main__":
    client.run(DISCORD_TOKEN)
