import os
import json
import asyncio
import random
import time
from typing import Dict, List, Optional
from datetime import datetime, timedelta

import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin

import discord
from discord.ext import commands, tasks
from discord import app_commands
from dotenv import load_dotenv

# ==========================
# CONFIG
# ==========================

SETTLEMENTS_INDEX_URL = "https://www.openclassactions.com/settlements.php"
SEEN_FILE = "openclass_seen.json"

# Base / max intervals for smart scheduling
BASE_INTERVAL_MINUTES = 60          # default when new stuff is found
MAX_INTERVAL_MINUTES = 180          # cap when nothing new for a while

POST_INTERVAL_SECONDS = 10          # spacing between posts
MAX_POSTS_PER_RUN = 30              # cap per scan

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
intents.message_content = True  # for legacy text commands

bot = commands.Bot(command_prefix="!", intents=intents)

seen_ids = set()

# Smart scheduling state
CURRENT_INTERVAL_MINUTES = BASE_INTERVAL_MINUTES
last_scan_time: Optional[datetime] = None

# Global toggle for auto-posting
AUTO_POSTING_ENABLED = True

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
# SCRAPING HELPERS
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


def score_proof_answer(text: str) -> int:
    """Score a proof answer: higher = more confident / specific."""
    low = text.lower().strip()

    # Strong "no" indicators
    if any(kw in low for kw in ["not required", "none required", "no proof"]):
        return 3
    if low in ("no", "none"):
        return 3

    # Medium "maybe" indicator
    if "may be required" in low:
        return 2

    # Plain required / yes
    if "required" in low or low in ("yes", "y"):
        return 1

    return 0


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


def is_bad_reward_heading(text: str) -> bool:
    """
    Return True if this 'reward' text is obviously just a heading/question,
    not an actual payout line.
    """
    low = text.lower().strip()

    # Question-style headings with no actual amount or variation keywords
    if text.endswith("?"):
        if "$" not in text and not any(k in low for k in ["varies", "pending", "tbd", "to be determined"]):
            return True

    # Common heading phrases that aren't payouts
    bad_phrases = [
        "who is eligible for a payout",
        "who is eligible",
        "who can file",
        "who may qualify",
        "what is this settlement",
        "what is the",
        "how do i file",
        "how can i file",
        "how to file",
        "class members",
        "who is included",
    ]
    if any(p in low for p in bad_phrases):
        return True

    return False


def simplify_summary(text: str) -> str:
    """
    Simple one-sentence-ish cleanup:
    - Take first sentence (or first ~220 chars)
    - Strip extra whitespace
    - Avoid cutting mid-word ugly
    """
    if not text:
        return text

    raw = " ".join(text.split())  # collapse whitespace

    # Try first sentence by punctuation
    for sep in [". ", "? ", "! "]:
        if sep in raw:
            first = raw.split(sep, 1)[0].strip()
            if len(first) > 40:  # don't return super-short junk
                return first + sep.strip()

    # Fallback: truncate softly to ~220 chars
    if len(raw) <= 220:
        return raw

    cut = raw[:220]
    # Try to cut on last space before the end
    last_space = cut.rfind(" ")
    if last_space > 80:
        cut = cut[:last_space]
    return cut + "..."


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
            "reward": "Not listed",
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
    claim_url = None

    # We'll collect proof candidates here
    proof_same_line_candidates: List[str] = []  # "Proof required: No"
    proof_heading_tags: List[object] = []      # tags like "Proof Required?"

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
                candidate = parts[1].strip() if len(parts) > 1 else text_full.strip()
                if not is_bad_reward_heading(candidate):
                    reward = candidate

        # Deadline
        if deadline is None and "deadline" in lower:
            parts = text_full.split(":", 1)
            deadline = parts[1].strip() if len(parts) > 1 else text_full.strip()

        # Proof detection
        if "proof" in lower:
            if ":" in text_full:
                # "Proof required: No"
                proof_same_line_candidates.append(text_full)
            else:
                # Heading like "Proof Required?"
                proof_heading_tags.append(tag)

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

    # If still nothing useful, set explicit "Not listed"
    if reward is None:
        reward = "Not listed"

    # --------------------------
    # Summary detection
    # --------------------------
    full_summary = None
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
            full_summary = txt
            break

        # Short but maybe still useful – backup
        if backup_summary is None:
            backup_summary = txt

    if full_summary is None:
        full_summary = backup_summary

    cleaned_summary = simplify_summary(full_summary) if full_summary else None

    # --------------------------
    # Proof normalization
    # --------------------------
    proof_answer_text = None
    best_score = -1

    # 1) Lines like "Proof required: No"
    for line in proof_same_line_candidates:
        val = line.split(":", 1)[1].strip() if ":" in line else line.strip()
        s = score_proof_answer(val)
        if s > best_score:
            best_score = s
            proof_answer_text = val

    # 2) Headings like "Proof Required?" + next sibling answer
    for heading in proof_heading_tags:
        sib = heading.find_next_sibling()
        while sib is not None and not sib.get_text(" ", strip=True):
            sib = sib.find_next_sibling()

        if sib is None:
            continue

        val = sib.get_text(" ", strip=True)
        s = score_proof_answer(val)
        if s > best_score:
            best_score = s
            proof_answer_text = val

    proof = None
    if proof_answer_text:
        proof = normalize_proof_answer(proof_answer_text)

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
        "summary": cleaned_summary,
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
    reward = data.get("reward") or "Not listed"
    deadline = data.get("deadline")
    summary = data.get("summary")
    proof = data.get("proof")

    primary_link = claim_url if claim_url else None

    description_lines: List[str] = []

    if summary:
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

    # Reward always shown, even if "Not listed"
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
# CORE SCAN FUNCTION
# ==========================

async def run_settlement_scan(
    target_channel: Optional[discord.abc.Messageable] = None,
    max_posts: Optional[int] = None,
) -> int:
    """
    Core logic to:
    - Fetch index
    - Filter out seen URLs
    - Post up to max_posts new settlements
    Returns number of new settlements posted.
    """
    channel = target_channel or bot.get_channel(CHANNEL_ID)

    if channel is None:
        print("[OCA] ERROR: Channel not found")
        return 0

    links = fetch_settlement_links()
    if not links:
        print("[OCA] No links fetched")
        return 0

    global seen_ids

    new_links = [u for u in links if u not in seen_ids]
    if not new_links:
        print("[OCA] No new settlements to post")
        return 0

    new_links = sorted(new_links)
    if max_posts is None:
        max_posts = MAX_POSTS_PER_RUN
    new_links = new_links[:max_posts]

    print(f"[OCA] Posting {len(new_links)} new settlements")

    count = 0
    for i, url in enumerate(new_links, start=1):
        details = fetch_settlement_details(url)
        await send_settlement_embed(channel, details)

        seen_ids.add(details["id"])
        save_seen_ids()
        count += 1

        if i < len(new_links):
            await asyncio.sleep(POST_INTERVAL_SECONDS)

    return count


# ==========================
# SMART SCHEDULER
# ==========================

@tasks.loop(seconds=60)
async def settlement_scheduler():
    """
    Runs once a minute, but only triggers a scan when
    CURRENT_INTERVAL_MINUTES has passed since last_scan_time.
    Interval adjusts based on whether new settlements were found.
    Respects AUTO_POSTING_ENABLED.
    """
    global last_scan_time, CURRENT_INTERVAL_MINUTES, AUTO_POSTING_ENABLED

    await bot.wait_until_ready()

    # If auto-posting is disabled, do nothing
    if not AUTO_POSTING_ENABLED:
        return

    now = datetime.utcnow()
    if last_scan_time is None:
        print(f"[OCA] First scheduled scan starting (interval {CURRENT_INTERVAL_MINUTES} min)")
        new_count = await run_settlement_scan()
        last_scan_time = datetime.utcnow()

        if new_count == 0:
            CURRENT_INTERVAL_MINUTES = min(MAX_INTERVAL_MINUTES, CURRENT_INTERVAL_MINUTES * 2)
        else:
            CURRENT_INTERVAL_MINUTES = BASE_INTERVAL_MINUTES

        print(f"[OCA] Next scheduled scan in {CURRENT_INTERVAL_MINUTES} minutes")
        return

    # Check if it's time to scan again
    if now - last_scan_time < timedelta(minutes=CURRENT_INTERVAL_MINUTES):
        return

    print(f"[OCA] Scheduled scan triggered (interval {CURRENT_INTERVAL_MINUTES} min)")
    new_count = await run_settlement_scan()
    last_scan_time = datetime.utcnow()

    if new_count == 0:
        CURRENT_INTERVAL_MINUTES = min(MAX_INTERVAL_MINUTES, CURRENT_INTERVAL_MINUTES * 2)
    else:
        CURRENT_INTERVAL_MINUTES = BASE_INTERVAL_MINUTES

    print(f"[OCA] Scan complete; new_count={new_count}. Next scan in {CURRENT_INTERVAL_MINUTES} minutes")


# ==========================
# EVENTS
# ==========================

@bot.event
async def on_ready():
    global last_scan_time
    print(f"[OCA] Logged in as {bot.user}")
    load_seen_ids()

    if not settlement_scheduler.is_running():
        settlement_scheduler.start()

    # Sync slash commands
    try:
        synced = await bot.tree.sync()
        print(f"[OCA] Synced {len(synced)} slash command(s).")
    except Exception as e:
        print(f"[OCA] Failed to sync slash commands: {e}")

    if last_scan_time is None:
        last_scan_time = None  # explicitly mark as not run yet


# ==========================
# LEGACY TEXT COMMANDS (!)
# ==========================

@bot.command(name="oca_test")
async def oca_test(ctx: commands.Context):
    """Simple check to ensure the bot responds and embeds work."""
    await ctx.send("FH Settlements Bot is online and ready!")

    dummy = {
        "id": "dummy",
        "title": "Example Settlement",
        "reward": "$10 – $100 (example)",
        "deadline": "January 1, 2026",
        "summary": "This is a test settlement to confirm embed formatting in this channel.",
        "proof": "Yes, proof is required.",
        "claim_url": "https://example.com/claim",
    }

    await send_settlement_embed(ctx.channel, dummy)


@bot.command(name="oca_next")
@commands.has_permissions(manage_guild=True)
async def oca_next(ctx: commands.Context):
    """
    Manually trigger a scan & post new settlements in this channel.
    Does not affect the smart scheduler interval.
    Admin (Manage Server) only.
    """
    await ctx.send("🔎 Checking for new settlements now...")
    new_count = await run_settlement_scan(target_channel=ctx.channel)

    if new_count == 0:
        await ctx.send("No new settlements found.")
    else:
        await ctx.send(f"Posted {new_count} new settlement(s).")


@oca_next.error
async def oca_next_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("You need `Manage Server` permissions to use this command.")


@bot.command(name="oca_info")
@commands.has_permissions(manage_guild=True)
async def oca_info(ctx: commands.Context):
    """
    Show internal status:
    - seen count
    - current smart interval
    - last scan time
    - index URL
    - auto-posting state
    """
    global last_scan_time, CURRENT_INTERVAL_MINUTES, AUTO_POSTING_ENABLED

    seen_count = len(seen_ids)
    last_scan_str = last_scan_time.isoformat(timespec="seconds") + " UTC" if last_scan_time else "Never"
    auto_state = "Enabled" if AUTO_POSTING_ENABLED else "Disabled"
    msg = (
        f"**FH Settlements Bot Info**\n"
        f"- Seen settlements: `{seen_count}`\n"
        f"- Current scan interval: `{CURRENT_INTERVAL_MINUTES}` minutes\n"
        f"- Last scheduled scan: `{last_scan_str}`\n"
        f"- Auto-posting: `{auto_state}`\n"
        f"- Index source (scraped): `{SETTLEMENTS_INDEX_URL}`\n"
        f"- Posts per scan (max): `{MAX_POSTS_PER_RUN}`\n"
        f"- Delay between posts: `{POST_INTERVAL_SECONDS}` seconds"
    )
    await ctx.send(msg)


@oca_info.error
async def oca_info_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("You need `Manage Server` permissions to use this command.")


@bot.command(name="oca_stop")
@commands.has_permissions(manage_guild=True)
async def oca_stop(ctx: commands.Context):
    """Stop automatic settlement posting (scheduler stays running but idle)."""
    global AUTO_POSTING_ENABLED
    AUTO_POSTING_ENABLED = False
    await ctx.send("🛑 Automatic settlement posting has been **stopped**.")


@oca_stop.error
async def oca_stop_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("You need `Manage Server` permissions to use this command.")


@bot.command(name="oca_start")
@commands.has_permissions(manage_guild=True)
async def oca_start(ctx: commands.Context):
    """Resume automatic settlement posting."""
    global AUTO_POSTING_ENABLED
    AUTO_POSTING_ENABLED = True
    await ctx.send("▶️ Automatic settlement posting has been **resumed**.")


@oca_start.error
async def oca_start_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("You need `Manage Server` permissions to use this command.")


# ==========================
# SLASH COMMANDS (/)
# ==========================

@bot.tree.command(name="oca_test", description="Check that the FH Settlements Bot is online.")
async def slash_oca_test(interaction: discord.Interaction):
    await interaction.response.send_message(
        "FH Settlements Bot is online and ready! Posting a sample settlement embed here...",
        ephemeral=True,
    )

    dummy = {
        "id": "dummy",
        "title": "Example Settlement",
        "reward": "$10 – $100 (example)",
        "deadline": "January 1, 2026",
        "summary": "This is a test settlement to confirm embed formatting in this channel.",
        "proof": "Yes, proof is required.",
        "claim_url": "https://example.com/claim",
    }

    if interaction.channel:
        await send_settlement_embed(interaction.channel, dummy)


@bot.tree.command(name="oca_next", description="Manually scan and post new settlements in this channel.")
@app_commands.checks.has_permissions(manage_guild=True)
async def slash_oca_next(interaction: discord.Interaction):
    if interaction.channel is None:
        await interaction.response.send_message(
            "This command must be used in a server text channel.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(thinking=True, ephemeral=True)
    new_count = await run_settlement_scan(target_channel=interaction.channel)

    if new_count == 0:
        await interaction.followup.send("No new settlements found.", ephemeral=True)
    else:
        await interaction.followup.send(f"Posted {new_count} new settlement(s).", ephemeral=True)


@slash_oca_next.error
async def slash_oca_next_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message(
            "You need `Manage Server` permissions to use this command.",
            ephemeral=True,
        )


@bot.tree.command(name="oca_info", description="Show internal info about the FH Settlements Bot.")
@app_commands.checks.has_permissions(manage_guild=True)
async def slash_oca_info(interaction: discord.Interaction):
    global last_scan_time, CURRENT_INTERVAL_MINUTES, AUTO_POSTING_ENABLED

    seen_count = len(seen_ids)
    last_scan_str = last_scan_time.isoformat(timespec="seconds") + " UTC" if last_scan_time else "Never"
    auto_state = "Enabled" if AUTO_POSTING_ENABLED else "Disabled"
    msg = (
        f"**FH Settlements Bot Info**\n"
        f"- Seen settlements: `{seen_count}`\n"
        f"- Current scan interval: `{CURRENT_INTERVAL_MINUTES}` minutes\n"
        f"- Last scheduled scan: `{last_scan_str}`\n"
        f"- Auto-posting: `{auto_state}`\n"
        f"- Index source (scraped): `{SETTLEMENTS_INDEX_URL}`\n"
        f"- Posts per scan (max): `{MAX_POSTS_PER_RUN}`\n"
        f"- Delay between posts: `{POST_INTERVAL_SECONDS}` seconds"
    )

    await interaction.response.send_message(msg, ephemeral=True)


@slash_oca_info.error
async def slash_oca_info_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message(
            "You need `Manage Server` permissions to use this command.",
            ephemeral=True,
        )


@bot.tree.command(name="oca_stop", description="Stop automatic settlement posting.")
@app_commands.checks.has_permissions(manage_guild=True)
async def slash_oca_stop(interaction: discord.Interaction):
    global AUTO_POSTING_ENABLED
    AUTO_POSTING_ENABLED = False

    await interaction.response.send_message(
        "🛑 Automatic settlement posting has been **stopped**.\n"
        "You can start it again anytime with `/oca_start`.",
        ephemeral=True,
    )


@slash_oca_stop.error
async def slash_oca_stop_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message(
            "You need `Manage Server` permissions to use this command.",
            ephemeral=True,
        )


@bot.tree.command(name="oca_start", description="Resume automatic settlement posting.")
@app_commands.checks.has_permissions(manage_guild=True)
async def slash_oca_start(interaction: discord.Interaction):
    global AUTO_POSTING_ENABLED
    AUTO_POSTING_ENABLED = True

    await interaction.response.send_message(
        "▶️ Automatic settlement posting has been **resumed**.",
        ephemeral=True,
    )


@slash_oca_start.error
async def slash_oca_start_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message(
            "You need `Manage Server` permissions to use this command.",
            ephemeral=True,
        )


# ==========================
# RUN
# ==========================

if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
