"""SMTP / Microsoft Graph helpers for outbound emails."""

from __future__ import annotations

import base64
import logging
import smtplib
from collections.abc import Sequence
from email.message import MIMEPart
from urllib.parse import quote

import requests
from django.conf import settings
from django.core.mail import EmailMessage, EmailMultiAlternatives, get_connection

logger = logging.getLogger(__name__)

Attachment = tuple[str, bytes, str]
# (content_id, filename, content_bytes, mimetype)
InlineImage = tuple[str, str, bytes, str]


def _graph_configured() -> bool:
    return bool(
        getattr(settings, "MICROSOFT_GRAPH_CLIENT_ID", "")
        and getattr(settings, "MICROSOFT_GRAPH_CLIENT_SECRET", "")
    )


def send_via_microsoft_graph(
    mailbox_email: str,
    to_addresses: list[str],
    subject: str,
    html_body: str,
    cc_addresses: Sequence[str] | None = None,
    inline_images: Sequence[InlineImage] | None = None,
    file_attachments: Sequence[Attachment] | None = None,
    *,
    read_receipt: bool = False,
) -> tuple[bool, str | None, str | None]:
    """
    Send HTML mail via Microsoft Graph with optional inline (CID) images.

    Uses a single ``sendMail`` request (attachments included) so assign/close
    notifications are not blocked on create + attach + send round-trips.
    Returns (ok, error, internet_message_id).
    """
    from business.support_inbox import _graph_access_token

    access_token = _graph_access_token()
    mailbox = (mailbox_email or "").strip()
    recipients = [t.strip() for t in to_addresses if (t or "").strip()]
    if not mailbox or not recipients:
        return False, "Mailbox or recipient missing for Graph send.", None

    message: dict = {
        "subject": subject,
        "body": {"contentType": "HTML", "content": html_body},
        "toRecipients": [{"emailAddress": {"address": addr}} for addr in recipients],
    }
    if read_receipt:
        message["isReadReceiptRequested"] = True
    cc_recipients = [addr.strip() for addr in (cc_addresses or ()) if addr.strip()]
    if cc_recipients:
        message["ccRecipients"] = [
            {"emailAddress": {"address": addr}} for addr in cc_recipients
        ]

    attachments = []
    for content_id, filename, content, mimetype in inline_images or ():
        attachments.append(
            {
                "@odata.type": "#microsoft.graph.fileAttachment",
                "name": filename or "logo.png",
                "contentType": mimetype or "image/png",
                "contentBytes": base64.b64encode(content).decode("ascii"),
                "contentId": content_id,
                "isInline": True,
            }
        )
    for filename, content, mimetype in file_attachments or ():
        if not content:
            continue
        attachments.append(
            {
                "@odata.type": "#microsoft.graph.fileAttachment",
                "name": filename or "attachment",
                "contentType": mimetype or "application/octet-stream",
                "contentBytes": base64.b64encode(content).decode("ascii"),
                "isInline": False,
            }
        )
    if attachments:
        message["attachments"] = attachments

    send_url = f"https://graph.microsoft.com/v1.0/users/{quote(mailbox)}/sendMail"
    send_resp = requests.post(
        send_url,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        },
        json={"message": message, "saveToSentItems": True},
        timeout=20,
    )
    if not send_resp.ok:
        return False, f"Microsoft Graph sendMail failed: {send_resp.text[:300]}", None
    return True, None, None


def send_outbound(
    from_email: str,
    app_password: str,
    to_addresses: list[str],
    subject: str,
    body: str,
    html_body: str | None = None,
    cc_addresses: Sequence[str] | None = None,
    attachments: Sequence[Attachment] | None = None,
    inline_images: Sequence[InlineImage] | None = None,
    extra_headers: dict[str, str] | None = None,
) -> tuple[bool, str | None]:
    """
    Prefer Microsoft Graph for M365 mailboxes (reliable CID logos), else SMTP.
    """
    from business.support_inbox import _detect_imap_host, _is_microsoft_host

    sender = (from_email or "").strip()
    html = html_body or ""
    use_graph = (
        bool(html)
        and not attachments
        and _is_microsoft_host(_detect_imap_host(sender))
        and _graph_configured()
    )
    if use_graph:
        ok, err, _mid = send_via_microsoft_graph(
            sender,
            to_addresses,
            subject,
            html,
            cc_addresses=cc_addresses,
            inline_images=inline_images,
        )
        if ok:
            return True, None
        logger.warning(
            "Graph outbound failed for %s, falling back to SMTP: %s",
            sender,
            err,
        )

    return send_smtp_outbound(
        from_email,
        app_password,
        to_addresses,
        subject,
        body,
        html_body=html_body,
        cc_addresses=cc_addresses,
        attachments=attachments,
        inline_images=inline_images,
        extra_headers=extra_headers,
    )


class _EmailMultiRelated(EmailMultiAlternatives):
    """
    EmailMultiAlternatives that emits multipart/related when inline CID images
    are attached.

    Django 6 dropped undocumented mixed_subtype='related'. Without related,
    Outlook/Gmail show broken images for cid: references.
    """

    def message(self, *, policy=None):
        kwargs = {}
        if policy is not None:
            kwargs["policy"] = policy
        msg = super().message(**kwargs)
        if (
            msg.get_content_maintype() == "multipart"
            and msg.get_content_subtype() == "mixed"
        ):
            msg.set_type("multipart/related")
        return msg


def _inline_image_part(
    content_id: str,
    filename: str,
    content: bytes,
    mimetype: str,
) -> MIMEPart:
    """Build a Django 6-compatible inline image part with Content-ID."""
    maintype, _, subtype = (mimetype or "image/png").partition("/")
    if maintype != "image":
        maintype, subtype = "image", "png"
    cid = (content_id or "").strip()
    if cid and not (cid.startswith("<") and cid.endswith(">")):
        cid = f"<{cid}>"
    part = MIMEPart()
    part.set_content(
        content,
        maintype=maintype,
        subtype=subtype or "png",
        disposition="inline",
        filename=filename or "logo.png",
        cid=cid or None,
    )
    return part


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


def open_smtp_connection(from_email: str, app_password: str, *, timeout: int = 30):
    """Log in once and return an open connection for several send_smtp_outbound calls.

    Returns None when login fails; callers then fall back to one connection per mail.
    The caller must close() the connection.
    """
    sender = (from_email or "").strip()
    password = (app_password or "").strip()
    if not sender or not password:
        return None
    host, port = _smtp_for_address(sender)
    conn = get_connection(
        backend="django.core.mail.backends.smtp.EmailBackend",
        host=host,
        port=port,
        username=sender,
        password=password,
        use_tls=True,
        timeout=timeout,
    )
    try:
        conn.open()
    except Exception:
        logger.exception("SMTP login failed for sender %s via %s", sender, host)
        return None
    return conn


def send_smtp_outbound(
    from_email: str,
    app_password: str,
    to_addresses: list[str],
    subject: str,
    body: str,
    html_body: str | None = None,
    cc_addresses: Sequence[str] | None = None,
    attachments: Sequence[Attachment] | None = None,
    inline_images: Sequence[InlineImage] | None = None,
    extra_headers: dict[str, str] | None = None,
    connection=None,
) -> tuple[bool, str | None]:
    """Return (success, error_message). Pass `connection` to reuse an open SMTP login."""
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
        conn = connection or get_connection(
            backend="django.core.mail.backends.smtp.EmailBackend",
            host=host,
            port=port,
            username=sender,
            password=password,
            use_tls=True,
        )
        has_inline = bool(inline_images)
        if html_body:
            mail_cls = _EmailMultiRelated if has_inline else EmailMultiAlternatives
            message = mail_cls(
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
        for content_id, filename, content, mimetype in inline_images or ():
            message.attach(
                _inline_image_part(content_id, filename, content, mimetype)
            )
        for filename, content, mimetype in attachments or ():
            message.attach(filename, content, mimetype)
        for header, value in (extra_headers or {}).items():
            if value:
                message.extra_headers[header] = value
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
    inline_images: Sequence[InlineImage] | None = None,
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
        inline_images=inline_images,
    )
