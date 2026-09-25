"""WhatsApp outbound helpers — Stop button on interactive prompts."""

from __future__ import annotations

from .account_resolve import WhatsAppAccountLike
from .client import (
    send_interactive_list,
    send_interactive_reply_buttons,
    send_text_message,
)
from .conversation_state import reset_session
from .models import WhatsAppConversation

STOP_ID = "stop_now"
STOP_BUTTON = {"id": STOP_ID, "title": "Stop"}
BYE_THANK_YOU = "Thank you for contacting Tracker365. Have a great day!"


def is_stop_intent(*, reply_id: str, text: str) -> bool:
    if (reply_id or "").strip().lower() == STOP_ID:
        return True
    return (text or "").strip().lower() == "stop"


def end_session(
    *,
    account: WhatsAppAccountLike,
    session: WhatsAppConversation,
    to: str,
) -> None:
    reset_session(session)
    send_text_message(account=account, to=to, body=BYE_THANK_YOU)


def _buttons_with_stop(buttons: list[dict[str, str]]) -> list[dict[str, str]]:
    """WhatsApp allows max 3 reply buttons; reserve one for Stop."""
    trimmed = [b for b in buttons if b.get("id") != STOP_ID][:2]
    return trimmed + [STOP_BUTTON]


def send_flow_buttons(
    *,
    account: WhatsAppAccountLike,
    to: str,
    body_text: str,
    buttons: list[dict[str, str]],
    header_text: str = "",
    footer_text: str = "",
) -> None:
    send_interactive_reply_buttons(
        account=account,
        to=to,
        body_text=body_text,
        buttons=_buttons_with_stop(buttons),
        header_text=header_text,
        footer_text=footer_text,
    )


def send_flow_prompt(
    *,
    account: WhatsAppAccountLike,
    to: str,
    body_text: str,
) -> None:
    """User prompt with a single Stop reply button."""
    send_interactive_reply_buttons(
        account=account,
        to=to,
        body_text=body_text,
        buttons=[STOP_BUTTON],
    )


def send_flow_list(
    *,
    account: WhatsAppAccountLike,
    to: str,
    body_text: str,
    button_label: str,
    rows: list[dict[str, str]],
    header_text: str = "",
    footer_text: str = "",
) -> None:
    """List picker with Stop as the last row (when room)."""
    list_rows = list(rows)
    if len(list_rows) < 10 and not any(r.get("id") == STOP_ID for r in list_rows):
        list_rows.append({"id": STOP_ID, "title": "Stop", "description": "End chat"})
    send_interactive_list(
        account=account,
        to=to,
        body_text=body_text,
        button_label=button_label,
        rows=list_rows[:10],
        header_text=header_text,
        footer_text=footer_text,
    )
