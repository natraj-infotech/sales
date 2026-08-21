"""Session OTP challenge for public renewal confirmation links."""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from html import escape

from django.conf import settings
from django.utils import timezone

from business.models import SubscriptionRenewalInvite
from business.outbound_mail import (
    _graph_configured,
    send_smtp_outbound,
    send_via_microsoft_graph,
)
from business.support_inbox import _detect_imap_host, _is_microsoft_host

logger = logging.getLogger(__name__)

SESSION_KEY = "renewal_otp_challenges"
OTP_TTL_SECONDS = 10 * 60
RESEND_COOLDOWN_SECONDS = 60
MAX_ATTEMPTS = 5

UNAUTHORIZED_EMAIL_ERROR = (
    "This form can only be opened using Email 1, Email 2, or Email 3 "
    "from the client record."
)


def _invite_key(invite: SubscriptionRenewalInvite) -> str:
    return str(invite.pk)


def _hash_otp(invite_id: str, email: str, code: str) -> str:
    payload = f"{invite_id}:{email.strip().lower()}:{code}".encode()
    return hmac.new(
        settings.SECRET_KEY.encode(),
        payload,
        hashlib.sha256,
    ).hexdigest()


def _store(request, invite: SubscriptionRenewalInvite, payload: dict) -> None:
    challenges = request.session.get(SESSION_KEY) or {}
    challenges[_invite_key(invite)] = payload
    request.session[SESSION_KEY] = challenges
    request.session.modified = True


def clear_otp_state(request, invite: SubscriptionRenewalInvite) -> None:
    challenges = request.session.get(SESSION_KEY) or {}
    challenges.pop(_invite_key(invite), None)
    request.session[SESSION_KEY] = challenges
    request.session.modified = True


def get_otp_state(request, invite: SubscriptionRenewalInvite) -> dict:
    challenges = request.session.get(SESSION_KEY) or {}
    state = challenges.get(_invite_key(invite)) or {}
    return state if isinstance(state, dict) else {}


def verified_submitter_email(request, invite: SubscriptionRenewalInvite) -> str:
    state = get_otp_state(request, invite)
    email = (state.get("email") or "").strip()
    if not state.get("verified") or not email:
        return ""
    if not invite.is_allowed_submitter(email):
        return ""
    return email


def _normalize_otp(raw: str) -> str:
    return "".join(ch for ch in (raw or "") if ch.isdigit())


def issue_and_send_otp(
    request,
    invite: SubscriptionRenewalInvite,
    email: str,
    *,
    force: bool = False,
) -> tuple[bool, str | None]:
    email = (email or "").strip()
    if not invite.is_allowed_submitter(email):
        return False, UNAUTHORIZED_EMAIL_ERROR

    now_ts = timezone.now().timestamp()
    state = get_otp_state(request, invite)
    last_sent = float(state.get("sent_at") or 0)
    same_email = (state.get("email") or "").strip().lower() == email.lower()
    if (
        not force
        and same_email
        and last_sent
        and now_ts - last_sent < RESEND_COOLDOWN_SECONDS
    ):
        wait = int(RESEND_COOLDOWN_SECONDS - (now_ts - last_sent))
        return False, f"Please wait {max(wait, 1)} seconds before requesting a new code."

    code = f"{secrets.randbelow(1_000_000):06d}"
    ok, err = send_renewal_otp_email(invite.company, email, code)
    if not ok:
        return False, err or "Could not send the verification code. Please try again."

    _store(
        request,
        invite,
        {
            "email": email,
            "code_hash": _hash_otp(_invite_key(invite), email, code),
            "expires_at": now_ts + OTP_TTL_SECONDS,
            "sent_at": now_ts,
            "attempts": 0,
            "verified": False,
        },
    )
    return True, None


def verify_otp(
    request,
    invite: SubscriptionRenewalInvite,
    raw_code: str,
) -> tuple[bool, str | None]:
    state = get_otp_state(request, invite)
    email = (state.get("email") or "").strip()
    if not email or not state.get("code_hash"):
        return False, "Request a verification code first."
    if not invite.is_allowed_submitter(email):
        clear_otp_state(request, invite)
        return False, UNAUTHORIZED_EMAIL_ERROR

    now_ts = timezone.now().timestamp()
    expires_at = float(state.get("expires_at") or 0)
    if now_ts > expires_at:
        clear_otp_state(request, invite)
        return False, "That code has expired. Request a new one."

    attempts = int(state.get("attempts") or 0)
    if attempts >= MAX_ATTEMPTS:
        clear_otp_state(request, invite)
        return False, "Too many incorrect attempts. Request a new code."

    code = _normalize_otp(raw_code)
    expected = str(state.get("code_hash") or "")
    given = _hash_otp(_invite_key(invite), email, code)
    hashes_match = (
        len(code) == 6
        and len(expected) == len(given)
        and hmac.compare_digest(expected, given)
    )
    if not hashes_match:
        state["attempts"] = attempts + 1
        _store(request, invite, state)
        remaining = MAX_ATTEMPTS - state["attempts"]
        if remaining <= 0:
            clear_otp_state(request, invite)
            return False, "Too many incorrect attempts. Request a new code."
        return False, "That code is incorrect. Please try again."

    state["verified"] = True
    state["code_hash"] = ""
    _store(request, invite, state)
    return True, None


def send_renewal_otp_email(company, to_email: str, code: str) -> tuple[bool, str | None]:
    sender = (company.outgoing_email or "").strip()
    password = (company.outgoing_email_app_password or "").strip()
    if not sender or not password:
        return False, "Outgoing email is not configured for this company."

    to_email = (to_email or "").strip()
    if not to_email:
        return False, "A destination email is required."

    company_name = (company.company_name or "Subscription tracker").strip()
    subject = f"{company_name}: your renewal verification code"
    body = (
        f"Your verification code is {code}.\n\n"
        "Enter this code to confirm subscription renewal quantities. "
        "It expires in 10 minutes.\n\n"
        "If you did not request this, you can ignore this email."
    )
    html_body = """
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin:0;padding:0;background-color:#f1f5f9;">
  <tr>
    <td align="center" style="padding:24px 12px;">
      <table role="presentation" width="480" cellpadding="0" cellspacing="0" border="0" style="width:480px;max-width:480px;background-color:#ffffff;border:1px solid #e2e8f0;border-radius:8px;">
        <tr>
          <td style="padding:24px 28px;font-family:Segoe UI,Arial,sans-serif;">
            <p style="margin:0 0 8px 0;font-size:12px;letter-spacing:0.4px;text-transform:uppercase;color:#2563eb;font-weight:600;">
              Renewal confirmation
            </p>
            <h1 style="margin:0 0 16px 0;font-size:20px;line-height:1.3;color:#1e3a8a;">
              Your verification code
            </h1>
            <p style="margin:0 0 16px 0;font-size:15px;line-height:1.55;color:#334155;">
              Use this code to confirm renewal quantities for <strong>{company_name}</strong>.
            </p>
            <p style="margin:0 0 16px 0;font-size:28px;letter-spacing:0.28em;font-weight:700;color:#1e3a8a;text-align:center;">
              {code}
            </p>
            <p style="margin:0;font-size:13px;line-height:1.5;color:#64748b;">
              This code expires in 10 minutes. If you did not request it, you can ignore this email.
            </p>
          </td>
        </tr>
      </table>
    </td>
  </tr>
</table>
""".strip().format(company_name=escape(company_name), code=escape(code))

    use_graph = _is_microsoft_host(_detect_imap_host(sender)) and _graph_configured()
    if use_graph:
        ok, err, _msgid = send_via_microsoft_graph(
            sender,
            [to_email],
            subject,
            html_body,
        )
        if ok:
            return True, None
        logger.warning(
            "Graph OTP send failed for %s, falling back to SMTP: %s",
            sender,
            err,
        )

    return send_smtp_outbound(
        sender,
        password,
        [to_email],
        subject,
        body,
        html_body=html_body,
    )
