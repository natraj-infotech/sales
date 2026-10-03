"""Signed tokens for public WhatsApp / web lead enquiry forms."""

from __future__ import annotations

from django.core.signing import BadSignature, SignatureExpired, dumps, loads

SIGNING_SALT = "business.lead-public-enquiry"
TOKEN_MAX_AGE_SECONDS = 30 * 24 * 60 * 60


def sign_lead_public_token(
    *,
    company_id: int | None,
    whatsapp_wa_id: str = "",
    contact_name: str = "",
) -> str:
    payload = {
        "c": int(company_id) if company_id is not None else None,
        "w": (whatsapp_wa_id or "").strip(),
        "n": (contact_name or "").strip()[:255],
    }
    return dumps(payload, salt=SIGNING_SALT)


def unsign_lead_public_token(token: str) -> dict | None:
    try:
        payload = loads(
            token or "",
            salt=SIGNING_SALT,
            max_age=TOKEN_MAX_AGE_SECONDS,
        )
    except (BadSignature, SignatureExpired, TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    company_id = payload.get("c")
    if company_id is not None:
        try:
            company_id = int(company_id)
        except (TypeError, ValueError):
            return None
    return {
        "company_id": company_id,
        "whatsapp_wa_id": str(payload.get("w") or "").strip(),
        "contact_name": str(payload.get("n") or "").strip(),
    }
