"""WhatsApp Cloud API webhook + Flow data-exchange endpoints."""

from __future__ import annotations

import json
import logging

from django.conf import settings
from django.http import HttpResponse, HttpResponseBadRequest, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from .encryption import FlowEncryptionError, decrypt_request, encrypt_response
from .handlers import dispatch_webhook, get_flow_handler
from .models import WhatsAppAccount, WhatsAppMessageLog

logger = logging.getLogger(__name__)


def _resolve_account(phone_number_id: str | None) -> WhatsAppAccount | None:
    if not phone_number_id:
        return None
    return (
        WhatsAppAccount.objects.select_related("company")
        .filter(phone_number_id=str(phone_number_id), is_active=True)
        .first()
    )


@csrf_exempt
@require_http_methods(["GET", "POST"])
def webhook(request):
    if request.method == "GET":
        return _webhook_verify(request)
    return _webhook_receive(request)


@require_GET
def _webhook_verify(request):
    mode = request.GET.get("hub.mode", "")
    token = request.GET.get("hub.verify_token", "")
    challenge = request.GET.get("hub.challenge", "")
    expected = getattr(settings, "WHATSAPP_VERIFY_TOKEN", "") or ""
    if mode == "subscribe" and expected and token == expected:
        return HttpResponse(challenge, content_type="text/plain")
    return HttpResponse("Forbidden", status=403)


@require_POST
def _webhook_receive(request):
    try:
        body = json.loads(request.body.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return HttpResponseBadRequest("Invalid JSON")

    for entry in body.get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            metadata = value.get("metadata") or {}
            phone_number_id = metadata.get("phone_number_id")
            account = _resolve_account(phone_number_id)

            for status in value.get("statuses") or []:
                WhatsAppMessageLog.objects.create(
                    company=account.company if account else None,
                    account=account,
                    direction=WhatsAppMessageLog.Direction.INBOUND,
                    wa_message_id=status.get("id", ""),
                    event_type=f"status:{status.get('status', '')}",
                    payload=status,
                )
                dispatch_webhook(
                    account,
                    {"type": "status", "status": status, "metadata": metadata},
                )

            for message in value.get("messages") or []:
                WhatsAppMessageLog.objects.create(
                    company=account.company if account else None,
                    account=account,
                    direction=WhatsAppMessageLog.Direction.INBOUND,
                    wa_message_id=message.get("id", ""),
                    wa_from=message.get("from", ""),
                    event_type=message.get("type", "message"),
                    payload=message,
                )
                dispatch_webhook(
                    account,
                    {
                        "type": message.get("type") or "message",
                        "message": message,
                        "metadata": metadata,
                        "contacts": value.get("contacts") or [],
                    },
                )

    return JsonResponse({"ok": True})


@csrf_exempt
@require_POST
def flow_data_exchange(request):
    """
    Meta WhatsApp Flows endpoint.

    Optional query ?handler=lead (default WHATSAPP_DEFAULT_FLOW_HANDLER).
    Optional header X-WhatsApp-Phone-Number-Id or body field phone_number_id
    to resolve the company account.
    """
    try:
        body = json.loads(request.body.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return HttpResponseBadRequest("Invalid JSON")

    phone_number_id = (
        request.headers.get("X-WhatsApp-Phone-Number-Id")
        or body.get("phone_number_id")
        or getattr(settings, "WHATSAPP_DEFAULT_PHONE_NUMBER_ID", "")
    )
    account = _resolve_account(phone_number_id)

    handler_name = request.GET.get("handler") or getattr(
        settings, "WHATSAPP_DEFAULT_FLOW_HANDLER", "lead"
    )

    try:
        payload, aes_key, iv = decrypt_request(body)
    except FlowEncryptionError as exc:
        # Health checks / unencrypted ping during setup
        if body.get("action") == "ping" or (body.get("data") or {}).get("status") == "active":
            return JsonResponse({"data": {"status": "active"}})
        logger.warning("Flow decrypt failed: %s", exc)
        return HttpResponseBadRequest(str(exc))

    try:
        handler = get_flow_handler(handler_name)
        response_payload = handler(account, payload)
    except Exception:  # noqa: BLE001
        logger.exception("Flow handler error handler=%s", handler_name)
        return HttpResponse(status=500)

    try:
        encrypted = encrypt_response(response_payload, aes_key, iv)
    except Exception:  # noqa: BLE001
        logger.exception("Flow encrypt failed")
        return HttpResponse(status=500)

    return HttpResponse(encrypted, content_type="text/plain")
