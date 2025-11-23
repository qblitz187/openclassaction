import os
import json
import asyncio
import requests

from bs4 import BeautifulSoup
from urllib.parse import urljoin

import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv

# ---------------------------
# Config / Globals
# ---------------------------

SETTLEMENTS_INDEX_URL = "https://www.openclassactions.com/settlements.php"
SEEN_FILE = "openclass_seen.json"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# Load .env for local dev; on Railway, env vars come from the service config
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
    raise SystemExit("ERROR: DISCORD_CHANNEL_ID_OPENCLASS must be an integer")

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

seen_ids = set()


# ---------------------------
# Seen IDs persistence
# ---------------------------

def load_seen_ids():
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


def save_seen_ids():
    try:
        with open(SEEN_FILE, "w", encoding="utf-8") as f:
            json.dump({"seen_ids": sorted(list(seen_ids))}, f, indent=2)
        print(f"[OCA] Saved {len(seen_ids)} seen settlements to {SEEN_FILE}")
    except Exception as e:
        print(f"[OCA] Failed to save {SEEN_FILE}: {e}")


# ---------------------------
# Scraping helpers
# ---------------------------

def fetch_settlement_links():
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
    links = []

    for a in soup.find_all("a", href=True):
        href = a["href"]
        # We only care about settlement detail pages under /settlements/
        if "/settlements/" in href and href.endswith(".php"):
            full_url = urljoin(SETTLEMENTS_INDEX_URL, href)
            if full_url not in links:
                links.append(full_url)

    print(f"[OCA] Found {len(links)} settlement links.")
    return links


def fetch_settlement_details(url: str):
    """
    Given a single settlement URL, fetch useful details for the embed.
    """
    print(f"[OCA] Fetching settlement page: {url}")
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=20,
        )
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

    # Look through various tags for "Settlement Award:" and "Deadline:"
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

    # Try to grab a reasonable first paragraph as a summary (skip disclaimers)
    summary = None
    for p in soup.find_all("p"):
        txt = p.get_text(" ", strip=True)
        if not txt:
            continue
        if "OpenClassActions.com is a news site providing information" in txt:
            continue
        if "Class action claims are submitted under penalty of perjury" in txt:
            continue
        summary = txt
        break

    return {
        "id": url,       # Use URL as unique ID
        "title": title,
        "url": url,
        "award": award,
        "deadline": deadline,
        "summary": summary,
    }


async def send_settlement_embed(channel: discord.abc.Messageable, data: dict):
    """
    Build and send a Discord embed for a single settlement.
    """
    title = data.get("title") or "Class Action Settlement"
    url = data.get("url")
    award = data.get("award")
    deadline = data.get("deadline")
    summary = data.get("summary")

    description_lines = []
    if summary:
        if len(summary) > 500:
            summary = summary[:497] + "..."
        description_lines.append(summary)

    if not description_lines:
        description_lines.append(
            "New class action settlement listed on OpenClassActions.com."
        )

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

    embed.set_footer(text="Source: OpenClassActions.com")

    await channel.send(embed=embed)


# ---------------------------
# Background task
# ---------------------------

@tasks.loop(minutes=60)  # check every hour; tweak if needed
async def check_settlements():
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

    print(f"[OCA] Found {len(new_links)} new settlements to post.")

    for url in new_links:
        details = fetch_settlement_details(url)
        await send_settlement_embed(channel, details)

        # Mark as seen and save
        seen_ids.add(details["id"])
        save_seen_ids()

        # Space out posts (2 minutes between each)
        await asyncio.sleep(120)


# ---------------------------
# Bot events / commands
# ---------------------------

@bot.event
async def on_ready():
    print(f"[OCA] Logged in as {bot.user} (ID: {bot.user.id})")
    print("[OCA] Loading seen IDs...")
    load_seen_ids()

    if not check_settlements.is_running():
        print("[OCA] Starting background task: check_settlements")
        check_settlements.start()


@bot.command(name="oca_test")
async def oca_test(ctx):
    """
    Simple command to make sure the bot can talk and send an embed.
    """
    await ctx.send("OpenClassActions bot is online and ready!")

    dummy = {
        "id": "test",
        "title": "Test Class Action Settlement (Dummy)",
        "url": "https://www.openclassactions.com/",
        "award": "$10 - $100 (example)",
        "deadline": "Example Deadline",
        "summary": "This is just a test embed to confirm that the bot can post to this channel.",
    }
    await send_settlement_embed(ctx.channel, dummy)


# ---------------------------
# Run the bot
# ---------------------------

if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
