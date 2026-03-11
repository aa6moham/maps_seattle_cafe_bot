"""Google Sheets operations for MAPS Cafe Bot.

Handles menu reading and order management via Google Sheets API.
Uses OrdersCache for batched writes to prevent rate limiting.
"""

import asyncio
import time
from datetime import datetime
from functools import wraps
from threading import Lock

import gspread
from google.oauth2.service_account import Credentials

from logger import setup_logger
from orders_cache import OrdersCache
from private.constants import SPREADSHEET_ID

logger = setup_logger(__name__)

# Global orders cache instance
orders_cache = OrdersCache(sync_interval=20.0)

# Cafe state management (in-memory with persistence to sheet)
# State: {"brothers": True/False, "sisters": True/False}
_cafe_state: dict[str, bool] = {"brothers": False, "sisters": False}
_cafe_state_lock = Lock()

# Google Sheets API scopes
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# Cache for gspread client
_gspread_client = None
_gspread_client_lock = Lock()

# Simple in-memory cache
_cache: dict[str, tuple[any, float]] = {}
_CACHE_TTL = 60  # 1 minute default TTL


# ============================================================================
# CACHING UTILITIES
# ============================================================================


def get_cached(key: str) -> any:
    """Get a cached value if not expired."""
    if key in _cache:
        value, expiry = _cache[key]
        if time.time() < expiry:
            return value
        del _cache[key]
    return None


def set_cached(key: str, value: any, ttl: int = _CACHE_TTL):
    """Set a cached value with TTL."""
    _cache[key] = (value, time.time() + ttl)


def invalidate_cache(key: str):
    """Invalidate a specific cache key."""
    _cache.pop(key, None)


# ============================================================================
# RETRY DECORATOR
# ============================================================================


def retry_on_quota_error(max_retries: int = 3, base_delay: float = 2.0):
    """Decorator to retry on Google Sheets quota errors."""

    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            last_error = None
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except gspread.exceptions.APIError as e:
                    last_error = e
                    if "quota" in str(e).lower() or e.response.status_code == 429:
                        delay = base_delay * (2**attempt)
                        logger.warning(
                            f"Quota error on {func.__name__}, attempt {attempt + 1}/{max_retries}. "
                            f"Retrying in {delay}s..."
                        )
                        time.sleep(delay)
                    else:
                        raise
            raise last_error

        return wrapper

    return decorator


# ============================================================================
# GSPREAD CLIENT
# ============================================================================


def get_gspread_client() -> gspread.Client:
    """Get or create a gspread client."""
    global _gspread_client

    with _gspread_client_lock:
        if _gspread_client is None:
            creds = Credentials.from_service_account_file("creds/creds.json", scopes=SCOPES)
            _gspread_client = gspread.authorize(creds)
            logger.info("Created new gspread client")

        return _gspread_client


# ============================================================================
# ADMIN OPERATIONS
# ============================================================================


@retry_on_quota_error()
def is_admin(telegram_id: int) -> bool:
    """Check if a user is an admin.

    Args:
        telegram_id: The Telegram user ID to check.

    Returns:
        True if user is in the admins sheet, False otherwise.
    """
    cache_key = f"admin:{telegram_id}"
    cached = get_cached(cache_key)
    if cached is not None:
        return cached

    client = get_gspread_client()
    spreadsheet = client.open_by_key(SPREADSHEET_ID)

    try:
        admins_sheet = spreadsheet.worksheet("admins")
    except gspread.exceptions.WorksheetNotFound:
        logger.warning("'admins' sheet not found")
        set_cached(cache_key, False, ttl=300)
        return False

    all_admins = admins_sheet.get_all_records()
    logger.info(f"Checking admin status for {telegram_id}, found {len(all_admins)} admins in sheet")

    for admin in all_admins:
        try:
            admin_id_raw = admin.get("telegram_id", 0)
            # Handle string or int values, strip whitespace
            if isinstance(admin_id_raw, str):
                admin_id_raw = admin_id_raw.strip()
            admin_id = int(admin_id_raw)
            
            logger.debug(f"Comparing {telegram_id} with admin {admin_id}")
            
            if admin_id == telegram_id:
                logger.info(f"User {telegram_id} is an admin")
                set_cached(cache_key, True, ttl=300)
                return True
        except (ValueError, TypeError) as e:
            logger.warning(f"Invalid telegram_id in admins sheet: {admin.get('telegram_id')} - {e}")
            continue

    logger.info(f"User {telegram_id} is NOT an admin")
    set_cached(cache_key, False, ttl=300)
    return False


@retry_on_quota_error()
def get_admin_count() -> int:
    """Get the number of admins in the admins sheet.

    Returns:
        Number of valid admins (with numeric telegram_id).
    """
    client = get_gspread_client()
    spreadsheet = client.open_by_key(SPREADSHEET_ID)

    try:
        admins_sheet = spreadsheet.worksheet("admins")
    except gspread.exceptions.WorksheetNotFound:
        return 0

    all_admins = admins_sheet.get_all_records()
    count = 0
    for admin in all_admins:
        try:
            admin_id_raw = admin.get("telegram_id", 0)
            if isinstance(admin_id_raw, str):
                admin_id_raw = admin_id_raw.strip()
            int(admin_id_raw)  # Validate it's a number
            count += 1
        except (ValueError, TypeError):
            continue
    return count


@retry_on_quota_error()
def register_admin(telegram_id: int, telegram_name: str) -> bool:
    """Register a user as an admin.

    Args:
        telegram_id: The Telegram user ID (must be a numeric ID, not username).
        telegram_name: The Telegram display name or username.

    Returns:
        True if successfully registered, False if already exists or error.
    """
    # Validate telegram_id is a proper numeric ID
    if not isinstance(telegram_id, int) or telegram_id <= 0:
        logger.error(f"Invalid telegram_id: {telegram_id} (must be a positive integer)")
        return False

    # Ensure telegram_name is different from telegram_id (avoid storing ID as name)
    if telegram_name == str(telegram_id):
        logger.warning(f"telegram_name is same as telegram_id ({telegram_id}), using 'Unknown'")
        telegram_name = "Unknown"

    client = get_gspread_client()
    spreadsheet = client.open_by_key(SPREADSHEET_ID)

    try:
        admins_sheet = spreadsheet.worksheet("admins")
    except gspread.exceptions.WorksheetNotFound:
        # Create the sheet with headers
        admins_sheet = spreadsheet.add_worksheet(title="admins", rows=100, cols=3)
        admins_sheet.append_row(["telegram_id", "telegram_name", "registered_at"])
        logger.info("Created 'admins' sheet")

    # Check if already registered
    all_admins = admins_sheet.get_all_records()
    for admin in all_admins:
        try:
            admin_id_raw = admin.get("telegram_id", 0)
            if isinstance(admin_id_raw, str):
                admin_id_raw = admin_id_raw.strip()
            if int(admin_id_raw) == telegram_id:
                logger.info(f"User {telegram_id} is already an admin")
                return False
        except (ValueError, TypeError):
            continue

    # Add new admin - use int() to ensure numeric storage
    registered_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    admins_sheet.append_row(
        [int(telegram_id), str(telegram_name), registered_at],
        value_input_option="RAW",  # Prevents Google Sheets from interpreting values
    )

    # Invalidate cache
    invalidate_cache(f"admin:{telegram_id}")

    logger.info(f"Registered new admin: {telegram_name} ({telegram_id})")
    return True


# ============================================================================
# CAFE REGISTRATION OPERATIONS
# ============================================================================


@retry_on_quota_error()
def register_cafe_chat(
    chat_id: int,
    chat_title: str,
    brothers_topic_id: int,
    sisters_topic_id: int,
) -> bool:
    """Register a chat for cafe order notifications.

    Args:
        chat_id: The Telegram chat ID.
        chat_title: The chat title.
        brothers_topic_id: Topic ID for brothers orders.
        sisters_topic_id: Topic ID for sisters orders.

    Returns:
        True if successful, False otherwise.
    """
    client = get_gspread_client()
    spreadsheet = client.open_by_key(SPREADSHEET_ID)

    try:
        chats_sheet = spreadsheet.worksheet("cafe_registered")
    except gspread.exceptions.WorksheetNotFound:
        # Create the sheet with headers
        chats_sheet = spreadsheet.add_worksheet(title="cafe_registered", rows=100, cols=5)
        chats_sheet.append_row([
            "chat_id",
            "chat_title",
            "brothers_topic_id",
            "sisters_topic_id",
            "registered_at",
        ])
        logger.info("Created 'cafe_registered' sheet")

    # Check if already registered
    existing = get_registered_cafe_chat(chat_id)
    if existing:
        logger.info(f"Chat {chat_id} already registered")
        return True

    # Add new registration
    registered_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    chats_sheet.append_row([
        chat_id,
        chat_title,
        brothers_topic_id,
        sisters_topic_id,
        registered_at,
    ])

    # Invalidate cache
    invalidate_cache(f"cafe_chat:{chat_id}")
    invalidate_cache("cafe_chats:all")

    logger.info(f"Registered cafe chat: {chat_title} ({chat_id})")
    return True


@retry_on_quota_error()
def get_registered_cafe_chat(chat_id: int) -> dict | None:
    """Get a registered cafe chat by ID.

    Args:
        chat_id: The Telegram chat ID.

    Returns:
        Dictionary with chat info or None if not found.
    """
    cache_key = f"cafe_chat:{chat_id}"
    cached = get_cached(cache_key)
    if cached is not None:
        return cached if cached != "NOT_FOUND" else None

    client = get_gspread_client()
    spreadsheet = client.open_by_key(SPREADSHEET_ID)

    try:
        chats_sheet = spreadsheet.worksheet("cafe_registered")
    except gspread.exceptions.WorksheetNotFound:
        set_cached(cache_key, "NOT_FOUND")
        return None

    all_chats = chats_sheet.get_all_records()

    for chat in all_chats:
        if int(chat.get("chat_id", 0)) == chat_id:
            result = {
                "chat_id": int(chat["chat_id"]),
                "chat_title": str(chat.get("chat_title", "")),
                "brothers_topic_id": int(chat.get("brothers_topic_id", 0)),
                "sisters_topic_id": int(chat.get("sisters_topic_id", 0)),
            }
            set_cached(cache_key, result)
            return result

    set_cached(cache_key, "NOT_FOUND")
    return None


@retry_on_quota_error()
def deregister_cafe_chat(chat_id: int) -> bool:
    """Deregister a chat from cafe order notifications.

    Args:
        chat_id: The Telegram chat ID to deregister.

    Returns:
        True if successfully deregistered, False if not found or error.
    """
    client = get_gspread_client()
    spreadsheet = client.open_by_key(SPREADSHEET_ID)

    try:
        chats_sheet = spreadsheet.worksheet("cafe_registered")
    except gspread.exceptions.WorksheetNotFound:
        logger.warning("'cafe_registered' sheet not found")
        return False

    all_chats = chats_sheet.get_all_records()

    # Find the row to delete (1-indexed, +1 for header)
    for idx, chat in enumerate(all_chats):
        if int(chat.get("chat_id", 0)) == chat_id:
            row_number = idx + 2  # +1 for 0-index, +1 for header row
            chats_sheet.delete_rows(row_number)

            # Invalidate cache
            invalidate_cache(f"cafe_chat:{chat_id}")
            invalidate_cache("cafe_chats:all")

            logger.info(f"Deregistered cafe chat: {chat_id}")
            return True

    logger.warning(f"Chat {chat_id} not found for deregistration")
    return False


@retry_on_quota_error()
def get_all_registered_cafe_chats() -> list[dict]:
    """Get all registered cafe chats.

    Returns:
        List of registered chat dictionaries.
    """
    cache_key = "cafe_chats:all"
    cached = get_cached(cache_key)
    if cached is not None:
        return cached

    client = get_gspread_client()
    spreadsheet = client.open_by_key(SPREADSHEET_ID)

    try:
        chats_sheet = spreadsheet.worksheet("cafe_registered")
    except gspread.exceptions.WorksheetNotFound:
        return []

    all_chats = chats_sheet.get_all_records()

    result = [
        {
            "chat_id": int(chat["chat_id"]),
            "chat_title": str(chat.get("chat_title", "")),
            "brothers_topic_id": int(chat.get("brothers_topic_id", 0)),
            "sisters_topic_id": int(chat.get("sisters_topic_id", 0)),
        }
        for chat in all_chats
        if chat.get("chat_id")
    ]

    set_cached(cache_key, result, ttl=120)
    return result


# ============================================================================
# CAFE STATE MANAGEMENT
# ============================================================================


def get_cafe_state() -> dict[str, bool]:
    """Get the current cafe open/closed state.

    Returns:
        Dict with {"brothers": bool, "sisters": bool} indicating if each side is open.
    """
    with _cafe_state_lock:
        return _cafe_state.copy()


def is_cafe_open_for(section: str) -> bool:
    """Check if the cafe is open for a specific section.

    Args:
        section: The section to check ("brothers", "sisters", or "general").

    Returns:
        True if the cafe is accepting orders for that section.
    """
    with _cafe_state_lock:
        section_lower = section.lower()
        if section_lower in ("brothers", "brother"):
            return _cafe_state["brothers"]
        elif section_lower in ("sisters", "sister"):
            return _cafe_state["sisters"]
        else:
            # General items require at least one side to be open
            return _cafe_state["brothers"] or _cafe_state["sisters"]


def open_cafe(brothers: bool = True, sisters: bool = True) -> dict[str, bool]:
    """Open the cafe for orders.

    Args:
        brothers: Whether to open the brothers side.
        sisters: Whether to open the sisters side.

    Returns:
        The new cafe state.
    """
    with _cafe_state_lock:
        if brothers:
            _cafe_state["brothers"] = True
        if sisters:
            _cafe_state["sisters"] = True
        logger.info(f"Cafe opened: brothers={_cafe_state['brothers']}, sisters={_cafe_state['sisters']}")
        return _cafe_state.copy()


def close_cafe(brothers: bool = True, sisters: bool = True) -> dict[str, bool]:
    """Close the cafe for orders.

    Args:
        brothers: Whether to close the brothers side.
        sisters: Whether to close the sisters side.

    Returns:
        The new cafe state.
    """
    with _cafe_state_lock:
        if brothers:
            _cafe_state["brothers"] = False
        if sisters:
            _cafe_state["sisters"] = False
        logger.info(f"Cafe closed: brothers={_cafe_state['brothers']}, sisters={_cafe_state['sisters']}")
        return _cafe_state.copy()


# ============================================================================
# MENU OPERATIONS
# ============================================================================


@retry_on_quota_error()
def get_menu_items() -> list[dict]:
    """Get all available menu items from the 'menu' sheet.

    Sheet schema: item, price, gender, description

    Returns:
        List of menu item dictionaries.
    """
    cache_key = "menu:items"
    cached = get_cached(cache_key)
    if cached is not None:
        return cached

    client = get_gspread_client()
    spreadsheet = client.open_by_key(SPREADSHEET_ID)

    try:
        menu_sheet = spreadsheet.worksheet("menu")
    except gspread.exceptions.WorksheetNotFound:
        logger.error("'menu' sheet not found in spreadsheet")
        return []

    all_items = menu_sheet.get_all_records()

    # Process items and add IDs
    menu_items = []
    for idx, item in enumerate(all_items):
        try:
            price = float(item.get("price", 0))
        except (ValueError, TypeError):
            price = 0.0

        menu_items.append({
            "item_id": str(idx + 1),  # Simple ID based on row
            "item": str(item.get("item", "")).strip(),
            "price": price,
            "gender": str(item.get("gender", "")).strip(),
            "description": str(item.get("description", "")).strip(),
        })

    # Filter out empty items
    menu_items = [i for i in menu_items if i["item"]]

    set_cached(cache_key, menu_items, ttl=300)  # Cache for 5 minutes
    logger.info(f"Loaded {len(menu_items)} menu items")

    return menu_items


# ============================================================================
# ORDER OPERATIONS (Using OrdersCache)
# ============================================================================


def create_order(
    telegram_id: int,
    telegram_name: str,
    item: str,
    price: float,
    gender: str = "",
    notes: str = "",
) -> str | None:
    """Create a new order via the cache (batched write to Sheets).

    Args:
        telegram_id: Customer's Telegram user ID.
        telegram_name: Customer's display name.
        item: Name of the ordered item.
        price: Price of the item.
        gender: Gender category of the item (brothers/sisters/general).
        notes: Special instructions for the order.

    Returns:
        Order ID if successful, None otherwise.
    """
    try:
        order_id = orders_cache.add_order(
            telegram_id=telegram_id,
            telegram_name=telegram_name,
            item=item,
            price=price,
            gender=gender,
            notes=notes,
        )
        logger.info(f"Created order {order_id} for {telegram_name}: {item}")
        return order_id
    except Exception as e:
        logger.error(f"Failed to create order: {e}")
        return None


def get_pending_orders() -> list[dict]:
    """Get all pending orders from the cache.

    Returns:
        List of pending order dictionaries.
    """
    return orders_cache.get_pending_orders()


def get_orders_for_user(telegram_id: int) -> list[dict]:
    """Get all orders for a specific user from the cache.

    Args:
        telegram_id: The user's Telegram ID.

    Returns:
        List of order dictionaries for the user, sorted by most recent first.
    """
    return orders_cache.get_orders_for_user(telegram_id)


@retry_on_quota_error()
def mark_order_ready(order_id: str) -> dict | None:
    """Mark an order as ready and return customer info for notification.

    Args:
        order_id: The order ID to mark as ready.

    Returns:
        Dictionary with telegram_id, item, gender, and notes, or None if not found.
    """
    result = orders_cache.update_order_status(order_id, "ready")
    if result:
        gender_value = result.get("gender", "")
        logger.info(
            f"mark_order_ready {order_id}: gender='{gender_value}', "
            f"full_result_keys={list(result.keys())}"
        )
        return {
            "telegram_id": result.get("telegram_id"),
            "item": result.get("item"),
            "gender": gender_value,
            "notes": result.get("notes"),
        }
    return None


def mark_order_completed(order_id: str) -> dict | None:
    """Mark an order as completed via the cache (batched write to Sheets).

    Args:
        order_id: The order ID to mark as completed.

    Returns:
        Dictionary with order info if successful, None otherwise.
    """
    result = orders_cache.update_order_status(order_id, "completed")
    if result:
        logger.info(f"Marked order {order_id} as completed (via cache)")
        return {
            "telegram_id": result.get("telegram_id"),
            "telegram_name": result.get("telegram_name"),
            "item": result.get("item"),
            "gender": result.get("gender"),
            "already_completed": result.get("already_completed", False),
        }
    logger.error(f"Order {order_id} not found in cache")
    return None


def mark_order_denied(order_id: str) -> dict | None:
    """Mark an order as denied via the cache (batched write to Sheets).

    Args:
        order_id: The order ID to mark as denied.

    Returns:
        Dictionary with order info if successful, None otherwise.
    """
    result = orders_cache.update_order_status(order_id, "denied")
    if result:
        logger.info(f"Marked order {order_id} as denied (via cache)")
        return {
            "telegram_id": result.get("telegram_id"),
            "telegram_name": result.get("telegram_name"),
            "item": result.get("item"),
            "already_processed": result.get("already_processed", False),
        }
    logger.error(f"Order {order_id} not found in cache")
    return None


# ============================================================================
# UNIFIED SYNC PROCESSOR
# ============================================================================


def _perform_unified_sync() -> dict:
    """Perform a unified sync operation (flush writes + refresh cache).

    Returns:
        Dict with sync statistics.
    """
    stats = {
        "new_orders_written": 0,
        "status_updates_written": 0,
        "cache_entries": 0,
        "success": True,
        "error": None,
    }

    try:
        client = get_gspread_client()

        # 1. Flush pending writes to Google Sheets
        flush_result = orders_cache.flush_pending_writes(client, SPREADSHEET_ID)
        stats["new_orders_written"] = flush_result["new_orders_written"]
        stats["status_updates_written"] = flush_result["status_updates_written"]

        if not flush_result["success"]:
            stats["success"] = False
            stats["error"] = flush_result["error"]

        # 2. Refresh cache from Google Sheets
        cache_count = orders_cache.refresh_from_sheet(client, SPREADSHEET_ID)
        stats["cache_entries"] = cache_count

    except Exception as e:
        stats["success"] = False
        stats["error"] = str(e)
        logger.error(f"Unified sync failed: {e}")

    return stats


async def unified_sync_processor():
    """Single background processor that handles both writes and cache refresh.

    This handles:
    1. Flush pending writes from orders_cache to Google Sheets
    2. Refresh the orders_cache from Google Sheets

    This ensures the cache is always consistent with the sheet state.
    """
    sync_interval = orders_cache.sync_interval  # 10 seconds
    logger.info(f"Unified sync processor started (interval: {sync_interval}s)")

    # Initial sync - load existing orders into cache
    try:
        stats = await asyncio.get_event_loop().run_in_executor(
            None, _perform_unified_sync
        )
        logger.info(
            f"Initial sync complete: {stats['cache_entries']} orders loaded"
        )
    except Exception as e:
        logger.error(f"Failed to perform initial sync: {e}")
        orders_cache._initialized = True  # Allow bot to start anyway

    # Periodic sync loop
    try:
        while True:
            await asyncio.sleep(sync_interval)

            try:
                stats = await asyncio.get_event_loop().run_in_executor(
                    None, _perform_unified_sync
                )
                # Only log if there were writes
                if stats["new_orders_written"] > 0 or stats["status_updates_written"] > 0:
                    logger.info(
                        f"Sync: wrote {stats['new_orders_written']} orders, "
                        f"{stats['status_updates_written']} updates, "
                        f"refreshed {stats['cache_entries']} cache entries"
                    )
            except asyncio.CancelledError:
                raise  # Re-raise to exit the loop
            except Exception as e:
                logger.error(f"Error in unified sync: {e}")

    except asyncio.CancelledError:
        # Graceful shutdown - perform final sync
        logger.info("Unified sync processor shutting down, performing final sync...")
        try:
            stats = _perform_unified_sync()
            logger.info(
                f"Final sync complete: {stats['new_orders_written']} orders, "
                f"{stats['status_updates_written']} updates written"
            )
        except Exception as e:
            logger.error(f"Final sync failed: {e}")
        raise  # Re-raise so the task is properly marked as cancelled
