"""New-conversation detection and welcome/menu auto-reply for WhatsApp inbound."""

from __future__ import annotations

import json
import logging
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from business.lead_ingest import get_or_create_lead_by_whatsapp_id
from business.models import Lead

from .client import (
    WhatsAppClientError,
    send_flow_message,
    send_interactive_cta_url,
    send_interactive_reply_buttons,
    send_text_message,
)
from .account_resolve import WhatsAppAccountLike, resolve_lead_company

logger = logging.getLogger(__name__)


def inactivity_hours() -> float:
    try:
        return float(getattr(settings, "WHATSAPP_WELCOME_INACTIVITY_HOURS", 24) or 24)
    except (TypeError, ValueError):
        return 24.0


def is_new_conversation(lead: Lead, *, now=None) -> bool:
    """True if never messaged before, or last inbound older than inactivity window."""
    now = now or timezone.now()
    if lead.last_inbound_at is None:
        return True
    return (now - lead.last_inbound_at) >= timedelta(hours=inactivity_hours())


def _contact_name_from_event(event: dict[str, Any], wa_from: str) -> str:
    for contact in event.get("contacts") or []:
        profile = contact.get("profile") or {}
        name = (profile.get("name") or "").strip()
        if name:
            return name
        if str(contact.get("wa_id") or "") == wa_from:
            name = (profile.get("name") or "").strip()
            if name:
                return name
    return wa_from


def _parse_buttons() -> list[dict[str, str]]:
    """
    WHATSAPP_WELCOME_BUTTONS formats:
      JSON: [{"id":"lead","title":"New lead"},...]
      or: lead:New lead,support:Get support
    """
    raw = (getattr(settings, "WHATSAPP_WELCOME_BUTTONS", "") or "").strip()
    if not raw:
        return [
            {"id": "lead", "title": "New enquiry"},
            {"id": "support", "title": "Support"},
        ]
    if raw.startswith("["):
        try:
            data = json.loads(raw)
            return [
                {"id": str(b["id"]), "title": str(b["title"])}
                for b in data
                if b.get("id") and b.get("title")
            ][:3]
        except (json.JSONDecodeError, TypeError, KeyError):
            logger.warning("Invalid WHATSAPP_WELCOME_BUTTONS JSON")
    buttons: list[dict[str, str]] = []
    for part in raw.split(","):
        part = part.strip()
        if not part or ":" not in part:
            continue
        bid, title = part.split(":", 1)
        bid, title = bid.strip(), title.strip()
        if bid and title:
            buttons.append({"id": bid, "title": title[:20]})
    return buttons[:3] or [{"id": "lead", "title": "New enquiry"}]


def send_welcome_menu(
    *,
    account: WhatsAppAccountLike,
    to: str,
    contact_name: str = "",
) -> None:
    """Send the configured welcome/menu (CTA URL, flow, buttons, or text)."""
    body = (
        getattr(settings, "WHATSAPP_WELCOME_BODY", "")
        or "Welcome to Tracker365. How can we help you today?"
    ).strip()
    header = (getattr(settings, "WHATSAPP_WELCOME_HEADER", "") or "").strip()
    footer = (getattr(settings, "WHATSAPP_WELCOME_FOOTER", "") or "").strip()
    mode = (
        getattr(settings, "WHATSAPP_WELCOME_MODE", "auto") or "auto"
    ).strip().lower()
    flow_id = (getattr(settings, "WHATSAPP_WELCOME_FLOW_ID", "") or "").strip()
    flow_cta = (
        getattr(settings, "WHATSAPP_WELCOME_FLOW_CTA", "") or "Open form"
    ).strip()
    cta_label = (
        getattr(settings, "WHATSAPP_WELCOME_CTA_DISPLAY_TEXT", "")
        or "Open enquiry form"
    ).strip()

    if mode == "flow" or (mode == "auto" and flow_id):
        if not flow_id:
            raise WhatsAppClientError("WHATSAPP_WELCOME_FLOW_ID is required for flow mode")
        send_flow_message(
            account=account,
            to=to,
            body_text=body,
            flow_id=flow_id,
            flow_cta=flow_cta,
            header_text=header,
            footer_text=footer,
        )
        return

    if mode == "cta_url":
        send_lead_enquiry_cta(
            account=account,
            to=to,
            contact_name=contact_name,
            body_text=body,
            display_text=cta_label,
            header_text=header,
            footer_text=footer,
        )
        return

    if mode in ("buttons", "auto"):
        buttons = _parse_buttons()
        if buttons:
            send_interactive_reply_buttons(
                account=account,
                to=to,
                body_text=body,
                buttons=buttons,
                header_text=header,
                footer_text=footer,
            )
            return

    send_text_message(account=account, to=to, body=body)


def send_lead_enquiry_cta(
    *,
    account: WhatsAppAccountLike,
    to: str,
    contact_name: str = "",
    body_text: str | None = None,
    display_text: str | None = None,
    header_text: str = "",
    footer_text: str = "",
) -> None:
    from business.lead_public_urls import build_lead_public_enquiry_url

    body = (
        body_text
        or getattr(settings, "WHATSAPP_BUTTON_LEAD_REPLY_BODY", "")
        or "Tap the button below to open our enquiry form."
    ).strip()
    label = (
        display_text
        or getattr(settings, "WHATSAPP_WELCOME_CTA_DISPLAY_TEXT", "")
        or "Open enquiry form"
    ).strip()
    company = resolve_lead_company(account)
    enquiry_url = build_lead_public_enquiry_url(
        company=company,
        whatsapp_wa_id=to,
        contact_name=contact_name,
    )
    send_interactive_cta_url(
        account=account,
        to=to,
        body_text=body,
        url=enquiry_url,
        display_text=label,
        header_text=header_text,
        footer_text=footer_text,
    )


_LEAD_BUTTON_IDS = frozenset({"lead", "enquiry", "new_enquiry"})


def handle_menu_button_reply(
    account: WhatsAppAccountLike | None,
    event: dict[str, Any],
) -> bool:
    """
    Reply to welcome menu button taps (e.g. lead → public enquiry CTA URL).

    Returns True when the inbound message was a handled menu button.
    """
    if account is None:
        return False
    message = event.get("message") or {}
    if (message.get("type") or "") != "interactive":
        return False
    interactive = message.get("interactive") or {}
    if (interactive.get("type") or "") != "button_reply":
        return False
    reply = interactive.get("button_reply") or {}
    button_id = (reply.get("id") or "").strip().lower()
    if not button_id:
        return False

    wa_from = (message.get("from") or "").strip()
    if not wa_from:
        return False

    contact_name = _contact_name_from_event(event, wa_from)

    try:
        if button_id in _LEAD_BUTTON_IDS:
            send_lead_enquiry_cta(
                account=account,
                to=wa_from,
                contact_name=contact_name,
            )
            return True
        if button_id == "support":
            body = (
                getattr(settings, "WHATSAPP_BUTTON_SUPPORT_REPLY_BODY", "")
                or "Tell us what you need help with in a message here. Our team will reply shortly."
            ).strip()
            send_text_message(account=account, to=wa_from, body=body)
            return True
    except WhatsAppClientError:
        logger.exception(
            "WhatsApp menu button reply failed button_id=%s wa_from=%s",
            button_id,
            wa_from,
        )
    return False


@transaction.atomic
def handle_inbound_customer_message(
    account: WhatsAppAccountLike | None,
    event: dict[str, Any],
) -> dict[str, Any]:
    """
    Find/create Lead, decide new conversation, optionally send welcome, always
    update last_inbound_at.

    Returns a small result dict for tests/logging.
    """
    result = {
        "handled": False,
        "welcome_sent": False,
        "is_new_conversation": False,
        "skipped": "",
    }
    if account is None:
        result["skipped"] = "no_account"
        logger.warning(
            "WhatsApp inbound skipped welcome: no account (set WHATSAPP_ACCESS_TOKEN "
            "or an active WhatsAppAccount for this phone_number_id)"
        )
        return result

    message = event.get("message") or {}
    msg_type = (message.get("type") or event.get("type") or "").strip()
    interactive = message.get("interactive") or {}
    is_menu_button = (
        msg_type == "interactive"
        and (interactive.get("type") or "") == "button_reply"
    )
    # Ignore delivery statuses (handled separately) and empty payloads.
    if not message or msg_type in ("", "status"):
        result["skipped"] = "not_a_message"
        return result

    wa_from = (message.get("from") or "").strip()
    message_id = (message.get("id") or "").strip()
    if not wa_from:
        result["skipped"] = "missing_from"
        return result

    company = resolve_lead_company(account)
    contact_name = _contact_name_from_event(event, wa_from)
    lead, _created = get_or_create_lead_by_whatsapp_id(
        company=company,
        whatsapp_wa_id=wa_from,
        contact_name=contact_name,
        phone=wa_from,
        raw_payload={"inbound": message},
    )
    lead = Lead.objects.select_for_update().get(pk=lead.pk)

    # Meta webhook retries: same message id already processed.
    if message_id and lead.last_inbound_message_id == message_id:
        result["skipped"] = "duplicate_message"
        result["handled"] = True
        return result

    now = timezone.now()
    # Evaluate BEFORE updating last_inbound_at.
    new_convo = is_new_conversation(lead, now=now)
    result["is_new_conversation"] = new_convo

    lead.last_inbound_at = now
    update_fields = ["last_inbound_at", "updated_at"]
    if message_id:
        lead.last_inbound_message_id = message_id
        update_fields.append("last_inbound_message_id")
    if not lead.phone:
        lead.phone = wa_from
        update_fields.append("phone")
    lead.save(update_fields=update_fields)

    never_welcomed = lead.last_welcome_sent_at is None
    should_welcome = (new_convo or never_welcomed) and not is_menu_button
    if should_welcome:
        try:
            send_welcome_menu(
                account=account,
                to=wa_from,
                contact_name=contact_name,
            )
            lead.last_welcome_sent_at = timezone.now()
            lead.save(update_fields=["last_welcome_sent_at", "updated_at"])
            result["welcome_sent"] = True
            logger.info(
                "WhatsApp welcome sent lead_id=%s wa_from=%s new_convo=%s",
                lead.pk,
                wa_from,
                new_convo,
            )
        except WhatsAppClientError:
            logger.exception(
                "WhatsApp welcome send failed lead_id=%s wa_from=%s",
                lead.pk,
                wa_from,
            )
            result["skipped"] = "send_failed"
    elif not never_welcomed:
        result["skipped"] = "not_new_conversation"
        logger.info(
            "WhatsApp welcome skipped lead_id=%s wa_from=%s (within inactivity window)",
            lead.pk,
            wa_from,
        )

    result["handled"] = True
    result["lead_id"] = lead.pk
    return result
