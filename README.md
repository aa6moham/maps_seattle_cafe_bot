# MAPS Cafe Bot

Telegram bot for managing cafe orders at the masjid with gender-based sections (Brothers/Sisters).

## Features

- Browse menu items via `/order` command
- Place orders with optional special instructions (decaf, extra syrup, etc.)
- Gender-based order routing (Brothers/Sisters sections)
- Staff can view and manage pending orders
- Automatic customer notification when order is ready
- Admin controls for opening/closing cafe sections
- Orders cache with periodic sync to prevent Google Sheets API rate limiting

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
- `description` - Optional description of the item

The bot will automatically create these sheets when needed:
- `orders` - Order records
- `cafe_registered` - Registered chat/topic mappings
- `admins` - Admin user IDs

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
- `/mystatus` - Check your pending and recent orders (DM only)

### Staff Commands
- `/orders` - View pending orders with buttons to mark as ready/complete/deny

### Admin Commands
- `/register` - Register the current group chat for cafe notifications (creates Brothers/Sisters forum topics)
- `/deregister` - Deregister the current group chat from cafe notifications
- `/open` - Open cafe for both Brothers and Sisters orders
- `/open_brothers` - Open cafe for Brothers orders only
- `/open_sisters` - Open cafe for Sisters orders only
- `/close` - Close cafe for all orders
- `/close_brothers` - Close cafe for Brothers orders only
- `/close_sisters` - Close cafe for Sisters orders only
- `/status` - Show current cafe open/closed status

> **Note:** Admin commands require the user's Telegram ID to be listed in the `admins` sheet. Non-admins attempting admin commands will see a warning that auto-deletes after 5 seconds.

## Order Flow

1. User sends `/order` in DM
2. Bot checks if cafe is open for orders
3. Bot displays menu items as buttons (filtered by open sections)
4. User taps an item
5. Bot asks for optional special instructions (Add Instructions / Skip)
6. User can add free text instructions or skip
7. Bot asks for confirmation with order summary
8. If Yes: Order is enqueued, user will receive DM when ready
9. If No: Order cancelled, user can start over

## Architecture

### Orders Cache
The bot uses an in-memory cache (`OrdersCache`) with periodic sync to Google Sheets to:
- Prevent API rate limiting under high load (40+ concurrent orders)
- Batch writes for better performance
- Flush pending writes on shutdown to prevent data loss

### Cafe State Management
- In-memory state tracks whether Brothers/Sisters sections are open
- Admins control state via `/open*` and `/close*` commands
- Orders are automatically denied if the cafe is closed

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
