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
intents.message_content = True  # for !oca_test

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
        except Exception as e:
            print(f"[OCA] Error loading seen IDs: {e}")
            seen_ids = set()
    else:
        seen_ids = set()
        print("[OCA] No seen file, starting fresh")


def save_seen_ids():
    try:
        with open(SEEN_FILE, "w", encoding="utf-8") as f:
            json.dump({"seen_ids": sorted(list(seen_ids))}, f, indent=2)
        print(f"[OCA] Saved {len(seen_ids)} seen IDs")
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
    links: List[str] = []

    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/settlements/" in href and href.endswith(".php"):
            full_url = urljoin(SETTLEMENTS_INDEX_URL, href)
            if full_url not in links:
                links.append(full_url)

    print(f"[OCA] Found {len(links)} links")
    return links


def fetch_settlement_details(url: str) -> Dict:
    """
    Scrape a single settlement page:
    - Title
    - Reward (Settlement Award)
    - Deadline
    - Summary (skipping Steve/boilerplate)
    - Proof required info
    - Claim URL (Submit Claim button)
    """
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
    except Exception as e:
        print(f"[OCA] Error fetching {url}: {e}")
        return {
            "id": url,
            "title": "Class Action Settlement",
            "reward": None,
            "deadline": None,
            "summary": None,
            "proof": None,
            "claim_url": None,
        }

    soup = BeautifulSoup(resp.text, "html.parser")

    # Title
    h1 = soup.find("h1")
    title = h1.get_text(strip=True) if h1 else "Class Action Settlement"

    reward = None
    deadline = None
    proof = None
    summary = None
    claim_url = None

    # Award / deadline / proof
    for tag in soup.find_all(["h2", "h3", "h4", "strong", "p", "li"]):
        text_full = tag.get_text(" ", strip=True)
        if not text_full:
            continue

        lower = text_full.lower()

        # Reward (Settlement Award)
        if reward is None and "settlement award" in lower:
            parts = text_full.split(":", 1)
            reward = parts[1].strip() if len(parts) > 1 else text_full.strip()

        # Deadline
        if deadline is None and "deadline" in lower:
            parts = text_full.split(":", 1)
            deadline = parts[1].strip() if len(parts) > 1 else text_full.strip()

        # Proof required
        if proof is None and ("proof of purchase" in lower or "proof required" in lower):
            parts = text_full.split(":", 1)
            proof = parts[1].strip() if len(parts) > 1 else text_full.strip()

    # Summary: first decent paragraph, skipping boilerplate & "Steve" author text
    for p in soup.find_all("p"):
        txt = p.get_text(" ", strip=True)
        if not txt:
            continue

        low = txt.lower()

        # Skip site boilerplate
        if "openclassactions.com is a news site providing information" in low:
            continue
        if "class action claims are submitted under penalty of perjury" in low:
            continue

        # Skip author/Steve lines
        if " steve " in low or low.startswith("steve ") or low.startswith("by steve"):
            continue

        summary = txt
        break

    # Claim button
    for tag in soup.find_all(["a", "button"]):
        text_btn = tag.get_text(" ", strip=True).lower()
        if "submit claim" in text_btn:
            href = tag.get("href")
            if href:
                claim_url = urljoin(url, href)
                break

    return {
        "id": url,
        "title": title,
        "reward": reward,
        "deadline": deadline,
        "summary": summary,
        "proof": proof,
        "claim_url": claim_url,
    }


# ==========================
# EMBED SENDER
# ==========================

async def send_settlement_embed(channel: discord.abc.Messageable, data: Dict):
    """
    Build and send the embed:
    - Title links ONLY to the claim URL (if any)
    - Fields: Reward, Deadline, Proof Required?
    - Disclaimer in description
    """
    title = data.get("title") or "Class Action Settlement"
    claim_url = data.get("claim_url")
    reward = data.get("reward")
    deadline = data.get("deadline")
    summary = data.get("summary")
    proof = data.get("proof")

    # Title links ONLY to claim URL. If none, it's not clickable.
    primary_link = claim_url if claim_url else None

    description_lines: List[str] = []

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
        url=primary_link,  # no OCA link ever
        description="\n".join(description_lines),
        color=0x00AAFF,
    )

    if reward:
        embed.add_field(name="Reward", value=reward, inline=False)

    if deadline:
        embed.add_field(name="Deadline", value=deadline, inline=False)

    if proof:
        embed.add_field(name="Proof Required?", value=proof, inline=False)

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
        print("[OCA] No links fetched")
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
async def oca_test(ctx: commands.Context):
    await ctx.send("FH Settlements Bot is online!")

    dummy = {
        "id": "dummy",
        "title": "Example Settlement",
        "reward": "$10 – $100 (example)",
        "deadline": "January 1, 2026",
        "summary": "This is a test settlement to confirm embed formatting.",
        "proof": "Proof of purchase may be required. See claim form for details.",
        "claim_url": "https://example.com/claim",
    }

    await send_settlement_embed(ctx.channel, dummy)


# ==========================
# RUN
# ==========================

if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
