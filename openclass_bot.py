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

try:
    CHANNEL_ID = int(CHANNEL_ID)
except ValueError:
    raise SystemExit("ERROR: DISCORD_CHANNEL_ID_OPENCLASS must be an integer")

intents = discord.Intents.default()
intents.message_content = True  # for !oca_test

bot = commands.Bot(command_prefix="!", intents=intents)

seen_ids = set()

# ==========================
# SEEN IDS
# ==========================

def load_seen_ids() -> None:
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


def save_seen_ids() -> None:
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


def normalize_proof_answer(text: str) -> str:
    """Normalize proof answer text into friendly Yes/No/Maybe sentences."""
    low = text.lower().strip()
    if any(kw in low for kw in ["not required", "none required", "no proof"]):
        return "No, proof is not required."
    if low in ("no", "none"):
        return "No, proof is not required."
    if "may be required" in low:
        return "Proof may be required."
    if "required" in low or low in ("yes", "y"):
        return "Yes, proof is required."
    # Fallback raw
    return text.strip()


def fetch_settlement_details(url: str) -> Dict:
    """
    Scrape a single settlement page:
    - Title
    - Reward (Settlement Award / Estimated Award / etc.)
    - Deadline
    - Summary (cleaned)
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

    # --------------------------
    # Title
    # --------------------------
    h1 = soup.find("h1")
    title = h1.get_text(strip=True) if h1 else "Class Action Settlement"

    reward = None
    deadline = None
    proof_value_on_same_line = None  # e.g. "Proof of Purchase: Required"
    proof_heading_tag = None         # e.g. "Proof Required?"
    claim_url = None

    # --------------------------
    # Reward / Deadline / Proof (first pass)
    # --------------------------
    for tag in soup.find_all(["h2", "h3", "h4", "strong", "p", "li"]):
        text_full = tag.get_text(" ", strip=True)
        if not text_full:
            continue

        lower = text_full.lower()

        # Reward / payout info – handle more phrasings
        if reward is None:
            if any(
                key in lower
                for key in [
                    "settlement award",
                    "estimated award",
                    "cash payment",
                    "payment amount",
                    "payout",
                    "benefit amount",
                    "settlement benefits",
                ]
            ):
                parts = text_full.split(":", 1)
                reward = parts[1].strip() if len(parts) > 1 else text_full.strip()

        # Deadline
        if deadline is None and "deadline" in lower:
            parts = text_full.split(":", 1)
            deadline = parts[1].strip() if len(parts) > 1 else text_full.strip()

        # Proof detection:
        if "proof" in lower:
            # Pattern like: "Proof of Purchase: Required / Not Required"
            if ":" in text_full and proof_value_on_same_line is None:
                label, value = text_full.split(":", 1)
                proof_value_on_same_line = value.strip()
            # Heading like "Proof Required?" or "Proof of Purchase"
            if proof_heading_tag is None:
                proof_heading_tag = tag

    # Fallback for reward: dollar amount line mentioning payout/award/cash/etc.
    if reward is None:
        for tag in soup.find_all(["p", "li", "strong"]):
            t = tag.get_text(" ", strip=True)
            low = t.lower()
            if "$" not in t:
                continue
            if any(word in low for word in ["award", "payment", "payout", "cash", "benefit"]):
                reward = t
                break

    # --------------------------
    # Summary detection
    # --------------------------
    summary = None
    backup_summary = None

    for p in soup.find_all("p"):
        txt = p.get_text(" ", strip=True)
        if not txt:
            continue

        low = txt.lower()

        # Skip boilerplate
        if "openclassactions.com is a news site providing information" in low:
            continue
        if "class action claims are submitted under penalty of perjury" in low:
            continue

        # Skip Steve author stuff
        if " steve " in low or low.startswith("steve ") or low.startswith("by steve"):
            continue

        # Skip PDF viewer messages
        if "browser does not support viewing pdfs inline" in low:
            continue
        if "download the pdf" in low:
            continue

        # Skip big step/bullet lists as primary summary
        if "step 1" in low or "step 2" in low or " • " in txt:
            if backup_summary is None:
                backup_summary = txt
            continue

        # Prefer a nice descriptive paragraph
        if len(txt) > 80:
            summary = txt
            break

        # Short but maybe still useful – backup
        if backup_summary is None:
            backup_summary = txt

    if summary is None:
        summary = backup_summary

    # --------------------------
    # Proof normalization
    # --------------------------
    proof = None

    if proof_value_on_same_line:
        # e.g. "Required", "Not Required", "None Required"
        proof = normalize_proof_answer(proof_value_on_same_line)

    elif proof_heading_tag is not None:
        # Look at the next non-empty sibling after the heading
        sib = proof_heading_tag.find_next_sibling()
        while sib is not None and not sib.get_text(" ", strip=True):
            sib = sib.find_next_sibling()

        if sib is not None:
            ans_text = sib.get_text(" ", strip=True)
            proof = normalize_proof_answer(ans_text)

    # If no structured info found, leave proof as None

    # --------------------------
    # Claim button URL
    # --------------------------
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

async def send_settlement_embed(channel: discord.abc.Messageable, data: Dict) -> None:
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
        url=primary_link,
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
        "proof": "Yes, proof is required.",
        "claim_url": "https://example.com/claim",
    }

    await send_settlement_embed(ctx.channel, dummy)


# ==========================
# RUN
# ==========================

if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
