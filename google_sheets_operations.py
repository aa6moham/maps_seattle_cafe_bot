"""Google Sheets operations for MAPS Cafe Bot.

Handles menu reading and order management via Google Sheets API.
"""

import time
from datetime import datetime
from functools import wraps
from threading import Lock
from uuid import uuid4

import gspread
from google.oauth2.service_account import Credentials

from logger import setup_logger
from private.constants import SPREADSHEET_ID

logger = setup_logger(__name__)

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
# MENU OPERATIONS
# ============================================================================


@retry_on_quota_error()
def get_menu_items() -> list[dict]:
    """Get all available menu items from the 'menu' sheet.

    Sheet schema: item, price, gender

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
        })

    # Filter out empty items
    menu_items = [i for i in menu_items if i["item"]]

    set_cached(cache_key, menu_items, ttl=300)  # Cache for 5 minutes
    logger.info(f"Loaded {len(menu_items)} menu items")

    return menu_items


# ============================================================================
# ORDER OPERATIONS
# ============================================================================


@retry_on_quota_error()
def create_order(
    telegram_id: int,
    telegram_name: str,
    item: str,
    price: float,
) -> str | None:
    """Create a new order in the 'orders' sheet.

    Args:
        telegram_id: Customer's Telegram user ID.
        telegram_name: Customer's display name.
        item: Name of the ordered item.
        price: Price of the item.

    Returns:
        Order ID if successful, None otherwise.
    """
    client = get_gspread_client()
    spreadsheet = client.open_by_key(SPREADSHEET_ID)

    try:
        orders_sheet = spreadsheet.worksheet("orders")
    except gspread.exceptions.WorksheetNotFound:
        # Create the orders sheet with headers
        orders_sheet = spreadsheet.add_worksheet(title="orders", rows=1000, cols=8)
        orders_sheet.append_row([
            "order_id",
            "telegram_id",
            "telegram_name",
            "item",
            "price",
            "status",
            "created_at",
            "completed_at",
        ])
        logger.info("Created 'orders' sheet")

    # Generate order ID
    order_id = str(uuid4())[:8].upper()
    created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Append order row
    orders_sheet.append_row([
        order_id,
        telegram_id,
        telegram_name,
        item,
        price,
        "pending",
        created_at,
        "",  # completed_at
    ])

    logger.info(f"Created order {order_id} for {telegram_name}: {item}")
    return order_id


@retry_on_quota_error()
def get_pending_orders() -> list[dict]:
    """Get all pending orders.

    Returns:
        List of pending order dictionaries.
    """
    client = get_gspread_client()
    spreadsheet = client.open_by_key(SPREADSHEET_ID)

    try:
        orders_sheet = spreadsheet.worksheet("orders")
    except gspread.exceptions.WorksheetNotFound:
        return []

    all_orders = orders_sheet.get_all_records()

    pending = [
        {
            "order_id": str(order.get("order_id", "")),
            "telegram_id": int(order.get("telegram_id", 0)),
            "telegram_name": str(order.get("telegram_name", "")),
            "item": str(order.get("item", "")),
            "price": float(order.get("price", 0)),
            "created_at": str(order.get("created_at", "")),
        }
        for order in all_orders
        if str(order.get("status", "")).lower() == "pending"
    ]

    return pending


@retry_on_quota_error()
def mark_order_ready(order_id: str) -> dict | None:
    """Mark an order as ready and return customer info for notification.

    Args:
        order_id: The order ID to mark as ready.

    Returns:
        Dictionary with telegram_id and item, or None if not found.
    """
    client = get_gspread_client()
    spreadsheet = client.open_by_key(SPREADSHEET_ID)

    try:
        orders_sheet = spreadsheet.worksheet("orders")
    except gspread.exceptions.WorksheetNotFound:
        return None

    all_orders = orders_sheet.get_all_records()
    headers = orders_sheet.row_values(1)

    try:
        status_col = headers.index("status") + 1
    except ValueError:
        return None

    for idx, order in enumerate(all_orders):
        if str(order.get("order_id", "")) == str(order_id):
            row_num = idx + 2  # +1 for header, +1 for 1-based index

            # Update status to 'ready'
            orders_sheet.update_cell(row_num, status_col, "ready")

            logger.info(f"Marked order {order_id} as ready")

            return {
                "telegram_id": int(order.get("telegram_id", 0)),
                "item": str(order.get("item", "")),
            }

    return None


@retry_on_quota_error()
def mark_order_completed(order_id: str) -> bool:
    """Mark an order as completed.

    Args:
        order_id: The order ID to mark as completed.

    Returns:
        True if successful, False otherwise.
    """
    client = get_gspread_client()
    spreadsheet = client.open_by_key(SPREADSHEET_ID)

    try:
        orders_sheet = spreadsheet.worksheet("orders")
    except gspread.exceptions.WorksheetNotFound:
        return False

    all_orders = orders_sheet.get_all_records()
    headers = orders_sheet.row_values(1)

    try:
        status_col = headers.index("status") + 1
        completed_at_col = headers.index("completed_at") + 1
    except ValueError:
        return False

    for idx, order in enumerate(all_orders):
        if str(order.get("order_id", "")) == str(order_id):
            row_num = idx + 2

            completed_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            orders_sheet.update_cell(row_num, status_col, "completed")
            orders_sheet.update_cell(row_num, completed_at_col, completed_at)

            logger.info(f"Marked order {order_id} as completed")
            return True

    return False
