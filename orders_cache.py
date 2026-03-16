"""Orders Cache system for the MAPS Seattle Cafe Telegram Bot.

The OrdersCache is the unified data structure that handles:
1. Fast reads - Order lookups, status checks (no API calls)
2. Batched writes - New orders and status updates are flushed to Sheets periodically
3. Graceful shutdown - Flush pending writes before exit to prevent data loss

This prevents rate limiting from the Google Sheets API during high-traffic periods.

Optimized for high concurrency (40+ simultaneous orders).

Usage:
    from orders_cache import OrdersCache

    # Create instance (typically done in google_sheets_operations.py)
    cache = OrdersCache(sync_interval=5.0)  # 5 seconds for faster persistence

    # Add an order (will be flushed on next sync)
    order_id = cache.add_order(telegram_id, telegram_name, item, price, gender)

    # Update order status
    cache.update_order_status(order_id, "completed")

    # Get order by ID
    order = cache.get_order(order_id)
"""

import gspread
import threading
import time
from datetime import datetime
from uuid import uuid4

from logger import setup_logger

logger = setup_logger("maps_cafe_bot.orders_cache")


def _escape_sheet_value(value: str) -> str:
    """Escape a value to prevent Google Sheets from interpreting it as a formula.

    Args:
        value: The string value to escape.

    Returns:
        The escaped value (prefixed with ' if it starts with formula characters).
    """
    if value and value[0] in ('=', '+', '-', '@'):
        return "'" + value
    return value


class OrdersCache:
    """Unified in-memory cache for order data with background sync.

    Optimized for high concurrency with:
    - RLock for reentrant locking (prevents deadlocks)
    - Minimal time holding locks
    - Batched operations for efficiency

    This single data structure handles both reads AND writes:
    1. Fast reads - Order lookups, status checks (no API calls)
    2. Batched writes - New orders and updates are flushed to Sheets periodically
    3. Background refresh - Syncs with Sheets every N seconds

    Memory usage: ~100 bytes per order (~4KB for 40 orders)
    API usage: ~12 calls/minute (unified sync every 5s)
    Concurrency: Handles 40+ simultaneous orders
    """

    def __init__(self, sync_interval: float = 5.0):
        """Initialize the orders cache.

        Args:
            sync_interval: Seconds between sync operations (default 5s for faster persistence).
        """
        self.sync_interval = sync_interval
        # Use RLock for reentrant locking - safer for complex operations
        self._lock = threading.RLock()

        # Main storage: {order_id: order_dict}
        # order_dict contains: telegram_id, telegram_name, item, price, gender, status, created_at, completed_at
        self._orders: dict[str, dict] = {}

        # Pending new orders - list of orders to be appended to the sheet
        # Each entry is a full order dict
        self._pending_new_orders: list[dict] = []

        # Pending status updates - dict of {order_id: {status, completed_at}}
        # These are updates to existing orders (complete/deny/ready)
        self._pending_status_updates: dict[str, dict] = {}

        # Track when cache was last synced
        self._last_sync: float = 0.0

        # Flag to indicate if cache has been initialized
        self._initialized: bool = False

        # Performance tracking
        self._orders_processed: int = 0
        self._peak_pending: int = 0

    def is_initialized(self) -> bool:
        """Check if the cache has been initialized with data."""
        with self._lock:
            return self._initialized

    def add_order(
        self,
        telegram_id: int,
        telegram_name: str,
        item: str,
        price: float,
        gender: str = "",
        notes: str = "",
    ) -> str:
        """Add a new order to the cache (thread-safe, optimized for high concurrency).

        The order will be written to Google Sheets on the next sync.

        Args:
            telegram_id: Customer's Telegram user ID.
            telegram_name: Customer's display name.
            item: Name of the ordered item.
            price: Price of the item.
            gender: Gender category (brothers/sisters/general).
            notes: Special instructions for the order.

        Returns:
            The generated order ID.
        """
        # Generate order data OUTSIDE the lock to minimize lock time
        order_id = str(uuid4())[:8].upper()
        created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        order = {
            "order_id": order_id,
            "telegram_id": telegram_id,
            "telegram_name": telegram_name,
            "item": item,
            "price": price,
            "gender": gender,
            "notes": notes,
            "status": "pending",
            "created_at": created_at,
            "completed_at": "",
        }

        # Minimal time in lock - just dict assignments
        with self._lock:
            self._orders[order_id] = order.copy()
            self._pending_new_orders.append(order)
            pending_count = len(self._pending_new_orders)
            self._orders_processed += 1
            if pending_count > self._peak_pending:
                self._peak_pending = pending_count

        logger.info(
            f"Cache add_order: {order_id} for {telegram_name} - {item} "
            f"(gender={gender}, notes={notes[:20] if notes else 'none'}, pending={pending_count})"
        )
        return order_id

    def update_order_status(
        self,
        order_id: str,
        status: str,
        completed_at: str | None = None,
    ) -> dict | None:
        """Update an order's status in the cache (thread-safe).

        The update will be written to Google Sheets on the next sync.

        Args:
            order_id: The order ID to update.
            status: New status (completed/denied/ready).
            completed_at: Completion timestamp (auto-generated if None).

        Returns:
            The order dict if found, None otherwise.
        """
        # Generate timestamp outside lock
        if completed_at is None:
            completed_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        with self._lock:
            if order_id not in self._orders:
                logger.warning(f"Order {order_id} not found in cache for status update")
                return None

            order = self._orders[order_id]
            current_status = order.get("status", "").lower()

            # Check if already processed
            if current_status in ("completed", "denied"):
                logger.info(f"Order {order_id} already {current_status}")
                return {
                    **order,
                    "already_completed": current_status == "completed",
                    "already_processed": True,
                }

            # Update the order in cache
            order["status"] = status
            order["completed_at"] = completed_at

            # Queue the status update for sheet
            self._pending_status_updates[order_id] = {
                "status": status,
                "completed_at": completed_at,
            }

            result = {
                **order,
                "already_completed": False,
                "already_processed": False,
            }

        logger.info(
            f"Cache update_order_status: {order_id} -> {status} "
            f"(pending_updates={len(self._pending_status_updates)})"
        )

        return result

    def get_order(self, order_id: str) -> dict | None:
        """Get an order by ID from the cache (thread-safe).

        Args:
            order_id: The order ID to look up.

        Returns:
            The order dict if found, None otherwise.
        """
        with self._lock:
            order = self._orders.get(order_id)
            return order.copy() if order else None

    def get_pending_orders(self) -> list[dict]:
        """Get all orders with pending status (thread-safe).

        Returns:
            List of pending order dicts.
        """
        with self._lock:
            return [
                order.copy()
                for order in self._orders.values()
                if order.get("status", "").lower() == "pending"
            ]

    def get_orders_for_user(self, telegram_id: int) -> list[dict]:
        """Get all orders for a specific user (thread-safe).

        Args:
            telegram_id: The user's Telegram ID.

        Returns:
            List of order dicts for the user, sorted by created_at descending.
        """
        with self._lock:
            user_orders = [
                order.copy()
                for order in self._orders.values()
                if order.get("telegram_id") == telegram_id
            ]
            # Sort by created_at descending (most recent first)
            user_orders.sort(key=lambda x: x.get("created_at", ""), reverse=True)
            return user_orders

    def get_all_orders(self) -> list[dict]:
        """Get all orders from the cache (thread-safe).

        Returns:
            List of all order dicts, sorted by created_at descending.
        """
        with self._lock:
            all_orders = [order.copy() for order in self._orders.values()]
            # Sort by created_at descending (most recent first)
            all_orders.sort(key=lambda x: x.get("created_at", ""), reverse=True)
            return all_orders

    def get_pending_new_orders(self) -> list[dict]:
        """Get all new orders that need to be written to Sheets.

        Returns:
            List of new order dicts.
        """
        with self._lock:
            return list(self._pending_new_orders)

    def get_pending_status_updates(self) -> dict[str, dict]:
        """Get all status updates that need to be written to Sheets.

        Returns:
            Dict of {order_id: {status, completed_at}}.
        """
        with self._lock:
            return dict(self._pending_status_updates)

    def mark_new_orders_synced(self) -> int:
        """Clear pending new orders after successful write to Sheets.

        Returns:
            Number of orders cleared.
        """
        with self._lock:
            count = len(self._pending_new_orders)
            self._pending_new_orders.clear()
            return count

    def mark_status_updates_synced(self, synced_order_ids: list[str]) -> int:
        """Clear specific status updates after successful write to Sheets.

        Args:
            synced_order_ids: List of order IDs that were successfully synced.

        Returns:
            Number of updates cleared.
        """
        with self._lock:
            count = 0
            for order_id in synced_order_ids:
                if order_id in self._pending_status_updates:
                    del self._pending_status_updates[order_id]
                    count += 1
            return count

    def get_pending_count(self) -> tuple[int, int]:
        """Get the count of pending writes.

        Returns:
            Tuple of (new_orders_count, status_updates_count).
        """
        with self._lock:
            return len(self._pending_new_orders), len(self._pending_status_updates)

    def refresh_from_sheet(self, client, spreadsheet_id: str) -> int:
        """Refresh the cache from Google Sheets.

        Note: Pending writes are preserved during refresh.

        Args:
            client: The gspread client to use for API calls.
            spreadsheet_id: The Google Sheets spreadsheet ID.

        Returns:
            Number of orders loaded into cache.
        """
        try:
            spreadsheet = client.open_by_key(spreadsheet_id)
            try:
                orders_sheet = spreadsheet.worksheet("orders")
            except Exception:
                logger.warning("'orders' sheet not found")
                with self._lock:
                    self._initialized = True
                return 0

            # Fetch data OUTSIDE the lock
            all_orders = orders_sheet.get_all_records()

            # Process data and update cache with minimal lock time
            with self._lock:
                # Clear existing cache (but preserve pending writes)
                self._orders.clear()

                # Load orders from sheet
                count = 0
                for order in all_orders:
                    order_id = str(order.get("order_id", ""))
                    if not order_id:
                        continue

                    self._orders[order_id] = {
                        "order_id": order_id,
                        "telegram_id": int(order.get("telegram_id", 0)),
                        "telegram_name": str(order.get("telegram_name", "")),
                        "item": str(order.get("item", "")),
                        "price": float(order.get("price", 0)),
                        "gender": str(order.get("gender", "")),
                        "notes": str(order.get("notes", "")),
                        "status": str(order.get("status", "")).lower(),
                        "created_at": str(order.get("created_at", "")),
                        "completed_at": str(order.get("completed_at", "")),
                    }
                    count += 1

                # Re-apply pending new orders to cache (so reads work)
                for pending_order in self._pending_new_orders:
                    self._orders[pending_order["order_id"]] = pending_order.copy()

                # Re-apply pending status updates to cache
                for order_id, update in self._pending_status_updates.items():
                    if order_id in self._orders:
                        self._orders[order_id]["status"] = update["status"]
                        self._orders[order_id]["completed_at"] = update["completed_at"]

                self._last_sync = time.time()
                self._initialized = True

            logger.info(f"Orders cache refreshed: {count} entries loaded from sheet")
            return count

        except Exception as e:
            logger.error(f"Failed to refresh orders cache: {e}")
            with self._lock:
                self._initialized = True  # Allow operations to continue
            return 0

    def flush_pending_writes(self, client, spreadsheet_id: str) -> dict:
        """Flush all pending writes to Google Sheets (optimized for batch operations).

        Args:
            client: The gspread client to use for API calls.
            spreadsheet_id: The Google Sheets spreadsheet ID.

        Returns:
            Dict with {new_orders_written, status_updates_written, success, error}.
        """
        result = {
            "new_orders_written": 0,
            "status_updates_written": 0,
            "success": True,
            "error": None,
        }

        # Get pending data with minimal lock time
        new_orders = self.get_pending_new_orders()
        status_updates = self.get_pending_status_updates()

        if not new_orders and not status_updates:
            return result

        logger.info(
            f"Flushing {len(new_orders)} new orders and {len(status_updates)} status updates"
        )

        try:
            spreadsheet = client.open_by_key(spreadsheet_id)

            try:
                orders_sheet = spreadsheet.worksheet("orders")
            except Exception:
                # Create the orders sheet with headers (including notes column)
                orders_sheet = spreadsheet.add_worksheet(
                    title="orders", rows=1000, cols=10
                )
                orders_sheet.append_row([
                    "order_id",
                    "telegram_id",
                    "telegram_name",
                    "item",
                    "price",
                    "gender",
                    "notes",
                    "status",
                    "created_at",
                    "completed_at",
                ])
                logger.info("Created 'orders' sheet")

            # Write new orders (batch append - single API call for all orders)
            if new_orders:
                rows = [
                    [
                        order["order_id"],
                        order["telegram_id"],
                        order["telegram_name"],
                        order["item"],
                        order["price"],
                        order["gender"],
                        _escape_sheet_value(order.get("notes", "")),
                        order["status"],
                        order["created_at"],
                        order["completed_at"],
                    ]
                    for order in new_orders
                ]
                orders_sheet.append_rows(rows, value_input_option="RAW")
                self.mark_new_orders_synced()
                result["new_orders_written"] = len(new_orders)
                logger.info(f"Wrote {len(new_orders)} new orders to sheet (batch)")

            # Apply status updates using batch update for efficiency
            if status_updates:
                all_orders = orders_sheet.get_all_records()
                headers = orders_sheet.row_values(1)

                try:
                    status_col = headers.index("status") + 1
                    completed_at_col = headers.index("completed_at") + 1
                except ValueError as e:
                    logger.error(f"Missing required column in orders sheet: {e}")
                    result["success"] = False
                    result["error"] = str(e)
                    return result

                # Build batch update cells
                cells_to_update = []
                synced_order_ids = []

                for idx, order in enumerate(all_orders):
                    order_id = str(order.get("order_id", ""))
                    if order_id in status_updates:
                        row_num = idx + 2  # +1 for header, +1 for 1-based index
                        update = status_updates[order_id]
                        cells_to_update.append({
                            "row": row_num,
                            "status": update["status"],
                            "completed_at": update["completed_at"],
                        })
                        synced_order_ids.append(order_id)

                # Batch update - fewer API calls
                if cells_to_update:
                    # Use batch_update for efficiency (2 API calls instead of 2*N)
                    status_cells = []
                    completed_cells = []
                    for cell in cells_to_update:
                        status_cells.append(gspread.Cell(cell["row"], status_col, cell["status"]))
                        completed_cells.append(gspread.Cell(cell["row"], completed_at_col, cell["completed_at"]))

                    orders_sheet.update_cells(status_cells + completed_cells)

                self.mark_status_updates_synced(synced_order_ids)
                result["status_updates_written"] = len(synced_order_ids)
                logger.info(f"Updated {len(synced_order_ids)} order statuses in sheet (batch)")

            return result

        except Exception as e:
            # CRITICAL: Pending writes remain in memory and will retry on next sync
            logger.error(
                f"CRITICAL: Failed to flush pending writes to Google Sheets. "
                f"Data at risk if bot crashes! Error: {e}"
            )
            result["success"] = False
            result["error"] = str(e)
            return result

    def get_stats(self) -> dict:
        """Get cache statistics for monitoring."""
        with self._lock:
            total_orders = len(self._orders)
            pending_orders = sum(
                1 for o in self._orders.values() if o.get("status") == "pending"
            )
            pending_new = len(self._pending_new_orders)
            pending_updates = len(self._pending_status_updates)
            age_seconds = time.time() - self._last_sync if self._last_sync else None

            return {
                "total_orders": total_orders,
                "pending_orders": pending_orders,
                "pending_new_writes": pending_new,
                "pending_status_updates": pending_updates,
                "initialized": self._initialized,
                "cache_age_seconds": age_seconds,
                "total_orders_processed": self._orders_processed,
                "peak_pending_orders": self._peak_pending,
            }
