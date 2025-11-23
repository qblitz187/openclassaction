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

# How often to check the site for new settlements
CHECK_INTERVAL_MINUTES = 60  # every 60 minutes

# How long to wait between posting individual settlements
POST_INTERVAL_SECONDS = 10  # 10 seconds between posts

# Hard cap on how many NEW settlements to post per run
MAX_POSTS_PER_RUN = 30

# HTTP headers to look more like a normal browser
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# ==========================
# ENV / DISCORD SETUP
# ==========================

# Load .env for local dev. On Railway the vars come from its UI.
load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN_OPENCLASS")
CHANNEL_ID = os.getenv("DISCORD_CHANNEL_ID_OPENCLASS")

if not DISCORD_TOKEN:
    raise SystemExit("ERROR: DISCORD_TOKEN_OPENCLASS is not set (env or Railway).")

if not CHANNEL_ID:
    raise SystemExit("ERROR: DISCORD_CHANNEL_ID_OPENCLASS is not set (env or Railway).")

try:
    CHANNEL_ID = int(CHANNEL_ID)
except ValueError:
    raise SystemExit("ERROR: DISCORD_CHANNEL_ID_OPENCLASS must be an integer.")

# Intents – keep it simple: only ask for message_content (for !oca_test).
# Make sure "Message Content Intent" is enabled in the Developer Portal.
intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

# Seen IDs cache
seen_ids = set()


# ==========================
# SEEN IDS PERSISTENCE
# ==========================

def load_seen_ids() -> None:
    """Load already-posted settlement IDs from disk."""
    global seen_ids
    if os.path.exists(SEEN_FILE):
        try:
            with open(SEEN_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            seen_ids = set(data.get("seen_ids", []))
            print(f"[OCA] Loaded {len(seen_ids)} seen settlements from {SEEN_FILE}")
        except Exception as e:
            print(f"[OCA] Failed to load {SEEN_FILE}: {e}")
            seen_ids = set()
    else:
        seen_ids = set()
        print(f"[OCA] No {SEEN_FILE} found. Starting with an empty seen list.")


def save_seen_ids() -> None:
    """Persist the seen IDs to disk."""
    try:
        with open(SEEN_FILE, "w", encoding="utf-8") as f:
            json.dump({"seen_ids": sorted(list(seen_ids))}, f, indent=2)
        print(f"[OCA] Saved {len(seen_ids)} seen settlements to {SEEN_FILE}")
    except Exception as e:
        print(f"[OCA] Failed to save {SEEN_FILE}: {e}")


# ==========================
# SCRAPING HELPERS
# ==========================

def fetch_settlement_links() -> List[str]:
    """
    Grab all settlement detail URLs from the settlements index page.
    Returns a list of absolute URLs.
    """
    print("[OCA] Fetching settlements index...")

    try:
        resp = requests.get(
            SETTLEMENTS_INDEX_URL,
            headers={"User-Agent": USER_AGENT},
            timeout=20,
        )
        resp.raise_for_status()
    except Exception as e:
        print(f"[OCA] Error fetching settlements index: {e}")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    links: List[str] = []

    for a in soup.find_all("a", href=True):
        href = a["href"]
        # We only care about settlement detail pages under /settlements/
        if "/settlements/" in href and href.endswith(".php"):
            full_url = urljoin(SETTLEMENTS_INDEX_URL, href)
            if full_url not in links:
                links.append(full_url)

    print(f"[OCA] Found {len(links)} settlement links on index.")
    return links


def fetch_settlement_details(url: str) -> Dict:
    """
    Given a single settlement URL, fetch useful details for the embed.
    Defensive so one bad page doesn't kill the loop.
    """
    print(f"[OCA] Fetching settlement page: {url}")

    headers = {
        "User-Agent": USER_AGENT,
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": SETTLEMENTS_INDEX_URL,
    }

    try:
        # small random sleep so we don't hammer them instantly
        time.sleep(random.uniform(0.8, 1.6))

        resp = requests.get(url, headers=headers, timeout=25)
        resp.raise_for_status()
    except Exception as e:
        print(f"[OCA] Error fetching {url}: {e}")
        return {
            "id": url,
            "title": "Class Action Settlement",
            "url": url,
            "award": None,
            "deadline": None,
            "summary": None,
        }

    soup = BeautifulSoup(resp.text, "html.parser")

    # Title (usually the main H1)
    h1 = soup.find("h1")
    title = h1.get_text(strip=True) if h1 else "Class Action Settlement"

    award = None
    deadline = None

    # Look for "Settlement Award" and "Deadline" in various tags
    for tag in soup.find_all(["h2", "h3", "h4", "strong", "p", "li"]):
        text = tag.get_text(" ", strip=True)
        if not text:
            continue

        lower = text.lower()

        if award is None and "settlement award" in lower:
            parts = text.split(":", 1)
            award = parts[1].strip() if len(parts) > 1 else text.strip()

        if deadline is None and "deadline" in lower:
            parts = text.split(":", 1)
            deadline = parts[1].strip() if len(parts) > 1 else text.strip()

        if award and deadline:
            break

    # Try to grab a reasonable first paragraph as a summary (skip boilerplate)
    summary = None
    for p in soup.find_all("p"):
        txt = p.get_text(" ", strip=True)
        if not txt:
            continue

        # Skip boilerplate disclaimer chunks they repeat
        if "OpenClassActions.com is a news site providing information" in txt:
            continue
        if "Class action claims are submitted under penalty of perjury" in txt:
            continue

        summary = txt
        break

    return {
        "id": url,       # Using URL as unique ID is enough
        "title": title,
        "url": url,
        "award": award,
        "deadline": deadline,
        "summary": summary,
    }


async def send_settlement_embed(channel: discord.abc.Messageable, data: Dict) -> None:
    """
    Build and send a Discord embed for a single settlement.
    Includes your disclaimer and no source branding.
    """
    title = data.get("title") or "Class Action Settlement"
    url = data.get("url")
    award = data.get("award")
    deadline = data.get("deadline")
    summary = data.get("summary")

    description_lines: List[str] = []

    if summary:
        # Limit length so it doesn't get insane
        if len(summary) > 500:
            summary = summary[:497] + "..."
        description_lines.append(summary)

    # Your disclaimer
    disclaimer = (
        "⚠️ Apply for these at your own risk, and only if you were "
        "actually and legally affected by the issue. Do not submit false claims."
    )

    if description_lines:
        description_lines.append("")  # blank line before disclaimer

    description_lines.append(disclaimer)

    embed = discord.Embed(
        title=title,
        url=url,
        description="\n".join(description_lines),
        color=0x00AAFF,
    )

    if award:
        embed.add_field(name="Settlement Award", value=award, inline=False)

    if deadline:
        embed.add_field(name="Deadline", value=deadline, inline=False)

    # No explicit source footer
    await channel.send(embed=embed)


# ==========================
# BACKGROUND TASK
# ==========================

@tasks.loop(minutes=CHECK_INTERVAL_MINUTES)
async def check_settlements():
    """
    Periodic task that:
    - Fetches index
    - Filters out seen URLs
    - Posts up to MAX_POSTS_PER_RUN new settlements with spacing
    """
    await bot.wait_until_ready()
    channel = bot.get_channel(CHANNEL_ID)

    if channel is None:
        print(f"[OCA] ERROR: Could not find channel with ID {CHANNEL_ID}")
        return

    links = fetch_settlement_links()
    if not links:
        print("[OCA] No links found or error fetching index.")
        return

    global seen_ids

    # Only post ones we haven't seen before
    new_links = [url for url in links if url not in seen_ids]

    if not new_links:
        print("[OCA] No new settlements to post this cycle.")
        return

    print(f"[OCA] Found {len(new_links)} new settlements to post this run.")

    # Sort for stable ordering and cap the number per run
    new_links = sorted(new_links)[:MAX_POSTS_PER_RUN]

    for i, url in enumerate(new_links, start=1):
        print(f"[OCA] Posting {i}/{len(new_links)}: {url}")
        details = fetch_settlement_details(url)
        await send_settlement_embed(channel, details)

        # Mark as seen and save
        seen_ids.add(details["id"])
        save_seen_ids()

        # Space out posts
        if i < len(new_links):
            await asyncio.sleep(POST_INTERVAL_SECONDS)


# ==========================
# BOT EVENTS / COMMANDS
# ==========================

@bot.event
async def on_ready():
    print(f"[OCA] Logged in as {bot.user} (ID: {bot.user.id})")
    print("[OCA] Loading seen IDs...")
    load_seen_ids()

    if not check_settlements.is_running():
        print(
            f"[OCA] Starting background task: check_settlements "
            f"every {CHECK_INTERVAL_MINUTES} minutes"
        )
        check_settlements.start()


@bot.command(name="oca_test")
async def oca_test(ctx: commands.Context):
    """
    Simple command to make sure the bot can talk and send an embed.
    """
    await ctx.send("FH Settlements Bot is online and ready!")

    dummy = {
        "id": "test",
        "title": "Test Class Action Settlement (Dummy)",
        "url": "https://www.openclassactions.com/",
        "award": "$10 - $100 (example)",
        "deadline": "Example Deadline",
        "summary": (
            "This is just a test settlement to confirm that the bot can "
            "post embeds correctly in this channel."
        ),
    }
    await send_settlement_embed(ctx.channel, dummy)


# ==========================
# RUN
# ==========================

if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
