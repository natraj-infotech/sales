"""WhatsApp conversation entrypoints (delegates to inbound_chat + legacy helpers)."""

from __future__ import annotations

from .inbound_chat import handle_inbound_customer_message

__all__ = ["handle_inbound_customer_message", "send_welcome_menu"]


def send_welcome_menu(*, account, to: str, contact_name: str = "") -> None:
    """Send the main three-button welcome menu."""
    from .chat_flows import send_main_welcome

    send_main_welcome(account=account, to=to)
