"""Signed tokens for public ticket links in assignment and digest email."""

from __future__ import annotations

from django.core.signing import BadSignature, SignatureExpired, dumps, loads

SIGNING_SALT = "business.ticket-close"
TRANSFER_SIGNING_SALT = "business.ticket-transfer"
VIEW_SIGNING_SALT = "business.ticket-view"
TOKEN_MAX_AGE_SECONDS = 120 * 24 * 60 * 60


def sign_ticket_close_token(ticket_id, engineer_id) -> str:
    payload = {"t": int(ticket_id), "u": int(engineer_id)}
    return dumps(payload, salt=SIGNING_SALT)


def sign_ticket_transfer_token(ticket_id, engineer_id) -> str:
    payload = {"t": int(ticket_id), "u": int(engineer_id)}
    return dumps(payload, salt=TRANSFER_SIGNING_SALT)


def sign_ticket_view_token(ticket_id, company_id) -> str:
    payload = {"t": int(ticket_id), "c": int(company_id)}
    return dumps(payload, salt=VIEW_SIGNING_SALT)


def unsign_ticket_close_token(token: str) -> tuple[int, int] | None:
    return _unsign(token, SIGNING_SALT)


def unsign_ticket_transfer_token(token: str) -> tuple[int, int] | None:
    return _unsign(token, TRANSFER_SIGNING_SALT)


def unsign_ticket_view_token(token: str) -> tuple[int, int] | None:
    return _unsign(token, VIEW_SIGNING_SALT, second_key="c")


def _unsign(token: str, salt: str, second_key: str = "u") -> tuple[int, int] | None:
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
        second_id = int(payload.get(second_key))
    except (TypeError, ValueError):
        return None
    if ticket_id <= 0 or second_id <= 0:
        return None
    return ticket_id, second_id
