# Kick Drops Bot

Automatically checks Kick's drop campaigns roughly every 15 minutes and
posts new ones to a Telegram group — via GitHub Actions, so nothing needs
to stay running on any device.

- Logic: `kick_drops_bot.py`
- Schedule: `.github/workflows/check-drops.yml`
- To trigger a manual check: **Actions tab → "Check Kick Drops" → Run workflow**
  (tick "Send a test message" to just verify the Telegram connection)

Secrets (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`) are stored under
Settings → Secrets and variables → Actions, and are never shown in this code.
