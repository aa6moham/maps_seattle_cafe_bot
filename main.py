"""MAPS Masjid Cafe Bot - Telegram bot for managing cafe orders.

This bot allows users to browse menu items and place orders via DM.
Orders are tracked in Google Sheets and staff can manage them.
"""

import asyncio

import asyncio

from google_sheets_operations import (
    close_cafe,
    create_order,
    deregister_cafe_chat,
    get_all_registered_cafe_chats,
    get_cafe_state,
    get_menu_items,
    get_orders_for_user,
    get_pending_orders,
    get_registered_cafe_chat,
    is_admin,
    mark_order_completed,
    mark_order_denied,
    mark_order_ready,
    open_cafe,
    register_cafe_chat,
    unified_sync_processor,
)
from logger import setup_logger
from private.constants import BOT_TOKEN, BOT_USERNAME
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest, Forbidden
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# Setup logging
logger = setup_logger(__name__)

# Store background task references for proper cleanup
background_tasks: list = []


# ============================================================================
# COMMAND HANDLERS
# ============================================================================


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command."""
    user = update.effective_user
    logger.info(f"User {user.id} ({user.full_name}) started the bot")

    await update.message.reply_text(
        f"Assalamu Alaikum {user.first_name}! 🙏\n\n"
        f"Welcome to the *MAPS Masjid Cafe* ☕🕌\n\n"
        f"All proceeds go directly to support our masjid. "
        f"May Allah reward you for your contributions!\n\n"
        f"Use /order to browse our menu and place an order.\n"
        f"Use /help to see all available commands.\n\n"
        f"_JazakAllah Khair for your support!_",
        parse_mode="Markdown",
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /help command."""
    await update.message.reply_text(
        "🕌 *MAPS Masjid Cafe Commands*\n\n"
        "/order - Browse menu and place an order\n"
        "/mystatus - Check your pending orders\n"
        "/help - Show this help message\n\n"
        "_Place orders via DM to the bot._\n\n"
        "All proceeds support our masjid. JazakAllah Khair! 🤲",
        parse_mode="Markdown",
    )


async def mystatus_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /mystatus command - Show user's order status."""
    user = update.effective_user
    chat = update.effective_chat

    # Only allow in DM - silently ignore in group chats
    if chat.type != "private":
        return

    # Get user's orders
    user_orders = get_orders_for_user(user.id)

    if not user_orders:
        await update.message.reply_text(
            "📋 *Your Orders*\n\n"
            "You don't have any orders yet.\n\n"
            "Use /order to browse our menu and place an order!",
            parse_mode="Markdown",
        )
        return

    # Separate pending/ready orders from completed/denied
    active_orders = [o for o in user_orders if o.get("status", "").lower() in ("pending", "ready")]
    recent_completed = [o for o in user_orders if o.get("status", "").lower() in ("completed", "denied")][:5]

    # Build message
    message_parts = ["📋 *Your Orders*\n"]

    if active_orders:
        message_parts.append("\n*🔄 Active Orders:*")
        for order in active_orders:
            status = order.get("status", "pending").lower()
            status_emoji = "⏳" if status == "pending" else "✅"
            status_text = "Pending" if status == "pending" else "Ready for pickup!"
            item = order.get("item", "Unknown")
            order_id = order.get("order_id", "N/A")
            notes = order.get("notes", "")

            order_text = f"\n{status_emoji} *{item}* (#{order_id})\n   Status: {status_text}"
            if notes:
                order_text += f"\n   Notes: _{notes}_"
            message_parts.append(order_text)
    else:
        message_parts.append("\n_No active orders._")

    if recent_completed:
        message_parts.append("\n\n*📜 Recent History:*")
        for order in recent_completed[:3]:  # Show last 3
            status = order.get("status", "").lower()
            status_emoji = "✓" if status == "completed" else "✗"
            item = order.get("item", "Unknown")
            message_parts.append(f"\n{status_emoji} {item}")

    message_parts.append("\n\n_Use /order to place a new order._")

    await update.message.reply_text(
        "".join(message_parts),
        parse_mode="Markdown",
    )


async def order_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /order command - Display menu items as buttons."""
    user = update.effective_user
    chat = update.effective_chat

    # Only allow orders in DM - silently ignore in group chats
    if chat.type != "private":
        return

    logger.info(f"User {user.id} ({user.full_name}) requested menu")

    # Check if cafe is open
    cafe_state = get_cafe_state()
    brothers_open = cafe_state["brothers"]
    sisters_open = cafe_state["sisters"]

    if not brothers_open and not sisters_open:
        await update.message.reply_text(
            "🚫 *Cafe is Currently Closed*\n\n"
            "We are not accepting orders at this time.\n\n"
            "Please check back later, In Shaa Allah!\n\n"
            "_JazakAllah Khair for your patience._",
            parse_mode="Markdown",
        )
        return

    # Fetch menu items from Google Sheet
    menu_items = get_menu_items()

    if not menu_items:
        await update.message.reply_text(
            "😔 Sorry, the menu is currently unavailable. Please try again later."
        )
        return

    # Group items by gender
    brothers_items = []
    sisters_items = []
    all_items = []

    for item in menu_items:
        gender = item["gender"].lower() if item["gender"] else ""
        if gender in ("brothers", "brother"):
            brothers_items.append(item)
        elif gender in ("sisters", "sister"):
            sisters_items.append(item)
        elif gender == "both":
            # Items available for both appear in both sections
            brothers_items.append(item)
            sisters_items.append(item)
        else:
            all_items.append(item)

    # Build keyboard with menu items grouped by gender
    # Callback data includes section so we know pickup location
    # Only show sections that are open
    keyboard = []

    if brothers_items and brothers_open:
        keyboard.append(
            [InlineKeyboardButton("🧔 Brothers", callback_data="header:brothers")]
        )
        for item in brothers_items:
            button_text = f"{item['item']} - ${item['price']:.2f}"
            keyboard.append(
                [
                    InlineKeyboardButton(
                        button_text, callback_data=f"menu:{item['item_id']}:brothers"
                    )
                ]
            )

    if sisters_items and sisters_open:
        keyboard.append(
            [InlineKeyboardButton("🧕 Sisters", callback_data="header:sisters")]
        )
        for item in sisters_items:
            button_text = f"{item['item']} - ${item['price']:.2f}"
            keyboard.append(
                [
                    InlineKeyboardButton(
                        button_text, callback_data=f"menu:{item['item_id']}:sisters"
                    )
                ]
            )

    if all_items and (brothers_open or sisters_open):
        keyboard.append(
            [InlineKeyboardButton("🤲 Everyone", callback_data="header:all")]
        )
        for item in all_items:
            button_text = f"{item['item']} - ${item['price']:.2f}"
            keyboard.append(
                [
                    InlineKeyboardButton(
                        button_text, callback_data=f"menu:{item['item_id']}:general"
                    )
                ]
            )

    # Build status message for partial opening
    if brothers_open and sisters_open:
        status_msg = ""
    elif brothers_open:
        status_msg = "\n⚠️ _Sisters side is currently closed._\n"
    elif sisters_open:
        status_msg = "\n⚠️ _Brothers side is currently closed._\n"
    else:
        status_msg = ""

    await update.message.reply_text(
        "🕌 *MAPS Masjid Cafe Menu*\n\n"
        "_All proceeds go to support our masjid!_\n"
        f"{status_msg}\n"
        "Tap an item to order:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )


# ============================================================================
# CALLBACK HANDLERS
# ============================================================================


async def handle_header_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle header button clicks (no-op, just acknowledge)."""
    query = update.callback_query
    await query.answer("This is a section header")


async def handle_menu_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle menu item selection - Show item details and options."""
    query = update.callback_query
    await query.answer()

    # Parse callback data: menu:{item_id}:{section}
    parts = query.data.split(":")
    item_id = parts[1]
    section = parts[2] if len(parts) > 2 else "general"

    # Fetch the specific menu item
    menu_items = get_menu_items()
    item = next((i for i in menu_items if str(i["item_id"]) == str(item_id)), None)

    if not item:
        await query.edit_message_text("❌ Item not found. Please try /order again.")
        return

    name = item["item"]
    price = item["price"]
    description = item.get("description", "")

    # Store selection in user context for confirmation
    # Use section (from button clicked) instead of item's gender for pickup location
    context.user_data["pending_order"] = {
        "item_id": item_id,
        "item": name,
        "price": price,
        "section": section,
        "description": description,
        "notes": "",  # Will be filled if user adds special instructions
    }
    # Clear any waiting state
    context.user_data.pop("awaiting_instructions", None)

    # Format pickup location based on section clicked
    if section == "sisters":
        pickup_note = "🧕 _Pickup: Kitchen area (sisters section)_"
    elif section == "brothers":
        pickup_note = "🧔 _Pickup: Upstairs kitchen area (brothers section)_"
    else:
        pickup_note = ""

    # Build item details message
    item_details = f"*{name}*\n💰 *Price:* ${price:.2f}"
    if description:
        item_details += f"\n\n📝 _{description}_"
    if pickup_note:
        item_details += f"\n\n{pickup_note}"

    # Buttons for order flow
    keyboard = [
        [
            InlineKeyboardButton(
                "📝 Add Special Instructions", callback_data="instructions:add"
            )
        ],
        [
            InlineKeyboardButton("✅ Place Order", callback_data="confirm:yes"),
            InlineKeyboardButton("❌ Cancel", callback_data="confirm:no"),
        ],
    ]

    await query.edit_message_text(
        f"🛒 *Order Details*\n\n"
        f"{item_details}\n\n"
        f"Would you like to add special instructions (e.g., decaf, extra syrup)?",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )


async def handle_instructions_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle 'Add Special Instructions' button click."""
    query = update.callback_query
    await query.answer()

    pending_order = context.user_data.get("pending_order")
    if not pending_order:
        await query.edit_message_text("❌ Order expired. Please start again with /order.")
        return

    # Set flag to await text input
    context.user_data["awaiting_instructions"] = True

    # Show current order with instruction prompt
    keyboard = [
        [InlineKeyboardButton("⏭️ Skip (No Instructions)", callback_data="instructions:skip")],
        [InlineKeyboardButton("❌ Cancel Order", callback_data="confirm:no")],
    ]

    await query.edit_message_text(
        f"📝 *Add Special Instructions*\n\n"
        f"Order: *{pending_order['item']}*\n\n"
        f"Please type your special instructions below:\n"
        f"_(e.g., \"decaf\", \"extra pump vanilla\", \"oat milk\")_\n\n"
        f"Or tap Skip if you don't need any.",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )


async def handle_instructions_skip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle 'Skip Instructions' button click."""
    query = update.callback_query
    await query.answer()

    # Clear waiting state
    context.user_data.pop("awaiting_instructions", None)

    pending_order = context.user_data.get("pending_order")
    if not pending_order:
        await query.edit_message_text("❌ Order expired. Please start again with /order.")
        return

    # Show final confirmation
    await show_final_confirmation(query, context, pending_order)


async def handle_special_instructions_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    """Handle text input for special instructions."""
    # Only process if we're awaiting instructions
    if not context.user_data.get("awaiting_instructions"):
        return

    pending_order = context.user_data.get("pending_order")
    if not pending_order:
        await update.message.reply_text("❌ Order expired. Please start again with /order.")
        context.user_data.pop("awaiting_instructions", None)
        return

    # Store the instructions
    notes = update.message.text.strip()
    pending_order["notes"] = notes
    context.user_data["pending_order"] = pending_order
    context.user_data.pop("awaiting_instructions", None)

    # Build confirmation message
    section = pending_order.get("section", "general")
    if section == "sisters":
        pickup_note = "🧕 _Pickup: Kitchen area (sisters section)_"
    elif section == "brothers":
        pickup_note = "🧔 _Pickup: Upstairs kitchen area (brothers section)_"
    else:
        pickup_note = ""

    description = pending_order.get("description", "")
    item_details = f"*{pending_order['item']}*\n💰 *Price:* ${pending_order['price']:.2f}"
    if description:
        item_details += f"\n📝 _{description}_"
    if pickup_note:
        item_details += f"\n\n{pickup_note}"

    keyboard = [
        [
            InlineKeyboardButton("✅ Confirm Order", callback_data="confirm:yes"),
            InlineKeyboardButton("❌ Cancel", callback_data="confirm:no"),
        ],
        [
            InlineKeyboardButton("✏️ Edit Instructions", callback_data="instructions:add")
        ],
    ]

    await update.message.reply_text(
        f"🛒 *Final Order Confirmation*\n\n"
        f"{item_details}\n\n"
        f"📋 *Special Instructions:*\n_{notes}_\n\n"
        f"Ready to place your order?",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )


async def show_final_confirmation(query, context, pending_order):
    """Show final confirmation screen with all order details."""
    section = pending_order.get("section", "general")
    if section == "sisters":
        pickup_note = "🧕 _Pickup: Kitchen area (sisters section)_"
    elif section == "brothers":
        pickup_note = "🧔 _Pickup: Upstairs kitchen area (brothers section)_"
    else:
        pickup_note = ""

    description = pending_order.get("description", "")
    notes = pending_order.get("notes", "")

    item_details = f"*{pending_order['item']}*\n💰 *Price:* ${pending_order['price']:.2f}"
    if description:
        item_details += f"\n📝 _{description}_"
    if pickup_note:
        item_details += f"\n\n{pickup_note}"

    keyboard = [
        [
            InlineKeyboardButton("✅ Confirm Order", callback_data="confirm:yes"),
            InlineKeyboardButton("❌ Cancel", callback_data="confirm:no"),
        ],
        [
            InlineKeyboardButton("📝 Add Instructions", callback_data="instructions:add")
        ],
    ]

    message = f"🛒 *Final Order Confirmation*\n\n{item_details}"
    if notes:
        message += f"\n\n📋 *Special Instructions:*\n_{notes}_"
    message += "\n\nReady to place your order?"

    await query.edit_message_text(
        message,
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

    # Clear any awaiting instructions state
    context.user_data.pop("awaiting_instructions", None)

    if choice == "yes":
        if not pending_order:
            await query.edit_message_text(
                "❌ Order expired. Please start again with /order."
            )
            return

        # Use section (from button clicked) for gender/pickup determination
        section = pending_order.get("section", "general")
        notes = pending_order.get("notes", "")

        # Create the order in Google Sheets
        order_id = create_order(
            telegram_id=user.id,
            telegram_name=user.full_name,
            item=pending_order["item"],
            price=pending_order["price"],
            gender=section,  # Store section as gender for pickup routing
            notes=notes,
        )

        if order_id:
            logger.info(
                f"Order {order_id} created for user {user.id}: {pending_order['item']}"
            )

            # Determine pickup location based on section clicked
            if section == "sisters":
                pickup_location = "🧕 *Pickup Location:* Kitchen area (sisters section)"
            elif section == "brothers":
                pickup_location = (
                    "🧔 *Pickup Location:* Upstairs kitchen area (brothers section)"
                )
            else:
                pickup_location = "📍 *Pickup Location:* Cafe counter"

            # Build confirmation message
            confirmation_msg = (
                f"✅ *Order Placed!*\n\n"
                f"Your order for *{pending_order['item']}* has been enqueued.\n\n"
                f"{pickup_location}\n\n"
            )
            if notes:
                confirmation_msg += f"📋 *Special Instructions:* _{notes}_\n\n"
            confirmation_msg += (
                f"📩 You will receive a DM once your order is ready, In Shaa Allah.\n\n"
                f"Order ID: `{order_id}`\n\n"
                f"🤲 *JazakAllah Khair!* Your contribution supports our masjid. "
                f"May Allah bless you and your family!"
            )

            await query.edit_message_text(confirmation_msg, parse_mode="Markdown")

            # Notify staff in registered chats
            await notify_staff_of_order(
                bot=context.bot,
                order_id=order_id,
                telegram_id=user.id,
                telegram_name=user.full_name,
                item=pending_order["item"],
                price=pending_order["price"],
                gender=section,  # Use section for routing to correct topic
                description=pending_order.get("description", ""),
                notes=notes,
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
            "Use /order to start a new order.\n\n"
            "_JazakAllah Khair for considering our masjid cafe!_ 🕌",
            parse_mode="Markdown",
        )


# ============================================================================
# ADMIN/STAFF COMMANDS
# ============================================================================


async def register_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /register command - Register chat for cafe notifications (admin only)."""
    user = update.effective_user
    chat = update.effective_chat

    # Only allow in group chats
    if chat.type == "private":
        await update.message.reply_text(
            "❌ This command can only be used in group chats."
        )
        return

    # Check if user is admin
    if not is_admin(user.id):
        await _send_admin_denied_and_cleanup(update)
        return

    # Check if already registered
    existing = get_registered_cafe_chat(chat.id)
    if existing:
        await update.message.reply_text(
            f"✅ This chat is already registered!\n\n"
            f"🧔 Brothers Orders Topic ID: `{existing['brothers_topic_id']}`\n"
            f"🧕 Sisters Orders Topic ID: `{existing['sisters_topic_id']}`",
            parse_mode="Markdown",
        )
        return

    # Create topics for brothers and sisters orders
    try:
        brothers_topic = await context.bot.create_forum_topic(
            chat_id=chat.id,
            name="🧔 Brothers Orders",
            icon_color=0x6FB9F0,  # Blue
        )
        sisters_topic = await context.bot.create_forum_topic(
            chat_id=chat.id,
            name="🧕 Sisters Orders",
            icon_color=0xFF93B2,  # Pink
        )

        # Register the chat
        success = register_cafe_chat(
            chat_id=chat.id,
            chat_title=chat.title or "Unknown",
            brothers_topic_id=brothers_topic.message_thread_id,
            sisters_topic_id=sisters_topic.message_thread_id,
        )

        if success:
            await update.message.reply_text(
                f"✅ *Chat Registered Successfully!*\n\n"
                f"🕌 This chat will now receive cafe order notifications.\n\n"
                f"🧔 Brothers orders → *Brothers Orders* topic\n"
                f"🧕 Sisters orders → *Sisters Orders* topic\n\n"
                f"_JazakAllah Khair for setting this up!_",
                parse_mode="Markdown",
            )

            # Send welcome messages to each topic
            await context.bot.send_message(
                chat_id=chat.id,
                message_thread_id=brothers_topic.message_thread_id,
                text=(
                    "🧔 *Brothers Orders*\n\n"
                    "This topic will receive notifications for brothers' orders.\n\n"
                    "Use the ✅ Complete or ❌ Deny buttons to process orders."
                ),
                parse_mode="Markdown",
            )
            await context.bot.send_message(
                chat_id=chat.id,
                message_thread_id=sisters_topic.message_thread_id,
                text=(
                    "🧕 *Sisters Orders*\n\n"
                    "This topic will receive notifications for sisters' orders.\n\n"
                    "Use the ✅ Complete or ❌ Deny buttons to process orders."
                ),
                parse_mode="Markdown",
            )
        else:
            await update.message.reply_text(
                "❌ Failed to register chat. Please try again."
            )

    except BadRequest as e:
        error_str = str(e).lower()
        if "not enough rights" in error_str:
            await update.message.reply_text(
                "❌ I don't have permission to create topics.\n\n"
                "Please make sure:\n"
                "1. This is a forum-enabled supergroup\n"
                "2. I have admin rights to manage topics"
            )
        elif "forum" in error_str or "topic" in error_str:
            await update.message.reply_text(
                "❌ This chat doesn't support forum topics.\n\n"
                "Please enable forum topics in group settings first."
            )
        else:
            logger.error(f"Error creating topics: {e}")
            await update.message.reply_text(f"❌ Error: {e}")
    except Exception as e:
        logger.error(f"Unexpected error registering chat: {e}")
        await update.message.reply_text(
            "❌ An unexpected error occurred. Please try again."
        )


async def deregister_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /deregister command - Deregister chat from cafe notifications (admin only)."""
    user = update.effective_user
    chat = update.effective_chat

    # Only allow in group chats
    if chat.type == "private":
        await update.message.reply_text(
            "❌ This command can only be used in group chats."
        )
        return

    # Check if user is admin
    if not is_admin(user.id):
        await _send_admin_denied_and_cleanup(update)
        return

    # Check if registered
    existing = get_registered_cafe_chat(chat.id)
    if not existing:
        await update.message.reply_text(
            "ℹ️ This chat is not registered for cafe notifications."
        )
        return

    # Deregister the chat
    success = deregister_cafe_chat(chat.id)

    if success:
        await update.message.reply_text(
            "✅ *Chat Deregistered Successfully!*\n\n"
            "🕌 This chat will no longer receive cafe order notifications.\n\n"
            "_The forum topics created for orders have not been deleted. "
            "You can delete them manually if needed._",
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text(
            "❌ Failed to deregister chat. Please try again."
        )


# ============================================================================
# CAFE OPEN/CLOSE COMMANDS (Admin Only)
# ============================================================================


async def _send_admin_denied_and_cleanup(update: Update, delay: float = 5.0):
    """Send admin access denied message and auto-delete after delay.
    
    Args:
        update: The Telegram update object.
        delay: Seconds to wait before deleting messages.
    """
    warning_msg = await update.message.reply_text(
        "🚫 _Admin access required. This message will be deleted._",
        parse_mode="Markdown",
    )
    
    # Wait and then delete both messages
    await asyncio.sleep(delay)
    try:
        await update.message.delete()
    except Exception:
        pass  # Message may already be deleted or bot lacks permissions
    try:
        await warning_msg.delete()
    except Exception:
        pass


async def open_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /open command - Open cafe for both brothers and sisters (admin only)."""
    user = update.effective_user

    if not is_admin(user.id):
        await _send_admin_denied_and_cleanup(update)
        return

    open_cafe(brothers=True, sisters=True)
    await update.message.reply_text(
        "☕ *Cafe is now OPEN!*\n\n"
        "🧔 Brothers: ✅ Accepting orders\n"
        "🧕 Sisters: ✅ Accepting orders\n\n"
        "_Bismillah, let's serve our community!_",
        parse_mode="Markdown",
    )
    logger.info(f"Cafe opened by admin {user.id} ({user.full_name})")


async def open_brothers_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /open_brothers command - Open cafe for brothers only (admin only)."""
    user = update.effective_user

    if not is_admin(user.id):
        await _send_admin_denied_and_cleanup(update)
        return

    state = open_cafe(brothers=True, sisters=False)
    await update.message.reply_text(
        "☕ *Brothers Cafe is now OPEN!*\n\n"
        f"🧔 Brothers: ✅ Accepting orders\n"
        f"🧕 Sisters: {'✅ Accepting orders' if state['sisters'] else '❌ Closed'}\n\n"
        "_Bismillah!_",
        parse_mode="Markdown",
    )
    logger.info(f"Brothers cafe opened by admin {user.id} ({user.full_name})")


async def open_sisters_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /open_sisters command - Open cafe for sisters only (admin only)."""
    user = update.effective_user

    if not is_admin(user.id):
        await _send_admin_denied_and_cleanup(update)
        return

    state = open_cafe(brothers=False, sisters=True)
    await update.message.reply_text(
        "☕ *Sisters Cafe is now OPEN!*\n\n"
        f"🧔 Brothers: {'✅ Accepting orders' if state['brothers'] else '❌ Closed'}\n"
        f"🧕 Sisters: ✅ Accepting orders\n\n"
        "_Bismillah!_",
        parse_mode="Markdown",
    )
    logger.info(f"Sisters cafe opened by admin {user.id} ({user.full_name})")


async def close_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /close command - Close cafe for both brothers and sisters (admin only)."""
    user = update.effective_user

    if not is_admin(user.id):
        await _send_admin_denied_and_cleanup(update)
        return

    close_cafe(brothers=True, sisters=True)
    await update.message.reply_text(
        "🚫 *Cafe is now CLOSED*\n\n"
        "🧔 Brothers: ❌ Not accepting orders\n"
        "🧕 Sisters: ❌ Not accepting orders\n\n"
        "_JazakAllah Khair for your service today!_",
        parse_mode="Markdown",
    )
    logger.info(f"Cafe closed by admin {user.id} ({user.full_name})")


async def close_brothers_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /close_brothers command - Close cafe for brothers only (admin only)."""
    user = update.effective_user

    if not is_admin(user.id):
        await _send_admin_denied_and_cleanup(update)
        return

    state = close_cafe(brothers=True, sisters=False)
    await update.message.reply_text(
        "🚫 *Brothers Cafe is now CLOSED*\n\n"
        f"🧔 Brothers: ❌ Not accepting orders\n"
        f"🧕 Sisters: {'✅ Accepting orders' if state['sisters'] else '❌ Closed'}\n\n"
        "_JazakAllah Khair!_",
        parse_mode="Markdown",
    )
    logger.info(f"Brothers cafe closed by admin {user.id} ({user.full_name})")


async def close_sisters_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /close_sisters command - Close cafe for sisters only (admin only)."""
    user = update.effective_user

    if not is_admin(user.id):
        await _send_admin_denied_and_cleanup(update)
        return

    state = close_cafe(brothers=False, sisters=True)
    await update.message.reply_text(
        "🚫 *Sisters Cafe is now CLOSED*\n\n"
        f"🧔 Brothers: {'✅ Accepting orders' if state['brothers'] else '❌ Closed'}\n"
        f"🧕 Sisters: ❌ Not accepting orders\n\n"
        "_JazakAllah Khair!_",
        parse_mode="Markdown",
    )
    logger.info(f"Sisters cafe closed by admin {user.id} ({user.full_name})")


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /status command - Check cafe open/close status."""
    state = get_cafe_state()

    brothers_status = "✅ Open" if state["brothers"] else "❌ Closed"
    sisters_status = "✅ Open" if state["sisters"] else "❌ Closed"

    if state["brothers"] and state["sisters"]:
        overall = "☕ *Cafe is OPEN*"
    elif state["brothers"] or state["sisters"]:
        overall = "☕ *Cafe is PARTIALLY OPEN*"
    else:
        overall = "🚫 *Cafe is CLOSED*"

    await update.message.reply_text(
        f"{overall}\n\n"
        f"🧔 Brothers: {brothers_status}\n"
        f"🧕 Sisters: {sisters_status}",
        parse_mode="Markdown",
    )


async def notify_staff_of_order(
    bot,
    order_id: str,
    telegram_id: int,
    telegram_name: str,
    item: str,
    price: float,
    gender: str,
    description: str = "",
    notes: str = "",
):
    """Notify registered chats about a new order."""
    registered_chats = get_all_registered_cafe_chats()

    if not registered_chats:
        logger.warning("No registered chats to notify about new order")
        return

    # Determine which topic to use based on gender
    gender_lower = gender.lower() if gender else ""
    is_sisters = gender_lower in ("sisters", "sister")
    is_brothers = gender_lower in ("brothers", "brother")

    # Build notification message
    if is_sisters:
        emoji = "🧕"
        section = "Sisters"
    elif is_brothers:
        emoji = "🧔"
        section = "Brothers"
    else:
        emoji = "🤲"
        section = "General"

    message = (
        f"{emoji} *New {section} Order!*\n\n"
        f"📋 *Order ID:* `{order_id}`\n"
        f"👤 *Customer:* {telegram_name}\n"
        f"🍽️ *Item:* {item}\n"
    )
    if description:
        message += f"📝 _{description}_\n"
    message += f"💰 *Price:* ${price:.2f}\n"
    if notes:
        message += f"\n⚠️ *Special Instructions:* _{notes}_\n"
    message += "\n_Please prepare this order, In Shaa Allah._"

    # Buttons for staff to complete or deny order
    keyboard = [
        [
            InlineKeyboardButton(
                "✅ Complete Order", callback_data=f"complete:{order_id}:{telegram_id}"
            ),
            InlineKeyboardButton(
                "❌ Deny Order", callback_data=f"deny:{order_id}:{telegram_id}"
            ),
        ]
    ]

    for chat in registered_chats:
        try:
            # Choose topic based on gender
            if is_sisters:
                topic_id = chat["sisters_topic_id"]
            elif is_brothers:
                topic_id = chat["brothers_topic_id"]
            else:
                # Default to brothers topic for non-gendered items
                topic_id = chat["brothers_topic_id"]

            await bot.send_message(
                chat_id=chat["chat_id"],
                message_thread_id=topic_id,
                text=message,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown",
            )
            logger.info(
                f"Notified chat {chat['chat_id']} about order {order_id} in topic {topic_id}"
            )
        except Exception as e:
            logger.error(f"Failed to notify chat {chat['chat_id']}: {e}")


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
        gender = result.get("gender", "") or ""
        gender_lower = gender.strip().lower()
        notes = result.get("notes", "")

        logger.info(
            f"Order {order_id} ready - gender='{gender}', gender_lower='{gender_lower}'"
        )

        # Determine pickup location based on gender
        if gender_lower in ("sisters", "sister"):
            pickup_location = "🧕 *Pickup Location:* Kitchen area (sisters section)"
        elif gender_lower in ("brothers", "brother"):
            pickup_location = "🧔 *Pickup Location:* Upstairs kitchen area (brothers section)"
        else:
            pickup_location = "📍 *Pickup Location:* Cafe counter"
            logger.warning(
                f"Order {order_id} has unrecognized gender '{gender}', using default pickup"
            )

        # Build notification message
        message = (
            f"🎉 *Alhamdulillah, your order is ready!*\n\n"
            f"🍽️ *Item:* {item}\n"
            f"📋 *Order ID:* `{order_id}`\n\n"
            f"{pickup_location}\n"
        )
        if notes:
            message += f"\n📝 *Your Instructions:* _{notes}_\n"
        message += (
            f"\n🤲 JazakAllah Khair for supporting our masjid!\n"
            f"May Allah accept your contribution and bless you!"
        )

        # Notify customer
        try:
            await context.bot.send_message(
                chat_id=telegram_id,
                text=message,
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


async def handle_order_complete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle completing an order from staff notification."""
    query = update.callback_query
    await query.answer()

    parts = query.data.split(":")
    order_id = parts[1]
    customer_telegram_id = int(parts[2])

    # Mark order as completed
    result = mark_order_completed(order_id)

    if result:
        # Check if already completed
        if result.get("already_completed"):
            await query.edit_message_text(
                f"⚠️ Order `{order_id}` was already completed.",
                parse_mode="Markdown",
            )
            return

        # Get item and determine pickup location
        item = result.get("item", "order")
        gender = result.get("gender", "")
        
        logger.info(f"Order {order_id} complete - gender field value: '{gender}'")

        gender_lower = gender.lower() if gender else ""
        if gender_lower in ("sisters", "sister"):
            pickup_location = "🧕 *Pickup Location:* Kitchen area (sisters section)"
        elif gender_lower in ("brothers", "brother"):
            pickup_location = "🧔 *Pickup Location:* Upstairs kitchen area (brothers section)"
        else:
            logger.warning(f"Order {order_id} has unexpected gender value: '{gender}'")
            pickup_location = "📍 *Pickup Location:* Cafe counter"

        # Notify customer
        try:
            await context.bot.send_message(
                chat_id=customer_telegram_id,
                text=(
                    f"🎉 *Alhamdulillah, your order is ready!*\n\n"
                    f"🍽️ *Item:* {item}\n"
                    f"📋 *Order ID:* `{order_id}`\n\n"
                    f"{pickup_location}\n\n"
                    f"🤲 JazakAllah Khair for supporting our masjid!\n"
                    f"May Allah accept your contribution and bless you!"
                ),
                parse_mode="Markdown",
            )
            await query.edit_message_text(
                f"✅ *Order Completed!*\n\n"
                f"Order `{order_id}` has been marked as complete.\n"
                f"Customer has been notified.\n\n"
                f"_JazakAllah Khair!_",
                parse_mode="Markdown",
            )
        except (BadRequest, Forbidden) as e:
            logger.warning(f"Could not notify user {customer_telegram_id}: {e}")
            await query.edit_message_text(
                f"✅ Order `{order_id}` marked as complete.\n"
                f"⚠️ Could not notify customer (they may have blocked the bot).",
                parse_mode="Markdown",
            )
    else:
        await query.edit_message_text(
            f"❌ Could not complete order `{order_id}`. Order not found in system.",
            parse_mode="Markdown",
        )


async def handle_order_deny(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle denying an order from staff notification."""
    query = update.callback_query
    await query.answer()

    parts = query.data.split(":")
    order_id = parts[1]
    customer_telegram_id = int(parts[2])

    # Mark order as denied
    result = mark_order_denied(order_id)

    if result:
        # Check if already processed
        if result.get("already_processed"):
            await query.edit_message_text(
                f"⚠️ Order `{order_id}` was already processed.",
                parse_mode="Markdown",
            )
            return

        # Notify customer
        try:
            await context.bot.send_message(
                chat_id=customer_telegram_id,
                text=(
                    f"😔 *Order Update*\n\n"
                    f"We're sorry, but your order `{order_id}` could not be fulfilled at this time.\n\n"
                    f"This may be due to:\n"
                    f"• Item temporarily unavailable\n"
                    f"• Kitchen capacity\n"
                    f"• Cafe closing soon\n\n"
                    f"Please try ordering again later or visit the cafe counter.\n\n"
                    f"_We apologize for any inconvenience. JazakAllah Khair for your understanding!_"
                ),
                parse_mode="Markdown",
            )
            await query.edit_message_text(
                f"❌ *Order Denied*\n\n"
                f"Order `{order_id}` has been denied.\n"
                f"Customer has been notified.\n\n"
                f"_May Allah make it easy._",
                parse_mode="Markdown",
            )
        except (BadRequest, Forbidden) as e:
            logger.warning(f"Could not notify user {customer_telegram_id}: {e}")
            await query.edit_message_text(
                f"❌ Order `{order_id}` denied.\n"
                f"⚠️ Could not notify customer (they may have blocked the bot).",
                parse_mode="Markdown",
            )
    else:
        await query.edit_message_text(
            f"❌ Could not deny order `{order_id}`. Order not found.",
            parse_mode="Markdown",
        )


# ============================================================================
# MAIN
# ============================================================================


async def post_init(app):
    """Start the unified sync processor after the bot initializes."""
    from google_sheets_operations import (
        get_gspread_client,
        orders_cache,
        SPREADSHEET_ID,
    )

    # Force initial cache sync before accepting commands
    try:
        client = get_gspread_client()
        count = orders_cache.refresh_from_sheet(
            client=client, spreadsheet_id=SPREADSHEET_ID
        )
        logger.info(f"Initial cache sync complete: {count} orders loaded")
    except Exception as e:
        logger.warning(f"Initial cache sync failed, will retry in background: {e}")

    # Start unified sync processor (handles both writes and cache refresh)
    sync_task = asyncio.create_task(unified_sync_processor(), name="unified_sync")
    background_tasks.append(sync_task)
    logger.info("Unified sync processor started")


async def post_shutdown(app):
    """Cancel background tasks gracefully on shutdown and flush pending writes."""
    from google_sheets_operations import (
        get_gspread_client,
        orders_cache,
        SPREADSHEET_ID,
    )

    logger.info("Shutting down background tasks...")

    # Cancel background tasks
    for task in background_tasks:
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                logger.info(f"Task {task.get_name()} cancelled successfully")

    # Final flush to ensure no data is lost
    try:
        pending_new, pending_updates = orders_cache.get_pending_count()
        if pending_new > 0 or pending_updates > 0:
            logger.info(
                f"Final flush: {pending_new} new orders, {pending_updates} updates pending"
            )
            client = get_gspread_client()
            result = orders_cache.flush_pending_writes(client, SPREADSHEET_ID)
            logger.info(
                f"Final flush complete: {result['new_orders_written']} orders, "
                f"{result['status_updates_written']} updates written"
            )
    except Exception as e:
        logger.error(f"Final flush failed: {e}")

    logger.info("All background tasks shut down")


def main():
    """Start the bot."""
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN environment variable not set!")
        return

    logger.info(f"Starting {BOT_USERNAME}...")

    # Create application with post_init and post_shutdown hooks
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    # Register command handlers
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("mystatus", mystatus_command))
    app.add_handler(CommandHandler("order", order_command))
    app.add_handler(CommandHandler("orders", orders_command))
    app.add_handler(CommandHandler("register", register_command))
    app.add_handler(CommandHandler("deregister", deregister_command))
    app.add_handler(CommandHandler("status", status_command))

    # Cafe open/close commands (admin only)
    app.add_handler(CommandHandler("open", open_command))
    app.add_handler(CommandHandler("open_brothers", open_brothers_command))
    app.add_handler(CommandHandler("open_sisters", open_sisters_command))
    app.add_handler(CommandHandler("close", close_command))
    app.add_handler(CommandHandler("close_brothers", close_brothers_command))
    app.add_handler(CommandHandler("close_sisters", close_sisters_command))

    # Register callback handlers
    app.add_handler(CallbackQueryHandler(handle_header_click, pattern=r"^header:"))
    app.add_handler(CallbackQueryHandler(handle_menu_selection, pattern=r"^menu:"))
    app.add_handler(
        CallbackQueryHandler(handle_instructions_add, pattern=r"^instructions:add$")
    )
    app.add_handler(
        CallbackQueryHandler(handle_instructions_skip, pattern=r"^instructions:skip$")
    )
    app.add_handler(
        CallbackQueryHandler(handle_order_confirmation, pattern=r"^confirm:")
    )
    app.add_handler(CallbackQueryHandler(handle_order_ready, pattern=r"^ready:"))
    app.add_handler(CallbackQueryHandler(handle_order_complete, pattern=r"^complete:"))
    app.add_handler(CallbackQueryHandler(handle_order_deny, pattern=r"^deny:"))

    # Register message handler for special instructions text input
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND, handle_special_instructions_text
        )
    )

    logger.info(f"{BOT_USERNAME} is now running...")
    app.run_polling()


if __name__ == "__main__":
    main()
