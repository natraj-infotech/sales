"""Django email backend that sends helpdesk mail via company SMTP credentials."""

from __future__ import annotations

import logging
import re
from smtplib import SMTPException

from django.core.mail.backends.base import BaseEmailBackend

from accounts.models import CompanyProfile
from business.outbound_mail import send_smtp_outbound

logger = logging.getLogger(__name__)

_ADDR_RE = re.compile(r"<([^>]+)>")


def _extract_email(from_header: str) -> str:
    raw = (from_header or "").strip()
    if not raw:
        return ""
    match = _ADDR_RE.search(raw)
    if match:
        return match.group(1).strip().lower()
    return raw.strip().lower()


def _credentials_for_sender(sender_email: str) -> tuple[str, str] | None:
    email = (sender_email or "").strip().lower()
    if not email:
        return None
    company = (
        CompanyProfile.objects.filter(support_email__iexact=email).first()
        or CompanyProfile.objects.filter(outgoing_email__iexact=email).first()
    )
    if company is None:
        return None
    if (company.support_email or "").strip().lower() == email:
        password = (company.support_email_app_password or "").strip()
        if password:
            return company.support_email.strip(), password
    if (company.outgoing_email or "").strip().lower() == email:
        password = (company.outgoing_email_app_password or "").strip()
        if password:
            return company.outgoing_email.strip(), password
    if (company.outgoing_email_app_password or "").strip():
        return (
            (company.outgoing_email or company.support_email or email).strip(),
            company.outgoing_email_app_password.strip(),
        )
    if (company.support_email_app_password or "").strip():
        return (
            (company.support_email or company.outgoing_email or email).strip(),
            company.support_email_app_password.strip(),
        )
    return None


class CompanySMTPEmailBackend(BaseEmailBackend):
    """
    Routes Django mail (used by django-helpdesk) through the company's
    support/outgoing SMTP app password instead of console/global SMTP.
    """

    def send_messages(self, email_messages):
        if not email_messages:
            return 0
        sent = 0
        for message in email_messages:
            try:
                sender_header = message.from_email or ""
                sender_email = _extract_email(sender_header)
                creds = _credentials_for_sender(sender_email)
                if not creds:
                    msg = (
                        "No company SMTP credentials for From="
                        f"{sender_header}. Set support/outgoing email + app password."
                    )
                    logger.warning(msg)
                    if not self.fail_silently:
                        raise SMTPException(msg)
                    continue

                from_email, password = creds
                to_addrs = list(message.to or [])
                cc_addrs = list(getattr(message, "cc", None) or [])
                html_body = None
                for alt, mimetype in getattr(message, "alternatives", []) or []:
                    if mimetype == "text/html":
                        html_body = alt
                        break
                ok, err = send_smtp_outbound(
                    from_email=from_email,
                    app_password=password,
                    to_addresses=to_addrs,
                    subject=message.subject or "",
                    body=message.body or "",
                    html_body=html_body,
                    cc_addresses=cc_addrs,
                    extra_headers=dict(getattr(message, "extra_headers", {}) or {}),
                )
                if ok:
                    sent += 1
                else:
                    logger.error(
                        "Helpdesk SMTP send failed from=%s subject=%s error=%s",
                        from_email,
                        (message.subject or "")[:120],
                        err,
                    )
                    if not self.fail_silently:
                        raise SMTPException(err or "SMTP send failed")
            except SMTPException:
                if not self.fail_silently:
                    raise
            except Exception as exc:
                logger.exception("Helpdesk email send failed")
                if not self.fail_silently:
                    raise SMTPException(str(exc)) from exc
        return sent
