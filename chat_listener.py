#!/usr/bin/env python3
"""
Kick live-chat watcher -> Telegram alert

Connects to a Kick channel's live chat over Kick's (undocumented) Pusher
websocket and sends a Telegram alert whenever a chat message matches one
of the configured keywords (e.g. "stake drop").

Designed to run inside a single GitHub Actions job. Since GitHub kills any
job after 6 hours, this script tracks its own runtime budget and — before
that limit hits — asks GitHub to start a fresh run of the same workflow,
then exits. Combined with a concurrency group in the workflow file, the
new run queues up and starts the moment this one ends, giving continuous
coverage for free.
"""

import html
import json
import logging
import os
import sys
import threading
import time

import requests
import websocket  # pip: websocket-client

try:
    import cloudscraper
except ImportError:
    cloudscraper = None

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("kick_chat_listener")

CHANNEL_SLUG = os.environ.get("KICK_CHANNEL", "starladder")
KEYWORDS = [k.strip().lower() for k in os.environ.get("ALERT_KEYWORDS", "stake drop").split(",") if k.strip()]
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
GH_TOKEN = os.environ.get("GH_TOKEN", "")
GH_REPO = os.environ.get("GITHUB_REPOSITORY", "")
WORKFLOW_FILE = os.environ.get("WORKFLOW_FILE", "listen-chat.yml")
MAX_RUNTIME_SECONDS = int(os.environ.get("MAX_RUNTIME_SECONDS", str(5 * 3600 + 30 * 60)))  # 5h30m default
ALERT_COOLDOWN_SECONDS = 10  # avoid spamming if several matching messages land at once

PUSHER_WS_URL = "wss://ws-us2.pusher.com/app/32cbd69e4b950bf97679?protocol=7&client=js&version=8.4.0-rc2&flash=false"

start_time = time.time()
stop_flag = threading.Event()
next_run_triggered = threading.Event()
last_alert_time = 0.0


def send_telegram_message(text: str) -> None:
    if not BOT_TOKEN or not CHAT_ID:
        log.error("Missing Telegram credentials, cannot send alert")
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(
            url,
            json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=15,
        )
        if r.status_code != 200:
            log.error("Telegram error %s: %s", r.status_code, r.text)
    except requests.RequestException as e:
        log.error("Telegram send failed: %s", e)


def get_chatroom_id(slug: str) -> int:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        )
    }
    session = cloudscraper.create_scraper() if cloudscraper else requests.Session()
    session.headers.update(headers)
    r = session.get(f"https://kick.com/api/v2/channels/{slug}", timeout=20)
    r.raise_for_status()
    data = r.json()
    chatroom_id = (data.get("chatroom") or {}).get("id")
    if not chatroom_id:
        raise RuntimeError(f"Could not find chatroom id for '{slug}' in response")
    return chatroom_id


def trigger_next_run() -> None:
    """Ask GitHub to queue a fresh run of this workflow before this one is killed."""
    if next_run_triggered.is_set():
        return
    next_run_triggered.set()
    if not GH_TOKEN or not GH_REPO:
        log.error("Can't self re-trigger: missing GH_TOKEN or GITHUB_REPOSITORY")
        return
    url = f"https://api.github.com/repos/{GH_REPO}/actions/workflows/{WORKFLOW_FILE}/dispatches"
    headers = {"Authorization": f"token {GH_TOKEN}", "Accept": "application/vnd.github+json"}
    try:
        r = requests.post(url, headers=headers, json={"ref": "main"}, timeout=20)
        log.info("Re-trigger dispatch status: %s", r.status_code)
        if r.status_code >= 300:
            log.error("Re-trigger response: %s", r.text[:400])
    except requests.RequestException as e:
        log.error("Re-trigger failed: %s", e)


def extract_message(outer: dict):
    """Kick's chat payloads aren't documented; this handles the shapes seen in the wild."""
    raw_data = outer.get("data")
    if isinstance(raw_data, str):
        try:
            inner = json.loads(raw_data)
        except json.JSONDecodeError:
            inner = {}
    else:
        inner = raw_data or {}

    msg_obj = inner.get("message")
    if isinstance(msg_obj, dict):
        content = msg_obj.get("message") or msg_obj.get("content") or ""
    else:
        content = inner.get("content") or ""

    user_obj = inner.get("user") or inner.get("sender") or {}
    username = user_obj.get("username", "unknown") if isinstance(user_obj, dict) else "unknown"
    return username, content


def check_message(username: str, content: str) -> None:
    global last_alert_time
    lower = content.lower()
    for kw in KEYWORDS:
        if kw in lower:
            now = time.time()
            if now - last_alert_time < ALERT_COOLDOWN_SECONDS:
                log.info("Match found but within cooldown, skipping duplicate alert")
                return
            last_alert_time = now
            msg = (
                "🔥 <b>Stake Drop Alert</b>\n"
                f"📺 Channel: {html.escape(CHANNEL_SLUG)}\n"
                f"👤 {html.escape(username)}\n"
                f"💬 {html.escape(content)}\n"
                f'<a href="https://kick.com/{html.escape(CHANNEL_SLUG)}">Join the stream</a>'
            )
            send_telegram_message(msg)
            log.info("ALERT sent (match: %r) from %s: %s", kw, username, content)
            return


def on_message(ws, message):
    try:
        outer = json.loads(message)
    except json.JSONDecodeError:
        return

    event = outer.get("event", "")
    if event in ("App\\Events\\ChatMessageEvent", "App\\Events\\ChatMessageSentEvent"):
        username, content = extract_message(outer)
        if content:
            check_message(username, content)

    if time.time() - start_time > MAX_RUNTIME_SECONDS:
        log.info("Runtime budget reached, handing off to a fresh run")
        trigger_next_run()
        stop_flag.set()
        ws.close()


def on_open(ws, chatroom_id):
    sub = {"event": "pusher:subscribe", "data": {"auth": "", "channel": f"chatrooms.{chatroom_id}.v2"}}
    ws.send(json.dumps(sub))
    log.info("Subscribed to chatrooms.%s.v2", chatroom_id)


def budget_watchdog():
    """Backup timer in case chat is quiet near the runtime limit (on_message wouldn't fire)."""
    while not stop_flag.is_set():
        time.sleep(30)
        if time.time() - start_time > MAX_RUNTIME_SECONDS:
            log.info("Watchdog: runtime budget reached, handing off")
            trigger_next_run()
            stop_flag.set()
            time.sleep(3)
            os._exit(0)


def main():
    if not BOT_TOKEN or not CHAT_ID:
        log.error("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID")
        sys.exit(1)

    log.info("Channel: %s | keywords: %s | runtime budget: %ss", CHANNEL_SLUG, KEYWORDS, MAX_RUNTIME_SECONDS)
    chatroom_id = get_chatroom_id(CHANNEL_SLUG)
    log.info("Resolved chatroom id: %s", chatroom_id)

    threading.Thread(target=budget_watchdog, daemon=True).start()

    while not stop_flag.is_set():
        try:
            ws = websocket.WebSocketApp(
                PUSHER_WS_URL,
                on_open=lambda w: on_open(w, chatroom_id),
                on_message=on_message,
            )
            ws.run_forever(ping_interval=30, ping_timeout=10)
        except Exception as e:
            log.error("Websocket error: %s", e)
        if not stop_flag.is_set():
            log.info("Disconnected, reconnecting in 5s...")
            time.sleep(5)

    log.info("Exiting cleanly.")


if __name__ == "__main__":
    main()
