"""Outlook read tracking for renewal reminder emails."""

from __future__ import annotations

import base64
import logging
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone as dt_timezone
from urllib.parse import quote

import requests
from django.conf import settings
from django.urls import reverse
from django.utils import timezone

from business.models import SubscriptionRenewalInvite
from business.outbound_mail import (
    _smtp_for_address,
    send_smtp_outbound,
    send_via_microsoft_graph,
)
from business.support_inbox import (
    _detect_imap_host,
    _graph_access_token,
    _is_microsoft_host,
)

logger = logging.getLogger(__name__)

TRANSPARENT_GIF = base64.b64decode(
    b"R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"
)
READ_SOURCE_RECEIPT = "outlook_receipt"
READ_SOURCE_OPEN = "outlook_open"

READ_RECEIPT_PREFIXES = (
    "read:",
    "read :",
    "read;",
    "displayed:",
    "displayed :",
)


def _public_base_url() -> str:
    base = (getattr(settings, "PUBLIC_BASE_URL", "") or "").strip().rstrip("/")
    if base:
        return base
    return "http://127.0.0.1:8000"


def renewal_track_pixel_url(invite: SubscriptionRenewalInvite) -> str:
    path = reverse("renewal_track_open", kwargs={"invite_id": str(invite.pk)})
    return f"{_public_base_url()}{path}"


def append_read_tracking_pixel(html_body: str, invite: SubscriptionRenewalInvite) -> str:
    pixel_url = renewal_track_pixel_url(invite)
    pixel = (
        f'<img src="{pixel_url}" width="1" height="1" alt="" '
        'style="display:block;width:1px;height:1px;border:0;" />'
    )
    lower = html_body.lower()
    if "</body>" in lower:
        idx = lower.rfind("</body>")
        return html_body[:idx] + pixel + html_body[idx:]
    return html_body + pixel


def _graph_configured() -> bool:
    return bool(
        getattr(settings, "MICROSOFT_GRAPH_CLIENT_ID", "")
        and getattr(settings, "MICROSOFT_GRAPH_CLIENT_SECRET", "")
    )


def mark_renewal_invite_read(
    invite: SubscriptionRenewalInvite,
    when,
    *,
    source: str,
) -> bool:
    if invite.read_at and invite.read_source == READ_SOURCE_RECEIPT:
        return False
    if invite.read_at and source != READ_SOURCE_RECEIPT:
        return False
    invite.read_at = when
    invite.read_source = source
    invite.save(update_fields=["read_at", "read_source"])
    return True


def _normalize_message_id(value: str) -> str:
    value = (value or "").strip()
    if value.startswith("<") and value.endswith(">"):
        return value[1:-1].strip().lower()
    return value.lower()


def _sender_address(item: dict) -> str:
    return (
        ((item.get("from") or {}).get("emailAddress") or {}).get("address") or ""
    ).strip().lower()


def _parse_graph_datetime(value: str) -> datetime:
    received_at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if timezone.is_naive(received_at):
        received_at = timezone.make_aware(received_at)
    return received_at


def _read_receipt_original_subject(subject: str) -> str:
    receipt_subject = (subject or "").strip()
    lower = receipt_subject.lower()
    for prefix in READ_RECEIPT_PREFIXES:
        if lower.startswith(prefix):
            return receipt_subject[len(prefix) :].strip()
    return ""


def _is_read_receipt_message(subject: str) -> bool:
    return bool(_read_receipt_original_subject(subject))


def _subjects_match_receipt(receipt_subject: str, original_subject: str) -> bool:
    original = (original_subject or "").strip()
    extracted = _read_receipt_original_subject(receipt_subject)
    if not original or not extracted:
        return False
    return extracted.lower() == original.lower()


def _graph_list_recent_messages(
    access_token: str,
    mailbox_email: str,
    since_iso: str,
    *,
    folder: str = "Inbox",
) -> list[dict]:
    url = (
        "https://graph.microsoft.com/v1.0/users/"
        f"{quote(mailbox_email)}/mailFolders/{quote(folder)}/messages"
    )
    params = {
        "$filter": f"receivedDateTime ge {since_iso}",
        "$top": "100",
        "$select": "id,subject,receivedDateTime,from,internetMessageId",
    }
    resp = requests.get(
        url,
        headers={"Authorization": f"Bearer {access_token}"},
        params=params,
        timeout=30,
    )
    if resp.ok:
        return resp.json().get("value", []) or []

    logger.warning(
        "Graph folder poll failed for %s/%s: %s",
        mailbox_email,
        folder,
        resp.text[:300],
    )
    resp = requests.get(
        url,
        headers={"Authorization": f"Bearer {access_token}"},
        params={
            "$top": "100",
            "$orderby": "receivedDateTime desc",
            "$select": "id,subject,receivedDateTime,from,internetMessageId",
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json().get("value", []) or []


def _send_via_graph(
    mailbox_email: str,
    to_addresses: list[str],
    subject: str,
    body: str,
    html_body: str,
    cc_addresses: Sequence[str] | None = None,
    inline_images: Sequence[tuple[str, str, bytes, str]] | None = None,
) -> tuple[bool, str | None, str | None]:
    # `body` kept for call-site compatibility; Graph sends HTML only.
    del body
    return send_via_microsoft_graph(
        mailbox_email,
        to_addresses,
        subject,
        html_body,
        cc_addresses=cc_addresses,
        inline_images=inline_images,
        read_receipt=True,
    )


def send_renewal_email(
    company,
    invite: SubscriptionRenewalInvite,
    to_addresses: list[str],
    subject: str,
    body: str,
    html_body: str,
    cc_addresses: Sequence[str] | None = None,
    inline_images: Sequence[tuple[str, str, bytes, str]] | None = None,
) -> tuple[bool, str | None, str | None]:
    """Send renewal reminder; return (ok, error, internet_message_id)."""
    sender = (company.outgoing_email or "").strip()
    password = (company.outgoing_email_app_password or "").strip()
    if not sender or not password:
        return False, "Configure outgoing SMTP email and app password in Company profile.", None

    html_with_pixel = append_read_tracking_pixel(html_body, invite)
    use_graph = _is_microsoft_host(_detect_imap_host(sender)) and _graph_configured()

    if use_graph:
        ok, err, internet_message_id = _send_via_graph(
            sender,
            to_addresses,
            subject,
            body,
            html_with_pixel,
            cc_addresses=cc_addresses,
            inline_images=inline_images,
        )
        if ok:
            return ok, err, internet_message_id
        logger.warning(
            "Graph renewal send failed for %s, falling back to SMTP: %s",
            sender,
            err,
        )

    ok, err = send_smtp_outbound(
        sender,
        password,
        to_addresses,
        subject,
        body,
        html_body=html_with_pixel,
        cc_addresses=cc_addresses,
        inline_images=inline_images,
    )
    return ok, err, None


def _receipt_matches_invite(item: dict, invite: SubscriptionRenewalInvite) -> bool:
    subject = (item.get("subject") or "").strip()
    if not _is_read_receipt_message(subject):
        return False
    if not _subjects_match_receipt(subject, invite.email_subject):
        return False

    sender = _sender_address(item)
    allowed = {
        (addr or "").strip().lower()
        for addr in invite.sent_to_emails
        if (addr or "").strip()
    }
    return bool(sender) and sender in allowed


def _sync_read_receipts_for_invites(
    mailbox_email: str,
    invites: list[SubscriptionRenewalInvite],
) -> int:
    if not invites:
        return 0

    access_token = _graph_access_token()
    since = min(invite.email_sent_at for invite in invites if invite.email_sent_at)
    if since is None:
        return 0
    since = since - timedelta(minutes=5)
    since_iso = since.astimezone(dt_timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    items: list[dict] = []
    for folder in ("Inbox", "JunkEmail"):
        items.extend(
            _graph_list_recent_messages(access_token, mailbox_email, since_iso, folder=folder)
        )

    updated = 0
    for invite in invites:
        if not invite.email_sent_at:
            continue

        best_read_at: datetime | None = None
        for item in items:
            if not _receipt_matches_invite(item, invite):
                continue
            received_raw = item.get("receivedDateTime")
            if not received_raw:
                continue
            received_at = _parse_graph_datetime(received_raw)
            if received_at <= invite.email_sent_at:
                continue
            if best_read_at is None or received_at > best_read_at:
                best_read_at = received_at

        if best_read_at is None:
            if invite.read_source == READ_SOURCE_RECEIPT:
                invite.read_at = None
                invite.read_source = ""
                invite.save(update_fields=["read_at", "read_source"])
                updated += 1
            continue

        if invite.read_at != best_read_at or invite.read_source != READ_SOURCE_RECEIPT:
            invite.read_at = best_read_at
            invite.read_source = READ_SOURCE_RECEIPT
            invite.save(update_fields=["read_at", "read_source"])
            updated += 1

    return updated


def poll_outlook_read_receipts_for_company(company) -> tuple[int, list[str]]:
    mailbox = (company.outgoing_email or "").strip()
    logs: list[str] = []
    if not mailbox:
        return 0, logs

    invites = list(
        SubscriptionRenewalInvite.objects.filter(
            company=company,
            email_sent_at__isnull=False,
        )
        .exclude(email_subject="")
        .prefetch_related("recipients")
    )
    if not invites:
        return 0, logs

    updated = 0
    if _is_microsoft_host(_detect_imap_host(mailbox)) and _graph_configured():
        try:
            updated = _sync_read_receipts_for_invites(mailbox, invites)
            if updated:
                logs.append(
                    f"{company.company_name}: updated {updated} renewal read status(es) from Outlook."
                )
        except Exception as exc:
            logger.exception("Outlook read receipt poll failed for %s", company.company_name)
            logs.append(f"{company.company_name}: Outlook read receipt poll failed: {exc}")
    elif invites:
        logs.append(
            f"{company.company_name}: Outlook read tracking unavailable "
            "(Microsoft Graph not configured for this mailbox)."
        )
    return updated, logs


def poll_outlook_read_receipts_for_all_companies() -> dict[str, int | list[str]]:
    total = 0
    logs: list[str] = []
    from accounts.models import CompanyProfile

    for company in CompanyProfile.objects.order_by("pk"):
        try:
            updated, company_logs = poll_outlook_read_receipts_for_company(company)
        except Exception as exc:
            logger.exception(
                "Renewal read poll crashed company_id=%s", company.pk
            )
            logs.append(
                f"{company.company_name}: read poll crashed: {exc}"
            )
            continue
        total += updated
        logs.extend(company_logs)
    return {"matched": total, "logs": logs[-30:]}
