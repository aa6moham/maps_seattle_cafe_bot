# MAPS Cafe Bot

Telegram bot for managing cafe orders at the masjid.

## Features

- Browse menu items via `/order` command
- Place orders with confirmation flow
- Staff can view and manage pending orders
- Automatic customer notification when order is ready

## Setup

### 1. Create Telegram Bot

1. Message [@BotFather](https://t.me/botfather) on Telegram
2. Use `/newbot` to create a new bot
3. Save the bot token

### 2. Google Sheets Setup

Ensure your spreadsheet has a `menu` sheet with columns:
- `item` - Name of the menu item
- `price` - Price (numeric)
- `gender` - Optional gender restriction ("Brothers", "Sisters", or leave empty for all)

The bot will automatically create an `orders` sheet when the first order is placed.

### 3. Service Account Credentials

Copy your Google service account `creds.json` to the `creds/` directory.

### 4. Environment Variables

Set these environment variables:
- `BOT_TOKEN` - Telegram bot token from BotFather
- `BOT_USERNAME` - Bot username (optional)
- `SPREADSHEET_ID` - Google Sheets ID (optional, defaults to shared spreadsheet)

### 5. Deploy to Fly.io

```bash
# Login to fly.io
fly auth login

# Create app (first time only)
fly apps create maps-cafe-bot

# Set secrets
fly secrets set BOT_TOKEN="your-bot-token-here"

# Deploy
fly deploy
```

## Commands

### User Commands
- `/start` - Welcome message
- `/help` - Show available commands
- `/order` - Browse menu and place an order (DM only)

### Staff Commands
- `/orders` - View pending orders with buttons to mark as ready

## Order Flow

1. User sends `/order` in DM
2. Bot displays menu items as buttons
3. User taps an item
4. Bot asks for confirmation (Yes/No)
5. If Yes: Order is enqueued, user will receive DM when ready
6. If No: Order cancelled, user can start over

## Local Development

```bash
# Install dependencies
pip install -r requirements.txt

# Set environment variables
export BOT_TOKEN="your-bot-token"

# Copy credentials
cp /path/to/creds.json creds/creds.json

# Run the bot
python main.py
```
