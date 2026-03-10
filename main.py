"""MAPS Masjid Cafe Bot - Telegram bot for managing cafe orders.

This bot allows users to browse menu items and place orders via DM.
Orders are tracked in Google Sheets and staff can manage them.
"""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest, Forbidden
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

from google_sheets_operations import (
    get_menu_items,
    create_order,
    get_pending_orders,
    mark_order_ready,
)
from logger import setup_logger
from private.constants import BOT_TOKEN, BOT_USERNAME

# Setup logging
logger = setup_logger(__name__)


# ============================================================================
# COMMAND HANDLERS
# ============================================================================


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command."""
    user = update.effective_user
    logger.info(f"User {user.id} ({user.full_name}) started the bot")

    await update.message.reply_text(
        f"Assalamu Alaikum {user.first_name}! 👋\n\n"
        f"Welcome to the *MAPS Cafe Bot*.\n\n"
        f"Use /order to browse our menu and place an order.\n"
        f"Use /help to see all available commands.",
        parse_mode="Markdown",
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /help command."""
    await update.message.reply_text(
        "☕ *MAPS Cafe Bot Commands*\n\n"
        "/order - Browse menu and place an order\n"
        "/help - Show this help message\n\n"
        "_Place orders via DM to the bot._",
        parse_mode="Markdown",
    )


async def order_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /order command - Display menu items as buttons."""
    user = update.effective_user
    chat = update.effective_chat

    # Only allow orders in DM
    if chat.type != "private":
        await update.message.reply_text(
            "📩 Please DM me directly to place an order!\n"
            f"Click here: @{BOT_USERNAME}"
        )
        return

    logger.info(f"User {user.id} ({user.full_name}) requested menu")

    # Fetch menu items from Google Sheet
    menu_items = get_menu_items()

    if not menu_items:
        await update.message.reply_text(
            "😔 Sorry, the menu is currently unavailable. Please try again later."
        )
        return

    # Build keyboard with menu items
    keyboard = []
    for item in menu_items:
        item_id = item["item_id"]
        name = item["item"]
        price = item["price"]
        gender = item["gender"]

        # Format: "Item - $price - gender only"
        if gender and gender.lower() not in ("all", "both", ""):
            button_text = f"{name} - ${price:.2f} - {gender} only"
        else:
            button_text = f"{name} - ${price:.2f}"

        keyboard.append(
            [InlineKeyboardButton(button_text, callback_data=f"menu:{item_id}")]
        )

    await update.message.reply_text(
        "☕ *MAPS Cafe Menu*\n\n"
        "Tap an item to order:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )


# ============================================================================
# CALLBACK HANDLERS
# ============================================================================


async def handle_menu_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle menu item selection - Show confirmation."""
    query = update.callback_query
    await query.answer()

    item_id = query.data.split(":")[1]

    # Fetch the specific menu item
    menu_items = get_menu_items()
    item = next((i for i in menu_items if str(i["item_id"]) == str(item_id)), None)

    if not item:
        await query.edit_message_text("❌ Item not found. Please try /order again.")
        return

    name = item["item"]
    price = item["price"]
    gender = item["gender"]

    # Store selection in user context for confirmation
    context.user_data["pending_order"] = {
        "item_id": item_id,
        "item": name,
        "price": price,
        "gender": gender,
    }

    # Format confirmation message
    if gender and gender.lower() not in ("all", "both", ""):
        item_desc = f"*{name}* for *${price:.2f}* ({gender} only)"
    else:
        item_desc = f"*{name}* for *${price:.2f}*"

    # Confirmation buttons
    keyboard = [
        [
            InlineKeyboardButton("✅ Yes, place order", callback_data="confirm:yes"),
            InlineKeyboardButton("❌ No, cancel", callback_data="confirm:no"),
        ]
    ]

    await query.edit_message_text(
        f"🛒 *Confirm Order*\n\n"
        f"You selected: {item_desc}\n\n"
        f"Are you sure you want to place this order?",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )


async def handle_order_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle order confirmation (YES/NO)."""
    query = update.callback_query
    await query.answer()

    user = query.from_user
    choice = query.data.split(":")[1]

    pending_order = context.user_data.get("pending_order")

    if choice == "yes":
        if not pending_order:
            await query.edit_message_text(
                "❌ Order expired. Please start again with /order."
            )
            return

        # Create the order in Google Sheets
        order_id = create_order(
            telegram_id=user.id,
            telegram_name=user.full_name,
            item=pending_order["item"],
            price=pending_order["price"],
        )

        if order_id:
            logger.info(
                f"Order {order_id} created for user {user.id}: {pending_order['item']}"
            )

            await query.edit_message_text(
                f"✅ *Order Placed!*\n\n"
                f"Your order for *{pending_order['item']}* has been enqueued.\n\n"
                f"📩 You will receive a DM once your order is ready for pickup.\n\n"
                f"Order ID: `{order_id}`",
                parse_mode="Markdown",
            )
        else:
            logger.error(f"Failed to create order for user {user.id}")
            await query.edit_message_text(
                "❌ Sorry, there was an error placing your order. "
                "Please try again with /order."
            )

        # Clear pending order
        context.user_data.pop("pending_order", None)

    else:  # choice == "no"
        # Clear pending order
        context.user_data.pop("pending_order", None)

        await query.edit_message_text(
            "❌ *Order Cancelled*\n\n"
            "Your order was not placed.\n\n"
            "Use /order to start a new order.",
            parse_mode="Markdown",
        )


# ============================================================================
# ADMIN/STAFF COMMANDS
# ============================================================================


async def orders_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /orders command - Show pending orders (staff only)."""
    user = update.effective_user

    # TODO: Add staff verification via Google Sheet
    pending = get_pending_orders()

    if not pending:
        await update.message.reply_text("📋 No pending orders.")
        return

    message_lines = ["📋 *Pending Orders*\n"]

    for order in pending:
        message_lines.append(
            f"• `{order['order_id']}` - {order['item']} - {order['telegram_name']}"
        )

    # Add buttons to mark orders as ready
    keyboard = [
        [
            InlineKeyboardButton(
                f"✅ Ready: {order['order_id'][:8]}",
                callback_data=f"ready:{order['order_id']}",
            )
        ]
        for order in pending[:10]  # Limit to 10 buttons
    ]

    await update.message.reply_text(
        "\n".join(message_lines),
        reply_markup=InlineKeyboardMarkup(keyboard) if keyboard else None,
        parse_mode="Markdown",
    )


async def handle_order_ready(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle marking an order as ready."""
    query = update.callback_query
    await query.answer()

    order_id = query.data.split(":")[1]

    # Mark order as ready and get customer info
    result = mark_order_ready(order_id)

    if result:
        telegram_id = result["telegram_id"]
        item = result["item"]

        # Notify customer
        try:
            await context.bot.send_message(
                chat_id=telegram_id,
                text=(
                    f"🎉 *Your order is ready!*\n\n"
                    f"Your *{item}* is ready for pickup.\n\n"
                    f"Please come to the cafe counter. JazakAllah Khair!"
                ),
                parse_mode="Markdown",
            )
            await query.edit_message_text(
                f"✅ Order `{order_id}` marked as ready. Customer notified!",
                parse_mode="Markdown",
            )
        except (BadRequest, Forbidden) as e:
            logger.warning(f"Could not notify user {telegram_id}: {e}")
            await query.edit_message_text(
                f"✅ Order `{order_id}` marked as ready.\n"
                f"⚠️ Could not notify customer (they may have blocked the bot).",
                parse_mode="Markdown",
            )
    else:
        await query.edit_message_text(f"❌ Order `{order_id}` not found.")


# ============================================================================
# MAIN
# ============================================================================


def main():
    """Start the bot."""
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN environment variable not set!")
        return

    logger.info(f"Starting {BOT_USERNAME}...")

    # Create application
    app = Application.builder().token(BOT_TOKEN).build()

    # Register command handlers
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("order", order_command))
    app.add_handler(CommandHandler("orders", orders_command))

    # Register callback handlers
    app.add_handler(CallbackQueryHandler(handle_menu_selection, pattern=r"^menu:"))
    app.add_handler(
        CallbackQueryHandler(handle_order_confirmation, pattern=r"^confirm:")
    )
    app.add_handler(CallbackQueryHandler(handle_order_ready, pattern=r"^ready:"))

    logger.info(f"{BOT_USERNAME} is now running...")
    app.run_polling()


if __name__ == "__main__":
    main()
