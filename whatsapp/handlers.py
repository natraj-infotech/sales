"""
Pluggable inbound WhatsApp handlers.

Register handlers with @register_flow_handler / @register_webhook_handler.
Leads is the first consumer; renewals / Case Lock / etc. can plug in later.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from django.conf import settings

from business.lead_ingest import create_lead_from_whatsapp
from business.models import Product

from .models import WhatsAppAccount

logger = logging.getLogger(__name__)

FlowHandler = Callable[[WhatsAppAccount | None, dict[str, Any]], dict[str, Any]]
WebhookHandler = Callable[[WhatsAppAccount | None, dict[str, Any]], None]

_FLOW_HANDLERS: dict[str, FlowHandler] = {}
_WEBHOOK_HANDLERS: list[tuple[str, WebhookHandler]] = []


def register_flow_handler(name: str):
    def decorator(fn: FlowHandler):
        _FLOW_HANDLERS[name] = fn
        return fn

    return decorator


def register_webhook_handler(event_type: str = "*"):
    def decorator(fn: WebhookHandler):
        _WEBHOOK_HANDLERS.append((event_type, fn))
        return fn

    return decorator


def get_flow_handler(name: str | None) -> FlowHandler:
    key = (name or getattr(settings, "WHATSAPP_DEFAULT_FLOW_HANDLER", "lead") or "lead").strip()
    if key not in _FLOW_HANDLERS:
        raise KeyError(f"Unknown WhatsApp flow handler: {key}")
    return _FLOW_HANDLERS[key]


def dispatch_webhook(account: WhatsAppAccount | None, event: dict[str, Any]) -> None:
    event_type = (event.get("type") or "*").strip()
    for registered_type, handler in _WEBHOOK_HANDLERS:
        if registered_type in ("*", event_type):
            try:
                handler(account, event)
            except Exception:  # noqa: BLE001
                logger.exception("WhatsApp webhook handler failed type=%s", registered_type)


@register_webhook_handler("*")
def inbound_welcome_handler(
    account: WhatsAppAccount | None, event: dict[str, Any]
) -> None:
    """Auto welcome/menu on new WhatsApp conversations (any inbound message type)."""
    if (event.get("type") or "") == "status":
        return
    if not event.get("message"):
        return
    from .conversation import handle_inbound_customer_message

    handle_inbound_customer_message(account, event)


def _product_options(account: WhatsAppAccount | None) -> list[dict[str, str]]:
    if account is None:
        return []
    qs = Product.objects.filter(company=account.company).order_by("name")[:100]
    return [{"id": str(p.pk), "title": p.name[:30]} for p in qs]


def _parse_product_ids(data: dict[str, Any]) -> list[int]:
    raw = data.get("product_ids") or data.get("products") or data.get("product_id") or []
    if isinstance(raw, (str, int)):
        raw = [raw]
    ids: list[int] = []
    for item in raw:
        try:
            ids.append(int(item))
        except (TypeError, ValueError):
            continue
    return ids


@register_flow_handler("lead")
def lead_flow_handler(
    account: WhatsAppAccount | None, payload: dict[str, Any]
) -> dict[str, Any]:
    """
    WhatsApp Flow exchange for Lead capture.

    Expected Meta Flow screens (configure in Meta to match):
      - LEAD_FORM (data_exchange / complete)
      - SUCCESS
    """
    version = payload.get("version") or "3.0"
    action = (payload.get("action") or "").strip()
    data = payload.get("data") or {}

    if action == "ping":
        return {"version": version, "data": {"status": "active"}}

    if action in ("INIT", "BACK"):
        return {
            "version": version,
            "screen": "LEAD_FORM",
            "data": {
                "products": _product_options(account),
            },
        }

    if action in ("data_exchange", "COMPLETE", "complete"):
        # Final submission — create lead when contact_name present.
        contact_name = (
            data.get("contact_name")
            or data.get("name")
            or data.get("full_name")
            or ""
        ).strip()
        if contact_name and account is not None:
            product_ids = _parse_product_ids(data)
            wa_id = (
                data.get("wa_id")
                or payload.get("flow_token")
                or ""
            )
            message_id = (
                data.get("message_id")
                or data.get("whatsapp_message_id")
                or ""
            )
            create_lead_from_whatsapp(
                company=account.company,
                contact_name=contact_name,
                company_name=(data.get("company_name") or data.get("company") or "").strip(),
                email=(data.get("email") or "").strip(),
                phone=(data.get("phone") or "").strip(),
                requirement=(data.get("requirement") or data.get("notes") or "").strip(),
                product_ids=product_ids,
                whatsapp_wa_id=str(wa_id),
                whatsapp_message_id=str(message_id),
                raw_payload=payload,
            )
            return {
                "version": version,
                "screen": "SUCCESS",
                "data": {
                    "extension_message_response": {
                        "params": {
                            "flow_token": payload.get("flow_token"),
                        }
                    }
                },
            }

        # Intermediate navigation: refresh product list on LEAD_FORM
        return {
            "version": version,
            "screen": payload.get("screen") or "LEAD_FORM",
            "data": {
                **data,
                "products": _product_options(account),
            },
        }

    # Default: keep form alive
    return {
        "version": version,
        "screen": payload.get("screen") or "LEAD_FORM",
        "data": {
            **data,
            "products": _product_options(account),
        },
    }
