import os
import json
import asyncio
import random
import time
from typing import Dict, List

import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin

import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv

# ==========================
# CONFIG
# ==========================

SETTLEMENTS_INDEX_URL = "https://www.openclassactions.com/settlements.php"
SEEN_FILE = "openclass_seen.json"

CHECK_INTERVAL_MINUTES = 60
POST_INTERVAL_SECONDS = 10
MAX_POSTS_PER_RUN = 30

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# ==========================
# ENV / DISCORD SETUP
# ==========================

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN_OPENCLASS")
CHANNEL_ID = os.getenv("DISCORD_CHANNEL_ID_OPENCLASS")

if not DISCORD_TOKEN:
    raise SystemExit("ERROR: DISCORD_TOKEN_OPENCLASS not set")

if not CHANNEL_ID:
    raise SystemExit("ERROR: DISCORD_CHANNEL_ID_OPENCLASS not set")

CHANNEL_ID = int(CHANNEL_ID)

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

seen_ids = set()

# ==========================
# SEEN IDS
# ==========================

def load_seen_ids():
    global seen_ids
    if os.path.exists(SEEN_FILE):
        try:
            with open(SEEN_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            seen_ids = set(data.get("seen_ids", []))
            print(f"[OCA] Loaded {len(seen_ids)} seen IDs")
        except:
            seen_ids = set()
    else:
        seen_ids = set()

def save_seen_ids():
    try:
        with open(SEEN_FILE, "w", encoding="utf-8") as f:
            json.dump({"seen_ids": sorted(list(seen_ids))}, f, indent=2)
        print(f"[OCA] Saved seen IDs")
    except Exception as e:
        print(f"[OCA] Failed saving seen IDs: {e}")

# ==========================
# SCRAPING
# ==========================

def fetch_settlement_links() -> List[str]:
    print("[OCA] Fetching index...")
    try:
        resp = requests.get(
            SETTLEMENTS_INDEX_URL,
            headers={"User-Agent": USER_AGENT},
            timeout=20,
        )
        resp.raise_for_status()
    except Exception as e:
        print(f"[OCA] Index error: {e}")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    links = []

    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/settlements/" in href and href.endswith(".php"):
            links.append(urljoin(SETTLEMENTS_INDEX_URL, href))

    print(f"[OCA] Found {len(links)} links")
    return links


def fetch_settlement_details(url: str) -> Dict:
    print(f"[OCA] Fetching: {url}")

    headers = {
        "User-Agent": USER_AGENT,
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": SETTLEMENTS_INDEX_URL,
    }

    try:
        time.sleep(random.uniform(0.8, 1.6))
        resp = requests.get(url, headers=headers, timeout=25)
        resp.raise_for_status()
    except Exception:
        return {
            "id": url,
            "title": "Class Action Settlement",
            "claim_url": None,
            "award": None,
            "deadline": None,
            "summary": None,
        }

    soup = BeautifulSoup(resp.text, "html.parser")

    # Title
    h1 = soup.find("h1")
    title = h1.get_text(strip=True) if h1 else "Class Action Settlement"

    award = None
    deadline = None
    summary = None
    claim_url = None

    # Award / deadline
    for tag in soup.find_all(["h2", "h3", "h4", "strong", "p", "li"]):
        txt = tag.get_text(" ", strip=True).lower()
        if "settlement award" in txt:
            award = tag.get_text(strip=True).split(":", 1)[-1].strip()
        if "deadline" in txt:
            deadline = tag.get_text(strip=True).split(":", 1)[-1].strip()

    # Summary
    for p in soup.find_all("p"):
        txt = p.get_text(" ", strip=True)
        if not txt:
            continue
        if "OpenClassActions.com" in txt:
            continue
        if "submitted under penalty" in txt:
            continue
        summary = txt
        break

    # Claim button
    for tag in soup.find_all(["a", "button"]):
        text = tag.get_text(" ", strip=True).lower()
        if "submit claim" in text:
            href = tag.get("href")
            if href:
                claim_url = urljoin(url, href)
                break

    return {
        "id": url,
        "title": title,
        "claim_url": claim_url,
        "award": award,
        "deadline": deadline,
        "summary": summary,
    }

# ==========================
# EMBED SENDER
# ==========================

async def send_settlement_embed(channel, data: Dict):
    title = data.get("title") or "Class Action Settlement"

    claim_url = data.get("claim_url")  # real claim form
    award = data.get("award")
    deadline = data.get("deadline")
    summary = data.get("summary")

    # Title links ONLY to claim_url. If no claim URL → no link.
    primary_link = claim_url if claim_url else None

    description_lines = []

    if summary:
        if len(summary) > 500:
            summary = summary[:497] + "..."
        description_lines.append(summary)

    description_lines.append("")
    description_lines.append(
        "⚠️ Apply at your own risk. Only submit claims if you were actually and legally affected."
    )

    embed = discord.Embed(
        title=title,
        url=primary_link,   # ❗ NO OCA LINK EVER
        description="\n".join(description_lines),
        color=0x00AAFF,
    )

    if award:
        embed.add_field(name="Settlement Award", value=award, inline=False)

    if deadline:
        embed.add_field(name="Deadline", value=deadline, inline=False)

    if claim_url:
        embed.add_field(
            name="Submit Claim",
            value=f"[Click here to go to the official claim form]({claim_url})",
            inline=False,
        )

    await channel.send(embed=embed)

# ==========================
# BACKGROUND LOOP
# ==========================

@tasks.loop(minutes=CHECK_INTERVAL_MINUTES)
async def check_settlements():
    await bot.wait_until_ready()
    channel = bot.get_channel(CHANNEL_ID)

    if channel is None:
        print("[OCA] Channel not found")
        return

    links = fetch_settlement_links()
    if not links:
        return

    global seen_ids
    new_links = [u for u in links if u not in seen_ids]

    if not new_links:
        print("[OCA] No new settlements")
        return

    new_links = sorted(new_links)[:MAX_POSTS_PER_RUN]

    print(f"[OCA] Posting {len(new_links)} new settlements")

    for i, url in enumerate(new_links, start=1):
        details = fetch_settlement_details(url)
        await send_settlement_embed(channel, details)
        seen_ids.add(details["id"])
        save_seen_ids()

        if i < len(new_links):
            await asyncio.sleep(POST_INTERVAL_SECONDS)

# ==========================
# EVENTS & COMMANDS
# ==========================

@bot.event
async def on_ready():
    print(f"[OCA] Logged in as {bot.user}")
    load_seen_ids()
    if not check_settlements.is_running():
        check_settlements.start()

@bot.command(name="oca_test")
async def oca_test(ctx):
    await ctx.send("FH Settlements Bot is online!")

    dummy = {
        "id": "dummy",
        "title": "Example Settlement",
        "award": "$10 - $100",
        "deadline": "Jan 1 2026",
        "summary": "This is a test only.",
        "claim_url": "https://example.com/claim",
    }

    await send_settlement_embed(ctx.channel, dummy)

# ==========================
# RUN
# ==========================

if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
