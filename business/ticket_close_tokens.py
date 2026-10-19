"""Signed tokens for the public ticket-close link in assignment email."""

from __future__ import annotations

from django.core.signing import BadSignature, SignatureExpired, dumps, loads

SIGNING_SALT = "business.ticket-close"
TRANSFER_SIGNING_SALT = "business.ticket-transfer"
TOKEN_MAX_AGE_SECONDS = 120 * 24 * 60 * 60


def sign_ticket_close_token(ticket_id, engineer_id) -> str:
    payload = {"t": int(ticket_id), "u": int(engineer_id)}
    return dumps(payload, salt=SIGNING_SALT)


def sign_ticket_transfer_token(ticket_id, engineer_id) -> str:
    payload = {"t": int(ticket_id), "u": int(engineer_id)}
    return dumps(payload, salt=TRANSFER_SIGNING_SALT)


def unsign_ticket_close_token(token: str) -> tuple[int, int] | None:
    return _unsign(token, SIGNING_SALT)


def unsign_ticket_transfer_token(token: str) -> tuple[int, int] | None:
    return _unsign(token, TRANSFER_SIGNING_SALT)


def _unsign(token: str, salt: str) -> tuple[int, int] | None:
    try:
        payload = loads(
            token or "",
            salt=salt,
            max_age=TOKEN_MAX_AGE_SECONDS,
        )
    except (BadSignature, SignatureExpired, TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    try:
        ticket_id = int(payload.get("t"))
        engineer_id = int(payload.get("u"))
    except (TypeError, ValueError):
        return None
    if ticket_id <= 0 or engineer_id <= 0:
        return None
    return ticket_id, engineer_id
