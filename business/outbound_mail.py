"""SMTP helpers for outbound emails."""

from __future__ import annotations

import logging

from django.core.mail import EmailMessage, get_connection

logger = logging.getLogger(__name__)


def _smtp_for_address(email_addr: str) -> tuple[str, int]:
    domain = (email_addr.split("@")[-1] or "").lower().strip()
    if domain in {"gmail.com", "googlemail.com"}:
        return "smtp.gmail.com", 587
    if domain in {"outlook.com", "hotmail.com", "live.com", "msn.com"}:
        return "smtp.office365.com", 587
    if "onmicrosoft.com" in domain:
        return "smtp.office365.com", 587
    return "smtp.gmail.com", 587


def send_smtp_outbound(
    from_email: str,
    app_password: str,
    to_addresses: list[str],
    subject: str,
    body: str,
) -> bool:
    """Return True if SMTP accepted the message."""
    sender = (from_email or "").strip()
    password = (app_password or "").strip()
    if not sender or not password:
        return False
    recipients = [t.strip() for t in to_addresses if (t or "").strip()]
    if not recipients:
        return False

    host, port = _smtp_for_address(sender)
    try:
        conn = get_connection(
            backend="django.core.mail.backends.smtp.EmailBackend",
            host=host,
            port=port,
            username=sender,
            password=password,
            use_tls=True,
        )
        EmailMessage(
            subject,
            body,
            sender,
            recipients,
            connection=conn,
        ).send(fail_silently=False)
        return True
    except Exception:
        logger.exception("SMTP send failed for sender %s", sender)
        return False


def send_company_outbound(
    company,
    to_addresses: list[str],
    subject: str,
    body: str,
) -> bool:
    """Backward-compatible wrapper using company configured sender."""
    sender = (company.outgoing_email or "").strip()
    password = (company.outgoing_email_app_password or "").strip()
    if not sender or not password:
        logger.warning(
            "Outbound mail skipped: company %s missing outgoing_email or app password.",
            company.pk,
        )
        return False
    return send_smtp_outbound(sender, password, to_addresses, subject, body)
