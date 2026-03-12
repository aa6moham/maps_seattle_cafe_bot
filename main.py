"""MAPS Masjid Cafe Bot - Telegram bot for managing cafe orders.

This bot allows users to browse menu items and place orders via DM.
Orders are tracked in Google Sheets and staff can manage them.
"""

import asyncio

from google_sheets_operations import (
    close_cafe,
    create_order,
    deregister_cafe_chat,
    get_admin_count,
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
    register_admin,
    register_cafe_chat,
    unified_sync_processor,
)
from logger import setup_logger
from order_workflows import (
    get_brothers_workflow,
    get_sisters_workflow,
    get_workflow,
    OrderData,
)
from private.constants import BOT_TOKEN, BOT_USERNAME
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest, Forbidden
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    filters,
    MessageHandler,
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
    active_orders = [
        o for o in user_orders if o.get("status", "").lower() in ("pending", "ready")
    ]
    recent_completed = [
        o for o in user_orders if o.get("status", "").lower() in ("completed", "denied")
    ][:5]

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

            order_text = (
                f"\n{status_emoji} *{item}* (#{order_id})\n   Status: {status_text}"
            )
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
    """Handle /order command - Ask user to select their gender first."""
    user = update.effective_user
    chat = update.effective_chat

    # Only allow orders in DM - silently ignore in group chats
    if chat.type != "private":
        return

    logger.info(f"User {user.id} ({user.full_name}) requested menu")

    # Check if user already has a pending order (rate limiting)
    user_orders = get_orders_for_user(user.id)
    pending_orders = [
        o for o in user_orders if o.get("status", "").lower() == "pending"
    ]

    if pending_orders:
        pending_order = pending_orders[0]
        await update.message.reply_text(
            "⏳ *You Already Have a Pending Order*\n\n"
            f"🍽️ *Item:* {pending_order.get('item', 'Unknown')}\n"
            f"📋 *Order ID:* `{pending_order.get('order_id', 'N/A')}`\n\n"
            "Please wait for your current order to be completed before placing a new one.\n\n"
            "_Use /mystatus to check your order status._",
            parse_mode="Markdown",
        )
        return

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

    # Build gender selection buttons (only show open sections)
    keyboard = []

    if brothers_open:
        keyboard.append(
            [InlineKeyboardButton("🧔 Brother", callback_data="gender:brothers")]
        )

    if sisters_open:
        keyboard.append(
            [InlineKeyboardButton("🧕 Sister", callback_data="gender:sisters")]
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
        "🕌 *MAPS Masjid Cafe*\n\n"
        "_All proceeds go to support our masjid!_\n"
        f"{status_msg}\n"
        "Please select your section:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )


async def handle_gender_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle gender selection (Brother/Sister) - Show appropriate menu.

    Uses the workflow system to get section-specific menu and behavior.
    """
    query = update.callback_query
    await query.answer()

    # Parse callback data: gender:{section}
    section = query.data.split(":")[1]  # "brothers" or "sisters"

    # Get the workflow for this section
    workflow = get_workflow(section)
    if not workflow:
        await query.edit_message_text("❌ Invalid section. Please try /order again.")
        return

    # Store the selected section/gender
    context.user_data["selected_gender"] = section

    # Check if this section is open
    if not workflow.is_open():
        await query.edit_message_text(
            f"🚫 *{workflow.display_name} Section is Currently Closed*\n\n"
            "We are not accepting orders for this section at this time.\n\n"
            "Please check back later, In Shaa Allah!",
            parse_mode="Markdown",
        )
        return

    # Use the workflow to show the menu
    await workflow.show_menu(query, context)


async def handle_gender_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle back button from menu to gender selection."""
    query = update.callback_query
    await query.answer()

    # Clear selected gender
    context.user_data.pop("selected_gender", None)

    # Check if cafe is still open
    cafe_state = get_cafe_state()
    brothers_open = cafe_state["brothers"]
    sisters_open = cafe_state["sisters"]

    if not brothers_open and not sisters_open:
        await query.edit_message_text(
            "🚫 *Cafe is Currently Closed*\n\n"
            "We are not accepting orders at this time.\n\n"
            "Please check back later, In Shaa Allah!",
            parse_mode="Markdown",
        )
        return

    # Build gender selection buttons
    keyboard = []

    if brothers_open:
        keyboard.append(
            [InlineKeyboardButton("🧔 Brother", callback_data="gender:brothers")]
        )

    if sisters_open:
        keyboard.append(
            [InlineKeyboardButton("🧕 Sister", callback_data="gender:sisters")]
        )

    await query.edit_message_text(
        "🕌 *MAPS Masjid Cafe*\n\n"
        "_All proceeds go to support our masjid!_\n\n"
        "Please select your section:",
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
    """Handle menu item selection - Start drink customization flow.

    Flow for Sisters: Shots → Decaf → Temperature → Syrup → Instructions → Confirm
    Flow for Brothers: Decaf → Temperature → Syrup → Instructions → Confirm
    """
    query = update.callback_query
    await query.answer()

    # Parse callback data: menu:{item_id}:{section}
    parts = query.data.split(":")
    item_id = parts[1]
    section = parts[2] if len(parts) > 2 else "general"

    # Get the workflow for this section
    workflow = get_workflow(section)

    # Fetch the specific menu item
    menu_items = get_menu_items()
    item = next((i for i in menu_items if str(i["item_id"]) == str(item_id)), None)

    if not item:
        await query.edit_message_text("❌ Item not found. Please try /order again.")
        return

    name = item["item"]
    price = item["price"]
    description = item.get("description", "")
    temperature_options = item.get("temperature_options", [])
    syrup_options = item.get("syrup_options", [])
    caffeine_options = item.get("caffeine_options", [])
    shots_options = item.get("shots_options", [])

    # Store selection in user context for confirmation
    context.user_data["pending_order"] = {
        "item_id": item_id,
        "item": name,
        "price": price,
        "section": section,
        "description": description,
        "notes": "",
        "shots": "",
        "decaf": "",
        "temperature": "",
        "syrup": "",
        "temperature_options": temperature_options,
        "syrup_options": syrup_options,
        "caffeine_options": caffeine_options,
        "shots_options": shots_options,
    }
    # Clear any waiting state
    context.user_data.pop("awaiting_instructions", None)

    # Start the customization flow based on available options
    # Order: shots -> caffeine -> temperature -> syrup -> order details
    if shots_options:
        await show_shots_selection(query, context)
    elif caffeine_options:
        await show_decaf_selection(query, context)
    elif temperature_options:
        await show_temperature_selection(query, context)
    elif syrup_options:
        await show_syrup_selection(query, context)
    else:
        await show_order_details(query, context)


# ============================================================================
# DRINK CUSTOMIZATION HANDLERS
# ============================================================================


async def show_shots_selection(query, context):
    """Show shots selection based on menu options."""
    pending_order = context.user_data.get("pending_order")
    if not pending_order:
        await query.edit_message_text(
            "❌ Order expired. Please start again with /order."
        )
        return

    shots_options = pending_order.get("shots_options", [])
    if not shots_options:
        # No shots options, skip to next step
        caffeine_options = pending_order.get("caffeine_options", [])
        if caffeine_options:
            await show_decaf_selection(query, context)
        elif pending_order.get("temperature_options"):
            await show_temperature_selection(query, context)
        elif pending_order.get("syrup_options"):
            await show_syrup_selection(query, context)
        else:
            await show_order_details(query, context)
        return

    # Build keyboard from dynamic options
    buttons = []
    row = []
    for shot in shots_options:
        label = f"{shot} Shot" if shot == "1" else f"{shot} Shots"
        row.append(InlineKeyboardButton(label, callback_data=f"customize:shots:{shot}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("❌ Cancel Order", callback_data="confirm:no")])

    await query.edit_message_text(
        f"☕ *Customize Your Order*\n\n"
        f"*Item:* {pending_order['item']}\n"
        f"💰 *Price:* ${pending_order['price']:.2f}\n\n"
        f"*Step 1: Number of Espresso Shots*\n\n"
        f"How many shots would you like?",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="Markdown",
    )


async def handle_shots_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle shots selection callback."""
    query = update.callback_query
    await query.answer()

    pending_order = context.user_data.get("pending_order")
    if not pending_order:
        await query.edit_message_text(
            "❌ Order expired. Please start again with /order."
        )
        return

    # Parse: customize:shots:{value}
    shots = query.data.split(":")[2]
    pending_order["shots"] = shots

    # Next step: caffeine selection (if available), then temperature, then syrup
    caffeine_options = pending_order.get("caffeine_options", [])
    if caffeine_options:
        await show_decaf_selection(query, context)
    elif pending_order.get("temperature_options"):
        await show_temperature_selection(query, context)
    elif pending_order.get("syrup_options"):
        await show_syrup_selection(query, context)
    else:
        await show_order_details(query, context)


async def show_decaf_selection(query, context):
    """Show caffeine selection based on menu options."""
    pending_order = context.user_data.get("pending_order")
    if not pending_order:
        await query.edit_message_text(
            "❌ Order expired. Please start again with /order."
        )
        return

    caffeine_options = pending_order.get("caffeine_options", [])
    if not caffeine_options:
        # No caffeine options, skip to next step
        if pending_order.get("temperature_options"):
            await show_temperature_selection(query, context)
        elif pending_order.get("syrup_options"):
            await show_syrup_selection(query, context)
        else:
            await show_order_details(query, context)
        return

    # Calculate step number based on what options are available
    step_num = 1
    if pending_order.get("shots_options") and pending_order.get("shots"):
        step_num += 1

    # Build keyboard from dynamic options
    buttons = []
    row = []
    for option in caffeine_options:
        # Add appropriate emoji
        if option.lower() == "decaf":
            label = f"🌙 {option}"
        elif option.lower() == "caffeinated":
            label = f"☕ {option}"
        else:
            label = option
        row.append(InlineKeyboardButton(label, callback_data=f"customize:decaf:{option}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("❌ Cancel Order", callback_data="confirm:no")])

    # Show current selections
    selections = []
    if pending_order.get("shots"):
        selections.append(f"☕ Shots: {pending_order['shots']}")

    selections_text = "\n".join(selections) if selections else ""
    if selections_text:
        selections_text = f"\n\n📋 *Your Selections:*\n{selections_text}"

    await query.edit_message_text(
        f"☕ *Customize Your Order*\n\n"
        f"*Item:* {pending_order['item']}\n"
        f"💰 *Price:* ${pending_order['price']:.2f}"
        f"{selections_text}\n\n"
        f"*Step {step_num}: Caffeinated or Decaf?*",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="Markdown",
    )


async def handle_decaf_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle decaf selection callback."""
    query = update.callback_query
    await query.answer()

    pending_order = context.user_data.get("pending_order")
    if not pending_order:
        await query.edit_message_text(
            "❌ Order expired. Please start again with /order."
        )
        return

    # Parse: customize:decaf:{value}
    decaf = query.data.split(":")[2]
    pending_order["decaf"] = decaf

    # Next step: temperature (if options exist) or syrup
    if pending_order.get("temperature_options"):
        await show_temperature_selection(query, context)
    elif pending_order.get("syrup_options"):
        await show_syrup_selection(query, context)
    else:
        await show_order_details(query, context)


async def show_temperature_selection(query, context):
    """Show temperature selection (if available for the drink)."""
    pending_order = context.user_data.get("pending_order")
    if not pending_order:
        await query.edit_message_text(
            "❌ Order expired. Please start again with /order."
        )
        return

    temperature_options = pending_order.get("temperature_options", [])
    if not temperature_options:
        # Skip to next step
        if pending_order.get("syrup_options"):
            await show_syrup_selection(query, context)
        else:
            await show_order_details(query, context)
        return

    # Calculate step number dynamically based on previous selections
    step_num = 1
    if pending_order.get("shots_options") and pending_order.get("shots"):
        step_num += 1
    if pending_order.get("caffeine_options") and pending_order.get("decaf"):
        step_num += 1

    # Build keyboard with temperature options
    keyboard = []
    row = []
    for i, temp in enumerate(temperature_options):
        emoji = (
            "🔥"
            if "hot" in temp.lower()
            else "🧊" if "ice" in temp.lower() or "cold" in temp.lower() else "🥤"
        )
        row.append(
            InlineKeyboardButton(
                f"{emoji} {temp}", callback_data=f"customize:temp:{temp}"
            )
        )
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append(
        [InlineKeyboardButton("❌ Cancel Order", callback_data="confirm:no")]
    )

    # Show current selections
    selections = []
    if pending_order.get("shots"):
        selections.append(f"☕ Shots: {pending_order['shots']}")
    if pending_order.get("decaf"):
        selections.append(f"🌙 Type: {pending_order['decaf']}")

    selections_text = "\n".join(selections) if selections else ""
    if selections_text:
        selections_text = f"\n\n📋 *Your Selections:*\n{selections_text}"

    await query.edit_message_text(
        f"☕ *Customize Your Order*\n\n"
        f"*Item:* {pending_order['item']}\n"
        f"💰 *Price:* ${pending_order['price']:.2f}"
        f"{selections_text}\n\n"
        f"*Step {step_num}: Temperature*\n\n"
        f"How would you like your drink?",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )


async def handle_temperature_selection(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    """Handle temperature selection callback."""
    query = update.callback_query
    await query.answer()

    pending_order = context.user_data.get("pending_order")
    if not pending_order:
        await query.edit_message_text(
            "❌ Order expired. Please start again with /order."
        )
        return

    # Parse: customize:temp:{value}
    temperature = query.data.split(":")[2]
    pending_order["temperature"] = temperature

    # Next step: syrup (if options exist) or order details
    if pending_order.get("syrup_options"):
        await show_syrup_selection(query, context)
    else:
        await show_order_details(query, context)


async def show_syrup_selection(query, context):
    """Show syrup selection (if available for the drink)."""
    pending_order = context.user_data.get("pending_order")
    if not pending_order:
        await query.edit_message_text(
            "❌ Order expired. Please start again with /order."
        )
        return

    syrup_options = pending_order.get("syrup_options", [])
    if not syrup_options:
        await show_order_details(query, context)
        return

    # Calculate step number dynamically based on previous selections
    step_num = 1
    if pending_order.get("shots_options") and pending_order.get("shots"):
        step_num += 1
    if pending_order.get("caffeine_options") and pending_order.get("decaf"):
        step_num += 1
    if pending_order.get("temperature_options") and pending_order.get("temperature"):
        step_num += 1

    # Build keyboard with syrup options (including "No Syrup")
    keyboard = []
    row = []
    for i, syrup in enumerate(syrup_options):
        row.append(
            InlineKeyboardButton(
                f"🍯 {syrup}", callback_data=f"customize:syrup:{syrup}"
            )
        )
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append(
        [InlineKeyboardButton("⏭️ No Syrup", callback_data="customize:syrup:None")]
    )
    keyboard.append(
        [InlineKeyboardButton("❌ Cancel Order", callback_data="confirm:no")]
    )

    # Show current selections
    selections = []
    if pending_order.get("shots"):
        selections.append(f"☕ Shots: {pending_order['shots']}")
    if pending_order.get("decaf"):
        selections.append(f"🌙 Type: {pending_order['decaf']}")
    if pending_order.get("temperature"):
        selections.append(f"🌡️ Temp: {pending_order['temperature']}")

    selections_text = "\n".join(selections) if selections else ""
    if selections_text:
        selections_text = f"\n\n📋 *Your Selections:*\n{selections_text}"

    await query.edit_message_text(
        f"☕ *Customize Your Order*\n\n"
        f"*Item:* {pending_order['item']}\n"
        f"💰 *Price:* ${pending_order['price']:.2f}"
        f"{selections_text}\n\n"
        f"*Step {step_num}: Syrup Flavor*\n\n"
        f"Would you like to add a syrup?",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )


async def handle_syrup_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle syrup selection callback."""
    query = update.callback_query
    await query.answer()

    pending_order = context.user_data.get("pending_order")
    if not pending_order:
        await query.edit_message_text(
            "❌ Order expired. Please start again with /order."
        )
        return

    # Parse: customize:syrup:{value}
    syrup = query.data.split(":")[2]
    if syrup != "None":
        pending_order["syrup"] = syrup
    else:
        pending_order["syrup"] = ""

    # Final step: show order details
    await show_order_details(query, context)


async def show_order_details(query, context):
    """Show the order details with all customizations before confirmation."""
    pending_order = context.user_data.get("pending_order")
    if not pending_order:
        await query.edit_message_text(
            "❌ Order expired. Please start again with /order."
        )
        return

    section = pending_order.get("section", "general")
    workflow = get_workflow(section)

    # Build customizations summary
    order_data = OrderData.from_dict(pending_order)
    customizations = order_data.get_customizations_summary()

    # Build item details
    if workflow:
        item_details = workflow.build_order_details(order_data)
    else:
        item_details = (
            f"*{pending_order['item']}*\n💰 *Price:* ${pending_order['price']:.2f}"
        )

    # Add customizations to display
    if customizations:
        item_details += f"\n\n☕ *Customizations:* {customizations}"

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
        f"🛒 *Order Summary*\n\n"
        f"{item_details}\n\n"
        f"Would you like to add any additional special instructions?",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )


async def handle_instructions_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle 'Add Special Instructions' button click."""
    query = update.callback_query
    await query.answer()

    pending_order = context.user_data.get("pending_order")
    if not pending_order:
        await query.edit_message_text(
            "❌ Order expired. Please start again with /order."
        )
        return

    # Set flag to await text input
    context.user_data["awaiting_instructions"] = True

    # Show current order with instruction prompt
    keyboard = [
        [
            InlineKeyboardButton(
                "⏭️ Skip (No Instructions)", callback_data="instructions:skip"
            )
        ],
        [InlineKeyboardButton("❌ Cancel Order", callback_data="confirm:no")],
    ]

    await query.edit_message_text(
        f"📝 *Add Special Instructions*\n\n"
        f"Order: *{pending_order['item']}*\n\n"
        f"Please type your special instructions below:\n"
        f'_(e.g., "decaf", "extra pump vanilla", "oat milk")_\n\n'
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
        await query.edit_message_text(
            "❌ Order expired. Please start again with /order."
        )
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
        await update.message.reply_text(
            "❌ Order expired. Please start again with /order."
        )
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
    item_details = (
        f"*{pending_order['item']}*\n💰 *Price:* ${pending_order['price']:.2f}"
    )
    if description:
        item_details += f"\n📝 _{description}_"
    if pickup_note:
        item_details += f"\n\n{pickup_note}"

    keyboard = [
        [
            InlineKeyboardButton("✅ Confirm Order", callback_data="confirm:yes"),
            InlineKeyboardButton("❌ Cancel", callback_data="confirm:no"),
        ],
        [InlineKeyboardButton("✏️ Edit Instructions", callback_data="instructions:add")],
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
    """Show final confirmation screen with all order details.

    Uses the workflow system for section-specific formatting.
    """
    section = pending_order.get("section", "general")
    workflow = get_workflow(section)

    order_data = OrderData.from_dict(pending_order)

    if workflow:
        # Use workflow for section-specific formatting
        item_details = workflow.build_order_details(order_data)
    else:
        # Fallback for general items
        description = pending_order.get("description", "")
        item_details = (
            f"*{pending_order['item']}*\n💰 *Price:* ${pending_order['price']:.2f}"
        )
        if description:
            item_details += f"\n📝 _{description}_"

    notes = pending_order.get("notes", "")

    keyboard = [
        [
            InlineKeyboardButton("✅ Confirm Order", callback_data="confirm:yes"),
            InlineKeyboardButton("❌ Cancel", callback_data="confirm:no"),
        ],
        [InlineKeyboardButton("📝 Add Instructions", callback_data="instructions:add")],
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
    """Handle order confirmation (YES/NO).

    Uses the workflow system for section-specific order creation and messaging.
    Includes drink customizations in the order notes.
    """
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

        # Get the workflow for this section
        section = pending_order.get("section", "general")
        workflow = get_workflow(section)
        order_data = OrderData.from_dict(pending_order)

        # Build full notes including customizations
        full_notes = order_data.build_full_notes()
        # Update the order_data notes for confirmation message
        order_data.notes = full_notes

        # Create the order using workflow (allows section-specific creation)
        if workflow:
            order_id = await workflow.create_order(
                telegram_id=user.id,
                telegram_name=user.full_name,
                order_data=order_data,
            )
        else:
            # Fallback: direct order creation
            order_id = create_order(
                telegram_id=user.id,
                telegram_name=user.full_name,
                item=pending_order["item"],
                price=pending_order["price"],
                gender=section,
                notes=full_notes,
            )

        if order_id:
            logger.info(
                f"Order {order_id} created for user {user.id}: {pending_order['item']} "
                f"(customizations: {order_data.get_customizations_summary() or 'none'})"
            )

            # Build confirmation message using workflow
            if workflow:
                confirmation_msg = workflow.build_confirmation_message(
                    order_data, order_id
                )
            else:
                # Fallback confirmation
                confirmation_msg = (
                    f"✅ *Order Placed!*\n\n"
                    f"Your order for *{pending_order['item']}* has been enqueued.\n\n"
                    f"Order ID: `{order_id}`\n\n"
                    f"🤲 *JazakAllah Khair!*"
                )

            await query.edit_message_text(confirmation_msg, parse_mode="Markdown")

            # Notify staff in registered chats (include customizations in notes)
            await notify_staff_of_order(
                bot=context.bot,
                order_id=order_id,
                telegram_id=user.id,
                telegram_name=user.full_name,
                item=pending_order["item"],
                price=pending_order["price"],
                gender=section,
                notes=full_notes,
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
# REGISTER ADMIN COMMAND
# ============================================================================


async def register_admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Register a user as an admin (/register_admin).

    Bootstrap behavior: If no admins exist, the first user to run this
    command becomes the initial admin. After that, only existing admins
    can register new admins.

    Usage:
        /register_admin - Register yourself (bootstrap or admin only)
        /register_admin @username - Admin registers another user (reply to their message)
    """
    user = update.effective_user
    user_id = user.id
    user_name = user.full_name or user.username or str(user_id)

    logger.info(f"register_admin_command called by {user_name} ({user_id})")

    # Check if this is a bootstrap situation (no admins exist)
    admin_count = get_admin_count()
    is_bootstrap = admin_count == 0

    if is_bootstrap:
        # Bootstrap: First user becomes admin
        logger.info(f"Bootstrap mode: Registering first admin {user_name} ({user_id})")
        success = register_admin(user_id, user_name)
        if success:
            await update.message.reply_text(
                "🎉 *Welcome, First Admin!*\n\n"
                f"✅ You have been registered as the initial admin.\n"
                f"👤 Name: {user_name}\n"
                f"🆔 ID: `{user_id}`\n\n"
                "_You can now register other admins using this command._",
                parse_mode="Markdown",
            )
        else:
            await update.message.reply_text(
                "❌ Failed to register admin. Please try again."
            )
        return

    # Not bootstrap: Check if caller is an existing admin
    if not is_admin(user_id):
        await update.message.reply_text(
            "🚫 *Admin Access Required*\n\n"
            "Only existing admins can register new admins.",
            parse_mode="Markdown",
        )
        return

    # Check if registering someone else via reply
    target_user = None
    target_id = None
    target_name = None

    if update.message.reply_to_message:
        # Admin is replying to someone's message to register them
        target_user = update.message.reply_to_message.from_user
        if target_user:
            target_id = target_user.id
            target_name = (
                target_user.full_name or target_user.username or str(target_id)
            )
    else:
        # Admin is registering themselves
        target_id = user_id
        target_name = user_name

    if not target_id:
        await update.message.reply_text(
            "❌ Could not identify user to register.\n\n"
            "Usage:\n"
            "• `/register_admin` - Register yourself\n"
            "• Reply to a user's message with `/register_admin` - Register that user",
            parse_mode="Markdown",
        )
        return

    # Register the target user
    success = register_admin(target_id, target_name)

    if success:
        if target_id == user_id:
            await update.message.reply_text(
                "✅ *Admin Registered!*\n\n"
                f"👤 Name: {target_name}\n"
                f"🆔 ID: `{target_id}`",
                parse_mode="Markdown",
            )
        else:
            await update.message.reply_text(
                "✅ *New Admin Registered!*\n\n"
                f"👤 Name: {target_name}\n"
                f"🆔 ID: `{target_id}`\n\n"
                f"_Registered by {user_name}_",
                parse_mode="Markdown",
            )
    else:
        await update.message.reply_text(
            f"ℹ️ {target_name} is already registered as an admin."
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


async def _deny_pending_orders_and_notify(bot, gender_filter: str | None = None) -> int:
    """Deny pending orders and notify users.

    Args:
        bot: The Telegram bot instance.
        gender_filter: If set, only deny orders for this gender ("brothers" or "sisters").
                      If None, deny all pending orders.

    Returns:
        Number of orders denied.
    """
    pending = get_pending_orders()
    denied_count = 0

    for order in pending:
        order_gender = order.get("gender", "").lower()

        # Filter by gender if specified
        if gender_filter:
            # Match "brothers"/"brother" or "sisters"/"sister"
            if gender_filter == "brothers" and order_gender not in (
                "brothers",
                "brother",
            ):
                continue
            if gender_filter == "sisters" and order_gender not in ("sisters", "sister"):
                continue

        order_id = order.get("order_id")
        telegram_id = order.get("telegram_id")
        item = order.get("item", "Unknown")

        # Deny the order
        result = mark_order_denied(order_id)

        if result and not result.get("already_processed"):
            denied_count += 1

            # Notify the user
            try:
                if gender_filter:
                    section = (
                        "🧔 Brothers" if gender_filter == "brothers" else "🧕 Sisters"
                    )
                    message = (
                        f"🚫 *Order Cancelled - {section} Section Closed*\n\n"
                        f"🍽️ *Item:* {item}\n"
                        f"📋 *Order ID:* `{order_id}`\n\n"
                        f"The {section.split()[1].lower()} section has been closed. "
                        f"Your order has been cancelled.\n\n"
                        "_We apologize for the inconvenience. Please try again later!_"
                    )
                else:
                    message = (
                        f"🚫 *Order Cancelled - Cafe Closed*\n\n"
                        f"🍽️ *Item:* {item}\n"
                        f"📋 *Order ID:* `{order_id}`\n\n"
                        "The cafe has been closed. Your order has been cancelled.\n\n"
                        "_We apologize for the inconvenience. Please try again later!_"
                    )

                await bot.send_message(
                    chat_id=telegram_id,
                    text=message,
                    parse_mode="Markdown",
                )
                logger.info(
                    f"Notified user {telegram_id} about cancelled order {order_id}"
                )
            except Exception as e:
                logger.error(
                    f"Failed to notify user {telegram_id} about cancelled order: {e}"
                )

    return denied_count


async def close_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /close command - Close cafe for both brothers and sisters (admin only)."""
    user = update.effective_user

    if not is_admin(user.id):
        await _send_admin_denied_and_cleanup(update)
        return

    close_cafe(brothers=True, sisters=True)

    # Deny all pending orders and notify users
    denied_count = await _deny_pending_orders_and_notify(
        context.bot, gender_filter=None
    )

    denied_msg = ""
    if denied_count > 0:
        denied_msg = f"\n\n⚠️ _{denied_count} pending order(s) have been cancelled and users notified._"

    await update.message.reply_text(
        "🚫 *Cafe is now CLOSED*\n\n"
        "🧔 Brothers: ❌ Not accepting orders\n"
        "🧕 Sisters: ❌ Not accepting orders\n\n"
        f"_JazakAllah Khair for your service today!_{denied_msg}",
        parse_mode="Markdown",
    )
    logger.info(
        f"Cafe closed by admin {user.id} ({user.full_name}), {denied_count} orders denied"
    )


async def close_brothers_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /close_brothers command - Close cafe for brothers only (admin only)."""
    user = update.effective_user

    if not is_admin(user.id):
        await _send_admin_denied_and_cleanup(update)
        return

    state = close_cafe(brothers=True, sisters=False)

    # Deny brothers pending orders and notify users
    denied_count = await _deny_pending_orders_and_notify(
        context.bot, gender_filter="brothers"
    )

    denied_msg = ""
    if denied_count > 0:
        denied_msg = f"\n\n⚠️ _{denied_count} brothers order(s) have been cancelled and users notified._"

    await update.message.reply_text(
        "🚫 *Brothers Cafe is now CLOSED*\n\n"
        f"🧔 Brothers: ❌ Not accepting orders\n"
        f"🧕 Sisters: {'✅ Accepting orders' if state['sisters'] else '❌ Closed'}\n\n"
        f"_JazakAllah Khair!_{denied_msg}",
        parse_mode="Markdown",
    )
    logger.info(
        f"Brothers cafe closed by admin {user.id} ({user.full_name}), {denied_count} orders denied"
    )


async def close_sisters_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /close_sisters command - Close cafe for sisters only (admin only)."""
    user = update.effective_user

    if not is_admin(user.id):
        await _send_admin_denied_and_cleanup(update)
        return

    state = close_cafe(brothers=False, sisters=True)

    # Deny sisters pending orders and notify users
    denied_count = await _deny_pending_orders_and_notify(
        context.bot, gender_filter="sisters"
    )

    denied_msg = ""
    if denied_count > 0:
        denied_msg = f"\n\n⚠️ _{denied_count} sisters order(s) have been cancelled and users notified._"

    await update.message.reply_text(
        "🚫 *Sisters Cafe is now CLOSED*\n\n"
        f"🧔 Brothers: {'✅ Accepting orders' if state['brothers'] else '❌ Closed'}\n"
        f"🧕 Sisters: ❌ Not accepting orders\n\n"
        f"_JazakAllah Khair!_{denied_msg}",
        parse_mode="Markdown",
    )
    logger.info(
        f"Sisters cafe closed by admin {user.id} ({user.full_name}), {denied_count} orders denied"
    )


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
        f"💰 *Price:* ${price:.2f}\n"
    )
    if notes:
        message += f"\n⚠️ *Special Instructions:* _{notes}_\n"
    message += "\n_Please prepare this order, In Shaa Allah._"

    # Buttons for staff to mark order status
    keyboard = [
        [
            InlineKeyboardButton(
                "🔄 In Progress", callback_data=f"inprogress:{order_id}:{telegram_id}"
            ),
        ],
        [
            InlineKeyboardButton(
                "✅ Ready", callback_data=f"ready:{order_id}:{telegram_id}"
            ),
            InlineKeyboardButton(
                "❌ Deny", callback_data=f"deny:{order_id}:{telegram_id}"
            ),
        ],
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


async def handle_in_progress(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle marking an order as in progress (being prepared)."""
    query = update.callback_query
    await query.answer()

    parts = query.data.split(":")
    order_id = parts[1]
    customer_telegram_id = int(parts[2])

    # Get the original message to extract order details
    original_message = query.message.text if query.message else ""

    # Update the message to show in progress status
    # Keep the Ready and Deny buttons, remove In Progress button
    keyboard = [
        [
            InlineKeyboardButton(
                "✅ Ready", callback_data=f"ready:{order_id}:{customer_telegram_id}"
            ),
            InlineKeyboardButton(
                "❌ Deny", callback_data=f"deny:{order_id}:{customer_telegram_id}"
            ),
        ],
    ]

    # Extract order details from original message
    customer_name = ""
    item = ""
    price = ""
    notes = ""

    for line in original_message.split("\n"):
        if "Customer:" in line:
            customer_name = (
                line.replace("*", "").replace("👤", "").replace("Customer:", "").strip()
            )
        elif "Item:" in line:
            item = line.replace("*", "").replace("🍽️", "").replace("Item:", "").strip()
        elif "Price:" in line:
            price = (
                line.replace("*", "").replace("💰", "").replace("Price:", "").strip()
            )
        elif "Special Instructions:" in line:
            notes = (
                line.replace("*", "")
                .replace("⚠️", "")
                .replace("Special Instructions:", "")
                .replace("_", "")
                .strip()
            )

    # Build updated message preserving all order details
    message = (
        f"🔄 *ORDER IN PROGRESS*\n\n"
        f"📋 *Order ID:* `{order_id}`\n"
        f"👤 *Customer:* {customer_name}\n"
        f"🍽️ *Item:* {item}\n"
        f"💰 *Price:* {price}\n"
    )
    if notes:
        message += f"\n⚠️ *Special Instructions:* _{notes}_\n"
    message += "\n⏳ _This order is being prepared..._"

    await query.edit_message_text(
        message,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )

    logger.info(f"Order {order_id} marked as in progress by staff")


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
            pickup_location = (
                "🧔 *Pickup Location:* Upstairs kitchen area (brothers section)"
            )
        else:
            pickup_location = "📍 *Pickup Location:* Cafe counter"
            logger.warning(
                f"Order {order_id} has unrecognized gender '{gender}', using default pickup"
            )

        # Get workflow for this section (if available)
        workflow = get_workflow(gender) if gender else None

        # Build notification message using workflow or fallback
        if workflow:
            message = workflow.build_ready_message(
                {
                    "item": item,
                    "order_id": order_id,
                    "notes": notes,
                }
            )
        else:
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

    # Get the original message to extract order details
    original_message = query.message.text if query.message else ""

    # Extract order details from original message
    customer_name = ""
    item = ""
    price = ""
    notes = ""

    for line in original_message.split("\n"):
        if "Customer:" in line:
            customer_name = (
                line.replace("*", "").replace("👤", "").replace("Customer:", "").strip()
            )
        elif "Item:" in line:
            item = line.replace("*", "").replace("🍽️", "").replace("Item:", "").strip()
        elif "Price:" in line:
            price = (
                line.replace("*", "").replace("💰", "").replace("Price:", "").strip()
            )
        elif "Special Instructions:" in line:
            notes = (
                line.replace("*", "")
                .replace("⚠️", "")
                .replace("Special Instructions:", "")
                .replace("_", "")
                .strip()
            )

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
        item_from_db = result.get("item", "order")
        gender = result.get("gender", "")

        # Use item from message if extracted, otherwise from DB
        if not item:
            item = item_from_db

        logger.info(f"Order {order_id} complete - gender field value: '{gender}'")

        gender_lower = gender.lower() if gender else ""
        if gender_lower in ("sisters", "sister"):
            pickup_location = "🧕 *Pickup Location:* Kitchen area (sisters section)"
        elif gender_lower in ("brothers", "brother"):
            pickup_location = (
                "🧔 *Pickup Location:* Upstairs kitchen area (brothers section)"
            )
        else:
            logger.warning(f"Order {order_id} has unexpected gender value: '{gender}'")
            pickup_location = "📍 *Pickup Location:* Cafe counter"

        # Get customer name from result if not extracted from message
        if not customer_name:
            customer_name = result.get("telegram_name", "Unknown")

        # Build final message preserving all order details
        final_message = (
            f"✅ *ORDER COMPLETED*\n\n"
            f"📋 *Order ID:* `{order_id}`\n"
            f"👤 *Customer:* {customer_name}\n"
            f"🍽️ *Item:* {item}\n"
        )
        if price:
            final_message += f"💰 *Price:* {price}\n"
        if notes:
            final_message += f"\n⚠️ *Special Instructions:* _{notes}_\n"
        final_message += "\n✅ _Customer has been notified._\n\n_JazakAllah Khair!_"

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
                final_message,
                parse_mode="Markdown",
            )
        except (BadRequest, Forbidden) as e:
            logger.warning(f"Could not notify user {customer_telegram_id}: {e}")
            final_message_error = (
                f"✅ *ORDER COMPLETED*\n\n"
                f"📋 *Order ID:* `{order_id}`\n"
                f"👤 *Customer:* {customer_name}\n"
                f"🍽️ *Item:* {item}\n"
            )
            if price:
                final_message_error += f"💰 *Price:* {price}\n"
            if notes:
                final_message_error += f"\n⚠️ *Special Instructions:* _{notes}_\n"
            final_message_error += (
                "\n⚠️ _Could not notify customer (they may have blocked the bot)._"
            )
            await query.edit_message_text(
                final_message_error,
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

    # Get the original message to extract order details
    original_message = query.message.text if query.message else ""

    # Extract order details from original message
    customer_name = ""
    item = ""
    price = ""
    notes = ""

    for line in original_message.split("\n"):
        if "Customer:" in line:
            customer_name = (
                line.replace("*", "").replace("👤", "").replace("Customer:", "").strip()
            )
        elif "Item:" in line:
            item = line.replace("*", "").replace("🍽️", "").replace("Item:", "").strip()
        elif "Price:" in line:
            price = (
                line.replace("*", "").replace("💰", "").replace("Price:", "").strip()
            )
        elif "Special Instructions:" in line:
            notes = (
                line.replace("*", "")
                .replace("⚠️", "")
                .replace("Special Instructions:", "")
                .replace("_", "")
                .strip()
            )

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

        # Get customer name from result if not extracted from message
        if not customer_name:
            customer_name = result.get("telegram_name", "Unknown")

        # Build final message preserving all order details
        final_message = (
            f"❌ *ORDER DENIED*\n\n"
            f"📋 *Order ID:* `{order_id}`\n"
            f"👤 *Customer:* {customer_name}\n"
        )
        if item:
            final_message += f"🍽️ *Item:* {item}\n"
        if price:
            final_message += f"💰 *Price:* {price}\n"
        if notes:
            final_message += f"\n⚠️ *Special Instructions:* _{notes}_\n"
        final_message += (
            "\n❌ _Customer has been notified._\n\n_May Allah make it easy._"
        )

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
                final_message,
                parse_mode="Markdown",
            )
        except (BadRequest, Forbidden) as e:
            logger.warning(f"Could not notify user {customer_telegram_id}: {e}")
            final_message_error = (
                f"❌ *ORDER DENIED*\n\n"
                f"📋 *Order ID:* `{order_id}`\n"
                f"👤 *Customer:* {customer_name}\n"
            )
            if item:
                final_message_error += f"🍽️ *Item:* {item}\n"
            if price:
                final_message_error += f"💰 *Price:* {price}\n"
            if notes:
                final_message_error += f"\n⚠️ *Special Instructions:* _{notes}_\n"
            final_message_error += (
                "\n⚠️ _Could not notify customer (they may have blocked the bot)._"
            )
            await query.edit_message_text(
                final_message_error,
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
    app.add_handler(CommandHandler("register_admin", register_admin_command))
    app.add_handler(CommandHandler("status", status_command))

    # Cafe open/close commands (admin only)
    app.add_handler(CommandHandler("open", open_command))
    app.add_handler(CommandHandler("open_brothers", open_brothers_command))
    app.add_handler(CommandHandler("open_sisters", open_sisters_command))
    app.add_handler(CommandHandler("close", close_command))
    app.add_handler(CommandHandler("close_brothers", close_brothers_command))
    app.add_handler(CommandHandler("close_sisters", close_sisters_command))

    # Register callback handlers
    app.add_handler(
        CallbackQueryHandler(
            handle_gender_selection, pattern=r"^gender:(brothers|sisters)$"
        )
    )
    app.add_handler(CallbackQueryHandler(handle_gender_back, pattern=r"^gender:back$"))
    app.add_handler(CallbackQueryHandler(handle_header_click, pattern=r"^header:"))
    app.add_handler(CallbackQueryHandler(handle_menu_selection, pattern=r"^menu:"))
    # Drink customization handlers
    app.add_handler(
        CallbackQueryHandler(handle_shots_selection, pattern=r"^customize:shots:")
    )
    app.add_handler(
        CallbackQueryHandler(handle_decaf_selection, pattern=r"^customize:decaf:")
    )
    app.add_handler(
        CallbackQueryHandler(handle_temperature_selection, pattern=r"^customize:temp:")
    )
    app.add_handler(
        CallbackQueryHandler(handle_syrup_selection, pattern=r"^customize:syrup:")
    )
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
    app.add_handler(CallbackQueryHandler(handle_in_progress, pattern=r"^inprogress:"))
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
