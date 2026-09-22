"""Meta Graph API helpers for WhatsApp webhook diagnostics and WABA subscription."""

from __future__ import annotations

import logging
from typing import Any

import requests
from django.conf import settings

logger = logging.getLogger(__name__)


class GraphAPIError(Exception):
    def __init__(self, message: str, *, status: int | None = None, payload: Any = None):
        super().__init__(message)
        self.status = status
        self.payload = payload


def api_version() -> str:
    return (getattr(settings, "WHATSAPP_API_VERSION", "v26.0") or "v26.0").strip()


def access_token() -> str:
    return (getattr(settings, "WHATSAPP_ACCESS_TOKEN", "") or "").strip()


def graph_url(path: str) -> str:
    path = path.lstrip("/")
    return f"https://graph.facebook.com/{api_version()}/{path}"


def graph_get(path: str, *, params: dict | None = None) -> dict:
    token = access_token()
    if not token:
        raise GraphAPIError("WHATSAPP_ACCESS_TOKEN is not set")
    resp = requests.get(
        graph_url(path),
        params={**(params or {}), "access_token": token},
        timeout=30,
    )
    data = resp.json() if resp.content else {}
    if not resp.ok or "error" in data:
        err = data.get("error") or {}
        raise GraphAPIError(
            err.get("message") or f"Graph GET failed ({resp.status_code})",
            status=resp.status_code,
            payload=data,
        )
    return data


def graph_post(path: str, *, data: dict | None = None) -> dict:
    token = access_token()
    if not token:
        raise GraphAPIError("WHATSAPP_ACCESS_TOKEN is not set")
    resp = requests.post(
        graph_url(path),
        params={"access_token": token},
        data=data or {},
        timeout=30,
    )
    payload = resp.json() if resp.content else {}
    if not resp.ok or "error" in payload:
        err = payload.get("error") or {}
        raise GraphAPIError(
            err.get("message") or f"Graph POST failed ({resp.status_code})",
            status=resp.status_code,
            payload=payload,
        )
    return payload


def get_subscribed_apps(waba_id: str) -> dict:
    return graph_get(f"{waba_id}/subscribed_apps")


def subscribe_waba(waba_id: str) -> dict:
    """Link this Meta app to the WABA (required for live message webhooks)."""
    return graph_post(f"{waba_id}/subscribed_apps")


def get_phone_number(phone_number_id: str) -> dict:
    fields = (
        "id,display_phone_number,verified_name,quality_rating,"
        "platform_type,webhook_configuration,code_verification_status"
    )
    return graph_get(phone_number_id, params={"fields": fields})


def register_whatsapp_webhook(*, app_id: str, callback_url: str, verify_token: str) -> dict:
    """
    Register app-level WhatsApp webhook fields (messages) via Graph API.
    Mirrors Meta Developer Console → WhatsApp → Configuration.
    """
    return graph_post(
        f"{app_id}/subscriptions",
        data={
            "object": "whatsapp_business_account",
            "callback_url": callback_url,
            "verify_token": verify_token,
            "fields": "messages",
            "include_values": "true",
        },
    )
