"""Outbound WhatsApp Cloud API helpers (shared across features)."""

from __future__ import annotations

import logging
from typing import Any

import requests
from django.conf import settings

from .models import WhatsAppAccount, WhatsAppMessageLog

logger = logging.getLogger(__name__)


class WhatsAppClientError(Exception):
    pass


def _graph_url(phone_number_id: str, path: str = "messages") -> str:
    version = getattr(settings, "WHATSAPP_API_VERSION", "v26.0")
    return f"https://graph.facebook.com/{version}/{phone_number_id}/{path}"


def send_text_message(
    *,
    account: WhatsAppAccount,
    to: str,
    body: str,
    preview_url: bool = False,
) -> dict[str, Any]:
    token = account.resolved_access_token()
    if not token:
        raise WhatsAppClientError("WhatsApp access token not configured")

    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "text",
        "text": {"preview_url": preview_url, "body": body},
    }
    resp = requests.post(
        _graph_url(account.phone_number_id),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=30,
    )
    data: dict[str, Any]
    try:
        data = resp.json()
    except ValueError:
        data = {"raw": resp.text}

    WhatsAppMessageLog.objects.create(
        company=account.company,
        account=account,
        direction=WhatsAppMessageLog.Direction.OUTBOUND,
        wa_to=to,
        event_type="text",
        payload={"request": payload, "response": data, "status_code": resp.status_code},
        wa_message_id=(data.get("messages") or [{}])[0].get("id", ""),
    )

    if resp.status_code >= 400:
        logger.error("WhatsApp send failed: %s %s", resp.status_code, data)
        raise WhatsAppClientError(f"WhatsApp API error {resp.status_code}")
    return data


def send_template_message(
    *,
    account: WhatsAppAccount,
    to: str,
    template_name: str,
    language_code: str = "en",
    components: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    token = account.resolved_access_token()
    if not token:
        raise WhatsAppClientError("WhatsApp access token not configured")

    payload: dict[str, Any] = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": language_code},
        },
    }
    if components:
        payload["template"]["components"] = components

    resp = requests.post(
        _graph_url(account.phone_number_id),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=30,
    )
    try:
        data = resp.json()
    except ValueError:
        data = {"raw": resp.text}

    WhatsAppMessageLog.objects.create(
        company=account.company,
        account=account,
        direction=WhatsAppMessageLog.Direction.OUTBOUND,
        wa_to=to,
        event_type=f"template:{template_name}",
        payload={"request": payload, "response": data, "status_code": resp.status_code},
        wa_message_id=(data.get("messages") or [{}])[0].get("id", ""),
    )
    if resp.status_code >= 400:
        raise WhatsAppClientError(f"WhatsApp API error {resp.status_code}")
    return data


def _post_message(
    *,
    account: WhatsAppAccount,
    payload: dict[str, Any],
    event_type: str,
    wa_to: str,
) -> dict[str, Any]:
    token = account.resolved_access_token()
    if not token:
        raise WhatsAppClientError("WhatsApp access token not configured")

    resp = requests.post(
        _graph_url(account.phone_number_id),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=30,
    )
    try:
        data = resp.json()
    except ValueError:
        data = {"raw": resp.text}

    WhatsAppMessageLog.objects.create(
        company=account.company,
        account=account,
        direction=WhatsAppMessageLog.Direction.OUTBOUND,
        wa_to=wa_to,
        event_type=event_type,
        payload={"request": payload, "response": data, "status_code": resp.status_code},
        wa_message_id=(data.get("messages") or [{}])[0].get("id", ""),
    )
    if resp.status_code >= 400:
        logger.error("WhatsApp send failed: %s %s", resp.status_code, data)
        raise WhatsAppClientError(f"WhatsApp API error {resp.status_code}")
    return data


def send_interactive_reply_buttons(
    *,
    account: WhatsAppAccount,
    to: str,
    body_text: str,
    buttons: list[dict[str, str]],
    header_text: str = "",
    footer_text: str = "",
) -> dict[str, Any]:
    """
    Send interactive reply buttons.

    buttons: list of {"id": "...", "title": "..."} (max 3, title max 20 chars).
    """
    btn_rows = []
    for btn in buttons[:3]:
        btn_rows.append(
            {
                "type": "reply",
                "reply": {
                    "id": str(btn["id"])[:256],
                    "title": str(btn["title"])[:20],
                },
            }
        )
    interactive: dict[str, Any] = {
        "type": "button",
        "body": {"text": body_text[:1024]},
        "action": {"buttons": btn_rows},
    }
    if header_text:
        interactive["header"] = {"type": "text", "text": header_text[:60]}
    if footer_text:
        interactive["footer"] = {"text": footer_text[:60]}

    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "interactive",
        "interactive": interactive,
    }
    return _post_message(
        account=account,
        payload=payload,
        event_type="welcome_buttons",
        wa_to=to,
    )


def send_flow_message(
    *,
    account: WhatsAppAccount,
    to: str,
    body_text: str,
    flow_id: str,
    flow_cta: str = "Open",
    header_text: str = "",
    footer_text: str = "",
    flow_token: str = "lead",
    screen: str = "LEAD_FORM",
) -> dict[str, Any]:
    """Send an interactive Flow CTA message (customer-care window)."""
    parameters: dict[str, Any] = {
        "flow_message_version": "3",
        "flow_id": str(flow_id),
        "flow_cta": (flow_cta or "Open")[:30],
        "flow_token": flow_token or "lead",
        "flow_action": "navigate",
        "flow_action_payload": {"screen": screen or "LEAD_FORM", "data": {}},
    }
    interactive: dict[str, Any] = {
        "type": "flow",
        "body": {"text": body_text[:1024]},
        "action": {"name": "flow", "parameters": parameters},
    }
    if header_text:
        interactive["header"] = {"type": "text", "text": header_text[:60]}
    if footer_text:
        interactive["footer"] = {"text": footer_text[:60]}

    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "interactive",
        "interactive": interactive,
    }
    return _post_message(
        account=account,
        payload=payload,
        event_type="welcome_flow",
        wa_to=to,
    )
