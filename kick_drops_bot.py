#!/usr/bin/env python3
"""
Kick Drops -> Telegram notifier

Polls Kick's public drop-campaigns endpoint on a timer and sends you a
Telegram message whenever a new drop campaign shows up.

Setup:
    1. pip install -r requirements.txt
    2. cp .env.example .env   (fill in TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)
    3. python kick_drops_bot.py            # runs forever, checks every N minutes
       python kick_drops_bot.py --once     # single check, then exit (good for testing)

See README.md for full setup instructions and troubleshooting.
"""

import argparse
import html
import json
import logging
import os
import sys
import time
from pathlib import Path

import requests

try:
    import cloudscraper  # handles Cloudflare's JS challenge, which this endpoint uses
except ImportError:
    cloudscraper = None

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # .env is optional if you export the vars yourself

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
POLL_INTERVAL_MINUTES = float(os.environ.get("POLL_INTERVAL_MINUTES", "10"))
KICK_COOKIE = os.environ.get("KICK_COOKIE", "").strip()  # optional Cloudflare fallback
NOTIFY_ON_START = os.environ.get("NOTIFY_ON_START", "true").lower() != "false"

CAMPAIGNS_URL = "https://web.kick.com/api/v1/drops/campaigns"
DROPS_PAGE_URL = "https://kick.com/drops"
SEEN_FILE = Path(__file__).parent / "seen_campaigns.json"

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("kick_drops_bot")


# ---------------------------------------------------------------------------
# Local "seen campaigns" store (so we only alert once per campaign)
# ---------------------------------------------------------------------------

def load_seen_ids() -> set:
    if SEEN_FILE.exists():
        try:
            return set(json.loads(SEEN_FILE.read_text()))
        except (json.JSONDecodeError, OSError):
            log.warning("Could not read %s, starting fresh", SEEN_FILE)
    return set()


def save_seen_ids(ids: set) -> None:
    try:
        SEEN_FILE.write_text(json.dumps(sorted(ids)))
    except OSError as e:
        log.warning("Could not save seen-campaigns file: %s", e)


# ---------------------------------------------------------------------------
# Fetching + parsing Kick's campaigns endpoint
# ---------------------------------------------------------------------------

def _make_session():
    """Cloudflare sits in front of this endpoint, so a plain requests session
    often gets a 403/503. cloudscraper mimics a real browser TLS/JS fingerprint
    and usually gets through. If it's installed we use it; otherwise we fall
    back to plain requests (works sometimes, fails other times)."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json",
    }
    if KICK_COOKIE:
        headers["Cookie"] = KICK_COOKIE

    if cloudscraper is not None:
        session = cloudscraper.create_scraper(browser={"custom": headers["User-Agent"]})
    else:
        session = requests.Session()
    session.headers.update(headers)
    return session


def _pick(d: dict, *keys, default=None):
    """Return the first present, non-None value for any of the candidate keys."""
    for k in keys:
        if isinstance(d, dict) and k in d and d[k] is not None:
            return d[k]
    return default


def _extract_channels(raw: dict) -> list:
    """Kick's campaign objects sometimes list participating channels/streamers.
    Field name isn't documented, so we probe common candidates and shapes."""
    raw_channels = _pick(raw, "channels", "streamers", "participating_channels", "streams", default=[])
    if not isinstance(raw_channels, list):
        return []
    names = []
    for c in raw_channels:
        if isinstance(c, str):
            names.append(c)
        elif isinstance(c, dict):
            name = _pick(c, "username", "slug", "name", "channel_name", "display_name")
            if name:
                names.append(str(name))
    return names


def _normalize_campaign(raw: dict) -> dict:
    """Kick hasn't published a schema for this endpoint, so we probe a few
    likely key names instead of assuming one exact shape."""
    game = _pick(raw, "game", "category", default={})
    game_name = game.get("name") if isinstance(game, dict) else game

    return {
        "id": str(_pick(raw, "id", "campaign_id", "uuid", "slug", default=json.dumps(raw, sort_keys=True))),
        "title": _pick(raw, "name", "title", "campaign_name", default="Untitled drop campaign"),
        "game": game_name or _pick(raw, "game_name", default=None),
        "starts": _pick(raw, "starts_at", "start_date", "started_at", default=None),
        "ends": _pick(raw, "ends_at", "end_date", "ended_at", default=None),
        "status": _pick(raw, "status", "state", default=None),
        "channels": _extract_channels(raw),
    }


def fetch_campaigns() -> list:
    """Fetch and normalize the active drop campaigns list. Raises on failure."""
    session = _make_session()
    resp = session.get(CAMPAIGNS_URL, timeout=20)

    if resp.status_code in (403, 503):
        raise RuntimeError(
            f"Got HTTP {resp.status_code} from Kick, likely blocked by Cloudflare. "
            "See the README troubleshooting section (KICK_COOKIE fallback)."
        )
    resp.raise_for_status()

    try:
        data = resp.json()
    except ValueError as e:
        raise RuntimeError(f"Response wasn't valid JSON (site may have changed): {e}")

    if isinstance(data, list):
        raw_campaigns = data
    elif isinstance(data, dict):
        raw_campaigns = _pick(data, "data", "campaigns", "results", default=[])
    else:
        raw_campaigns = []

    if not isinstance(raw_campaigns, list):
        raw_campaigns = []

    return [_normalize_campaign(c) for c in raw_campaigns if isinstance(c, dict)]


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def send_telegram_message(text: str) -> None:
    if not BOT_TOKEN or not CHAT_ID:
        log.error("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set, can't send message.")
        return
    url = TELEGRAM_API.format(token=BOT_TOKEN)
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    try:
        r = requests.post(url, json=payload, timeout=15)
        if r.status_code != 200:
            log.error("Telegram API error %s: %s", r.status_code, r.text)
    except requests.RequestException as e:
        log.error("Failed to reach Telegram API: %s", e)


def format_campaign_message(c: dict) -> str:
    lines = [f"🎁 <b>New Kick Drop Campaign</b>"]
    lines.append(html.escape(c["title"]))
    if c["game"]:
        lines.append(f"🎮 {html.escape(str(c['game']))}")
    if c["starts"] or c["ends"]:
        window = f"🕒 {c['starts'] or '?'} → {c['ends'] or '?'}"
        lines.append(html.escape(window))
    if c["channels"]:
        chan_list = ", ".join(f"kick.com/{html.escape(ch)}" for ch in c["channels"][:10])
        lines.append(f"📺 <b>Watch here:</b> {chan_list}")
    else:
        lines.append("📺 Channel list wasn't in the data — check the Drops page for participating streams.")
    lines.append(f'<a href="{DROPS_PAGE_URL}">Open Kick Drops</a>')
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

STATUS_FILE = Path(__file__).parent / "last_status.json"


def _write_status(ok: bool, campaign_count: int = 0, error: str = None) -> None:
    status = {
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "fetch_ok": ok,
        "active_campaigns": campaign_count,
        "error": error,
    }
    try:
        STATUS_FILE.write_text(json.dumps(status, indent=2))
    except OSError as e:
        log.warning("Could not write status file: %s", e)


def check_once(seen: set) -> set:
    try:
        campaigns = fetch_campaigns()
    except Exception as e:
        log.error("Fetch failed: %s", e)
        _write_status(ok=False, error=str(e))
        return seen

    new = [c for c in campaigns if c["id"] not in seen]
    log.info("Checked campaigns: %d active, %d new", len(campaigns), len(new))
    _write_status(ok=True, campaign_count=len(campaigns))

    for c in new:
        send_telegram_message(format_campaign_message(c))
        seen.add(c["id"])

    if new:
        save_seen_ids(seen)
    return seen


def main():
    parser = argparse.ArgumentParser(description="Kick Drops -> Telegram notifier")
    parser.add_argument("--once", action="store_true", help="Check once and exit (for testing)")
    parser.add_argument(
        "--test", action="store_true",
        help="Send a one-off confirmation message and exit, without checking for drops",
    )
    args = parser.parse_args()

    if not BOT_TOKEN or not CHAT_ID:
        log.error(
            "Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID. "
            "Copy .env.example to .env and fill them in, then rerun."
        )
        sys.exit(1)

    if args.test:
        send_telegram_message("✅ Test message from your Kick Drops bot — the connection works!")
        log.info("Test message sent.")
        return

    seen = load_seen_ids()
    log.info("Starting Kick Drops bot. Tracking %d previously-seen campaign(s).", len(seen))

    if args.once:
        check_once(seen)
        return

    if NOTIFY_ON_START:
        send_telegram_message("✅ Kick Drops bot is up and watching for new campaigns.")

    interval_seconds = max(POLL_INTERVAL_MINUTES, 1) * 60
    while True:
        seen = check_once(seen)
        time.sleep(interval_seconds)


if __name__ == "__main__":
    main()
