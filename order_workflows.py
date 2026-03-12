"""Order workflow handlers for MAPS Cafe Bot.

This module provides separate workflow classes for brothers and sisters sections,
allowing for customized order flows, menus, and configurations per section.

The architecture separates concerns:
- BaseOrderWorkflow: Common functionality shared between both sections
- BrothersOrderWorkflow: Brothers-specific customizations
- SistersOrderWorkflow: Sisters-specific customizations

Each workflow can have:
- Custom menu filtering
- Custom pickup locations
- Custom notifications
- Custom payment handling (future)
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from google_sheets_operations import (
    create_order,
    get_cafe_state,
    get_menu_items,
)
from logger import setup_logger

logger = setup_logger(__name__)


# ============================================================================
# DATA CLASSES
# ============================================================================


@dataclass
class OrderData:
    """Data class representing an order in progress."""
    item_id: str
    item: str
    price: float
    section: str  # "brothers" or "sisters"
    description: str = ""
    notes: str = ""
    # Drink customizations
    shots: str = ""  # Number of shots (from menu num_shots column)
    decaf: str = ""  # Caffeine option (from menu caffeine_option column)
    temperature: str = ""  # Selected temperature option
    syrup: str = ""  # Selected syrup option
    # Available options from menu (for validation)
    temperature_options: list = None
    syrup_options: list = None
    caffeine_options: list = None
    shots_options: list = None

    def __post_init__(self):
        if self.temperature_options is None:
            self.temperature_options = []
        if self.syrup_options is None:
            self.syrup_options = []
        if self.caffeine_options is None:
            self.caffeine_options = []
        if self.shots_options is None:
            self.shots_options = []

    def to_dict(self) -> dict:
        """Convert to dictionary for storage in user_data."""
        return {
            "item_id": self.item_id,
            "item": self.item,
            "price": self.price,
            "section": self.section,
            "description": self.description,
            "notes": self.notes,
            "shots": self.shots,
            "decaf": self.decaf,
            "temperature": self.temperature,
            "syrup": self.syrup,
            "temperature_options": self.temperature_options,
            "syrup_options": self.syrup_options,
            "caffeine_options": self.caffeine_options,
            "shots_options": self.shots_options,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "OrderData":
        """Create from dictionary stored in user_data."""
        return cls(
            item_id=data.get("item_id", ""),
            item=data.get("item", ""),
            price=data.get("price", 0.0),
            section=data.get("section", "general"),
            description=data.get("description", ""),
            notes=data.get("notes", ""),
            shots=data.get("shots", ""),
            decaf=data.get("decaf", ""),
            temperature=data.get("temperature", ""),
            syrup=data.get("syrup", ""),
            temperature_options=data.get("temperature_options", []),
            syrup_options=data.get("syrup_options", []),
            caffeine_options=data.get("caffeine_options", []),
            shots_options=data.get("shots_options", []),
        )

    def get_customizations_summary(self) -> str:
        """Get a formatted summary of selected customizations."""
        parts = []
        if self.shots:
            parts.append(f"{self.shots} shot(s)")
        if self.decaf:
            parts.append(self.decaf)
        if self.temperature:
            parts.append(self.temperature)
        if self.syrup:
            parts.append(f"{self.syrup} syrup")
        return ", ".join(parts) if parts else ""

    def build_full_notes(self) -> str:
        """Build the full notes string including customizations and user notes."""
        customizations = self.get_customizations_summary()
        if customizations and self.notes:
            return f"{customizations} | {self.notes}"
        elif customizations:
            return customizations
        else:
            return self.notes


@dataclass
class SectionConfig:
    """Configuration for a section (brothers or sisters)."""
    name: str  # "brothers" or "sisters"
    display_name: str  # "🧔 Brothers" or "🧕 Sisters"
    emoji: str  # "🧔" or "🧕"
    pickup_location: str
    pickup_note: str
    # Future: Add payment config, custom messages, etc.


# ============================================================================
# SECTION CONFIGURATIONS
# ============================================================================


BROTHERS_CONFIG = SectionConfig(
    name="brothers",
    display_name="🧔 Brothers",
    emoji="🧔",
    pickup_location="Upstairs kitchen area (brothers section)",
    pickup_note="🧔 _Pickup: Upstairs kitchen area (brothers section)_",
)

SISTERS_CONFIG = SectionConfig(
    name="sisters",
    display_name="🧕 Sisters",
    emoji="🧕",
    pickup_location="Kitchen area (sisters section)",
    pickup_note="🧕 _Pickup: Kitchen area (sisters section)_",
)


# ============================================================================
# BASE WORKFLOW CLASS
# ============================================================================


class BaseOrderWorkflow(ABC):
    """Base class for order workflows.

    Provides common functionality and defines the interface for section-specific
    workflows. Subclasses can override methods to customize behavior.
    """

    def __init__(self, config: SectionConfig):
        self.config = config

    @property
    def section_name(self) -> str:
        """Return the section name (brothers/sisters)."""
        return self.config.name

    @property
    def display_name(self) -> str:
        """Return the display name with emoji."""
        return self.config.display_name

    def is_open(self) -> bool:
        """Check if this section is currently open."""
        cafe_state = get_cafe_state()
        return cafe_state.get(self.section_name, False)

    def get_menu_items(self) -> list[dict]:
        """Get menu items filtered for this section.

        Override in subclass for custom filtering logic.
        """
        all_items = get_menu_items()
        filtered = []

        for item in all_items:
            item_gender = item.get("gender", "").lower()

            # Include item if:
            # - It's for this section specifically
            # - It's for "both" genders
            # - It has no gender restriction (general item)
            if self._item_matches_section(item_gender):
                filtered.append(item)

        return filtered

    def _item_matches_section(self, item_gender: str) -> bool:
        """Check if an item's gender restriction matches this section."""
        section = self.section_name
        # Handle both "brothers" and "brother" forms
        section_singular = section.rstrip("s")

        if item_gender in (section, section_singular):
            return True
        if item_gender == "both":
            return True
        if item_gender == "":
            return True

        return False

    def get_pickup_note(self) -> str:
        """Get the pickup location note for this section."""
        return self.config.pickup_note

    def get_pickup_location(self) -> str:
        """Get the pickup location for this section."""
        return self.config.pickup_location

    async def show_menu(
        self,
        query,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """Display the menu for this section.

        Override in subclass for custom menu display.
        """
        menu_items = self.get_menu_items()

        if not menu_items:
            await query.edit_message_text(
                f"😔 Sorry, there are no menu items available for "
                f"{self.display_name} right now."
            )
            return

        # Build keyboard with menu items
        keyboard = self._build_menu_keyboard(menu_items)

        await query.edit_message_text(
            f"🕌 *MAPS Masjid Cafe Menu*\n\n"
            f"*Section:* {self.display_name}\n"
            "_All proceeds go to support our masjid!_\n\n"
            "Tap an item to order:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown",
        )

    def _build_menu_keyboard(self, menu_items: list[dict]) -> list[list]:
        """Build the keyboard layout for menu items.

        Override in subclass for custom layout.
        """
        keyboard = []

        for item in menu_items:
            button_text = f"{item['item']} - ${item['price']:.2f}"
            callback_data = f"menu:{item['item_id']}:{self.section_name}"
            keyboard.append([InlineKeyboardButton(button_text, callback_data=callback_data)])

        # Add back button
        keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data="gender:back")])

        return keyboard

    def build_order_details(self, order_data: OrderData) -> str:
        """Build the order details message.

        Override in subclass for custom formatting.
        """
        item_details = f"*{order_data.item}*\n💰 *Price:* ${order_data.price:.2f}"

        if order_data.description:
            item_details += f"\n\n📝 _{order_data.description}_"

        item_details += f"\n\n{self.get_pickup_note()}"

        return item_details

    def build_confirmation_message(self, order_data: OrderData, order_id: str) -> str:
        """Build the order confirmation message.

        Override in subclass for custom confirmation.
        """
        pickup_location = f"{self.config.emoji} *Pickup Location:* {self.get_pickup_location()}"

        msg = (
            f"✅ *Order Placed!*\n\n"
            f"Your order for *{order_data.item}* has been enqueued.\n\n"
            f"{pickup_location}\n\n"
        )

        if order_data.notes:
            msg += f"📋 *Special Instructions:* _{order_data.notes}_\n\n"

        msg += (
            f"📩 You will receive a DM once your order is ready, In Shaa Allah.\n\n"
            f"Order ID: `{order_id}`\n\n"
            f"🤲 *JazakAllah Khair!* Your contribution supports our masjid. "
            f"May Allah bless you and your family!"
        )

        return msg

    def build_ready_message(self, order_data: dict) -> str:
        """Build the 'order ready' notification message.

        Override in subclass for custom notification.
        """
        item = order_data.get("item", "your item")
        pickup_location = self.get_pickup_location()

        return (
            f"✅ *Your Order is Ready!*\n\n"
            f"*{item}* is ready for pickup at:\n"
            f"{self.config.emoji} {pickup_location}\n\n"
            f"🤲 JazakAllah Khair!"
        )

    async def create_order(
        self,
        telegram_id: int,
        telegram_name: str,
        order_data: OrderData,
    ) -> str | None:
        """Create the order in the system.

        Override in subclass for custom order creation (e.g., payment flow).
        """
        return create_order(
            telegram_id=telegram_id,
            telegram_name=telegram_name,
            item=order_data.item,
            price=order_data.price,
            gender=self.section_name,
            notes=order_data.notes,
        )


# ============================================================================
# BROTHERS WORKFLOW
# ============================================================================


class BrothersOrderWorkflow(BaseOrderWorkflow):
    """Order workflow for brothers section.

    Brothers pay when they pick up their order (after receiving ready notification).
    """

    def __init__(self):
        super().__init__(BROTHERS_CONFIG)

    def build_confirmation_message(self, order_data: OrderData, order_id: str) -> str:
        """Build confirmation message with brothers-specific payment instructions."""
        pickup_location = f"{self.config.emoji} *Pickup Location:* {self.get_pickup_location()}"

        msg = (
            f"✅ *Order Placed!*\n\n"
            f"Your order for *{order_data.item}* has been enqueued.\n\n"
            f"{pickup_location}\n\n"
        )

        if order_data.notes:
            msg += f"📋 *Special Instructions:* _{order_data.notes}_\n\n"

        # Emphasized payment instruction for brothers
        msg += (
            f"\n💳 *PAYMENT INSTRUCTIONS*\n"
            f"⚠️ *Please be ready to pay when you receive the notification that your order is ready for pickup.*\n\n"
        )

        msg += (
            f"📩 You will receive a DM once your order is ready, In Shaa Allah.\n\n"
            f"Order ID: `{order_id}`\n\n"
            f"🤲 *JazakAllah Khair!* Your contribution supports our masjid. "
            f"May Allah bless you and your family!"
        )

        return msg

    def build_ready_message(self, order_data: dict) -> str:
        """Build ready message with payment reminder for brothers."""
        item = order_data.get("item", "your item")
        pickup_location = self.get_pickup_location()

        return (
            f"✅ *Your Order is Ready!*\n\n"
            f"*{item}* is ready for pickup at:\n"
            f"{self.config.emoji} {pickup_location}\n\n"
            f"\n💳 *PAYMENT REQUIRED*\n"
            f"⚠️ *Please have your payment ready when you arrive to pick up your order.*\n\n"
            f"🤲 JazakAllah Khair for supporting our masjid!"
        )


# ============================================================================
# SISTERS WORKFLOW
# ============================================================================


class SistersOrderWorkflow(BaseOrderWorkflow):
    """Order workflow for sisters section.

    Sisters pay immediately after placing their order at the sisters kitchen area.
    """

    def __init__(self):
        super().__init__(SISTERS_CONFIG)

    def build_confirmation_message(self, order_data: OrderData, order_id: str) -> str:
        """Build confirmation message with sisters-specific payment instructions."""
        pickup_location = f"{self.config.emoji} *Pickup Location:* {self.get_pickup_location()}"

        msg = (
            f"✅ *Order Placed!*\n\n"
            f"Your order for *{order_data.item}* has been enqueued.\n\n"
            f"{pickup_location}\n\n"
        )

        if order_data.notes:
            msg += f"📋 *Special Instructions:* _{order_data.notes}_\n\n"

        # Emphasized payment instruction for sisters
        msg += (
            f"\n💳 *PAYMENT INSTRUCTIONS*\n"
            f"⚠️ *Please proceed to the sisters kitchen area NOW to complete your payment.*\n\n"
            f"🚨 *Pay immediately after placing your order!*\n\n"
        )

        msg += (
            f"📩 You will receive a DM once your order is ready, In Shaa Allah.\n\n"
            f"Order ID: `{order_id}`\n\n"
            f"🤲 *JazakAllah Khair!* Your contribution supports our masjid. "
            f"May Allah bless you and your family!"
        )

        return msg

    def build_ready_message(self, order_data: dict) -> str:
        """Build ready message for sisters (payment already done)."""
        item = order_data.get("item", "your item")
        pickup_location = self.get_pickup_location()

        return (
            f"✅ *Your Order is Ready!*\n\n"
            f"*{item}* is ready for pickup at:\n"
            f"{self.config.emoji} {pickup_location}\n\n"
            f"🤲 JazakAllah Khair for supporting our masjid!"
        )


# ============================================================================
# WORKFLOW REGISTRY
# ============================================================================


# Singleton instances of workflows
_brothers_workflow = BrothersOrderWorkflow()
_sisters_workflow = SistersOrderWorkflow()

# Registry for looking up workflows by section name
WORKFLOWS: dict[str, BaseOrderWorkflow] = {
    "brothers": _brothers_workflow,
    "sisters": _sisters_workflow,
}


def get_workflow(section: str) -> BaseOrderWorkflow | None:
    """Get the workflow for a given section.

    Args:
        section: The section name ("brothers" or "sisters").

    Returns:
        The workflow instance, or None if not found.
    """
    return WORKFLOWS.get(section.lower())


def get_brothers_workflow() -> BrothersOrderWorkflow:
    """Get the brothers workflow instance."""
    return _brothers_workflow


def get_sisters_workflow() -> SistersOrderWorkflow:
    """Get the sisters workflow instance."""
    return _sisters_workflow
