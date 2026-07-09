"""SMTP helpers for outbound emails."""

from __future__ import annotations

import logging
import smtplib
from collections.abc import Sequence

from django.conf import settings
from django.core.mail import EmailMessage, EmailMultiAlternatives, get_connection

logger = logging.getLogger(__name__)

Attachment = tuple[str, bytes, str]


def _smtp_for_address(email_addr: str) -> tuple[str, int]:
    host_override = getattr(settings, "OUTBOUND_SMTP_HOST", "").strip()
    if host_override:
        port = int(getattr(settings, "OUTBOUND_SMTP_PORT", 587) or 587)
        return host_override, port

    from business.support_inbox import _detect_imap_host

    imap_host = _detect_imap_host(email_addr).lower()
    if "gmail" in imap_host or "google" in imap_host:
        return "smtp.gmail.com", 587
    if "office365" in imap_host or "outlook" in imap_host:
        return "smtp.office365.com", 587
    if "yahoo" in imap_host:
        return "smtp.mail.yahoo.com", 587
    if "zoho" in imap_host:
        return "smtp.zoho.com", 587
    if "yandex" in imap_host:
        return "smtp.yandex.ru", 587

    domain = (email_addr.split("@")[-1] or "").lower().strip()
    if domain in {"gmail.com", "googlemail.com"}:
        return "smtp.gmail.com", 587
    if domain in {"outlook.com", "hotmail.com", "live.com", "msn.com"}:
        return "smtp.office365.com", 587
    if "onmicrosoft.com" in domain:
        return "smtp.office365.com", 587

    # Business custom domains are usually Microsoft 365 or Google Workspace.
    return "smtp.office365.com", 587


def _friendly_smtp_error(exc: Exception, *, host: str, sender: str) -> str:
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        if "office365" in host or "outlook" in host:
            return (
                f"Microsoft 365 SMTP login failed for {sender}. "
                "Use the full Outlook email address, an app password (not your normal "
                "login password if MFA is on), and ensure SMTP AUTH is enabled for the "
                "mailbox in Microsoft 365 admin."
            )
        if "gmail" in host or "google" in host:
            return (
                f"Gmail SMTP login failed for {sender}. "
                "Use a Google app password, not your regular account password."
            )
        return (
            f"SMTP login failed for {sender} via {host}. "
            "Check the outgoing email address and app password."
        )
    return f"SMTP send failed via {host} for {sender}: {exc}"


def send_smtp_outbound(
    from_email: str,
    app_password: str,
    to_addresses: list[str],
    subject: str,
    body: str,
    html_body: str | None = None,
    cc_addresses: Sequence[str] | None = None,
    attachments: Sequence[Attachment] | None = None,
) -> tuple[bool, str | None]:
    """Return (success, error_message)."""
    sender = (from_email or "").strip()
    password = (app_password or "").strip()
    if not sender or not password:
        return False, "Outgoing email or app password is missing."
    recipients = [t.strip() for t in to_addresses if (t or "").strip()]
    cc_recipients = [t.strip() for t in (cc_addresses or ()) if (t or "").strip()]
    if not recipients:
        return False, "No recipient email addresses were provided."

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
        if html_body:
            message = EmailMultiAlternatives(
                subject,
                body,
                sender,
                recipients,
                connection=conn,
                cc=cc_recipients,
            )
            message.attach_alternative(html_body, "text/html")
        else:
            message = EmailMessage(
                subject,
                body,
                sender,
                recipients,
                connection=conn,
                cc=cc_recipients,
            )
        for filename, content, mimetype in attachments or ():
            message.attach(filename, content, mimetype)
        message.send(fail_silently=False)
        return True, None
    except Exception as exc:
        error = _friendly_smtp_error(exc, host=host, sender=sender)
        logger.exception("SMTP send failed for sender %s via %s", sender, host)
        return False, error


def send_company_outbound(
    company,
    to_addresses: list[str],
    subject: str,
    body: str,
    html_body: str | None = None,
    cc_addresses: Sequence[str] | None = None,
    attachments: Sequence[Attachment] | None = None,
) -> tuple[bool, str | None]:
    """Backward-compatible wrapper using company configured sender."""
    sender = (company.outgoing_email or "").strip()
    password = (company.outgoing_email_app_password or "").strip()
    if not sender or not password:
        logger.warning(
            "Outbound mail skipped: company %s missing outgoing_email or app password.",
            company.pk,
        )
        return False, "Configure outgoing SMTP email and app password in Company profile."
    return send_smtp_outbound(
        sender,
        password,
        to_addresses,
        subject,
        body,
        html_body=html_body,
        cc_addresses=cc_addresses,
        attachments=attachments,
    )
