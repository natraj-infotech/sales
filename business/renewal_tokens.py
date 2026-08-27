"""Signed per-recipient tokens for public renewal confirmation links."""

from __future__ import annotations

from django.core.signing import BadSignature, SignatureExpired, dumps, loads

SIGNING_SALT = "business.renewal-confirm"
TOKEN_MAX_AGE_SECONDS = 120 * 24 * 60 * 60


def sign_renewal_confirm_token(invite_id, email: str) -> str:
    payload = {
        "i": str(invite_id),
        "e": (email or "").strip().lower(),
    }
    return dumps(payload, salt=SIGNING_SALT)


def unsign_renewal_confirm_token(token: str) -> tuple[str, str] | None:
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
    invite_id = str(payload.get("i") or "").strip()
    email = str(payload.get("e") or "").strip().lower()
    if not invite_id or not email or "@" not in email:
        return None
    return invite_id, email
