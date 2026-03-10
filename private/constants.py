"""Telegram Bot and Google Sheets credentials.

In production (fly.io), these are loaded from environment variables.
For local development, fall back to hardcoded values.
"""

import os

# Telegram Bot Credentials (required - loaded from environment)
BOT_TOKEN = os.environ.get("BOT_TOKEN")
BOT_USERNAME = os.environ.get("BOT_USERNAME", "MAPS Cafe Bot")

# Google Sheets Spreadsheet ID (same as volunteer bot)
SPREADSHEET_ID = os.environ.get(
    "SPREADSHEET_ID", "17bJUw0UcMe4olGqa5rUl8ZlCejMKA55tRQb452sMazU"
)
