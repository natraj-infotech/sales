"""Inbound WhatsApp message routing (welcome + multi-step flows)."""

from __future__ import annotations

import logging
from typing import Any

from django.db import transaction

from business.lead_ingest import get_or_create_lead_by_whatsapp_id
from business.models import Lead

from .account_resolve import WhatsAppAccountLike, resolve_lead_company
from .chat_flows import (
    route_active_flow,
    send_main_welcome,
    start_flow_from_welcome,
)
from .client import WhatsAppClientError
from .conversation_state import get_or_create_session
from .models import WhatsAppConversation

logger = logging.getLogger(__name__)

WELCOME_BUTTON_IDS = frozenset(
    {"procure", "lead", "support", "caselock", "enquiry", "new_enquiry"}
)


def _contact_name_from_event(event: dict[str, Any], wa_from: str) -> str:
    for contact in event.get("contacts") or []:
        profile = contact.get("profile") or {}
        name = (profile.get("name") or "").strip()
        if name:
            return name
    return wa_from


def parse_inbound_message(event: dict[str, Any]) -> dict[str, Any]:
    message = event.get("message") or {}
    msg_type = (message.get("type") or event.get("type") or "").strip()
    text = ""
    reply_id = ""

    if msg_type == "text":
        text = ((message.get("text") or {}).get("body") or "").strip()
    elif msg_type == "interactive":
        interactive = message.get("interactive") or {}
        itype = (interactive.get("type") or "").strip()
        if itype == "button_reply":
            reply_id = ((interactive.get("button_reply") or {}).get("id") or "").strip()
        elif itype == "list_reply":
            reply_id = ((interactive.get("list_reply") or {}).get("id") or "").strip()

    return {
        "message": message,
        "msg_type": msg_type,
        "text": text,
        "reply_id": reply_id,
        "wa_from": (message.get("from") or "").strip(),
        "message_id": (message.get("id") or "").strip(),
    }


@transaction.atomic
def handle_inbound_customer_message(
    account: WhatsAppAccountLike | None,
    event: dict[str, Any],
) -> dict[str, Any]:
    result = {
        "handled": False,
        "welcome_sent": False,
        "skipped": "",
    }
    if account is None:
        result["skipped"] = "no_account"
        return result

    parsed = parse_inbound_message(event)
    message = parsed["message"]
    msg_type = parsed["msg_type"]
    if not message or msg_type in ("", "status"):
        result["skipped"] = "not_a_message"
        return result

    wa_from = parsed["wa_from"]
    message_id = parsed["message_id"]
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

    if message_id and lead.last_inbound_message_id == message_id:
        result["skipped"] = "duplicate_message"
        result["handled"] = True
        return result

    lead.last_inbound_at = timezone_now()
    update_fields = ["last_inbound_at", "updated_at"]
    if message_id:
        lead.last_inbound_message_id = message_id
        update_fields.append("last_inbound_message_id")
    if not lead.phone:
        lead.phone = wa_from
        update_fields.append("phone")
    lead.save(update_fields=update_fields)

    session = get_or_create_session(lead)
    text = parsed["text"]
    reply_id = parsed["reply_id"].lower()

    try:
        if session.phase == WhatsAppConversation.Phase.IDLE:
            if reply_id in WELCOME_BUTTON_IDS:
                start_flow_from_welcome(
                    account=account,
                    lead=lead,
                    session=session,
                    to=wa_from,
                    reply_id=reply_id,
                )
            else:
                send_main_welcome(account=account, to=wa_from)
                lead.last_welcome_sent_at = timezone_now()
                lead.save(update_fields=["last_welcome_sent_at", "updated_at"])
                result["welcome_sent"] = True
        else:
            route_active_flow(
                account=account,
                lead=lead,
                session=session,
                to=wa_from,
                contact_name=contact_name,
                text=text,
                reply_id=reply_id,
            )
    except WhatsAppClientError:
        logger.exception("WhatsApp chat flow failed lead_id=%s", lead.pk)
        result["skipped"] = "send_failed"
    else:
        result["handled"] = True
        result["lead_id"] = lead.pk

    return result


def timezone_now():
    from django.utils import timezone

    return timezone.now()
