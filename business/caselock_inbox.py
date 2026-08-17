"""Poll Case lock mailboxes and email one-time form links to senders."""

from __future__ import annotations

import email.policy
import imaplib
import logging
from email import message_from_bytes
from urllib.parse import quote

import requests
from django.conf import settings
from django.db import IntegrityError
from django.urls import reverse
from django.utils import timezone

from business.models import CaseLockInbox, CaseLockInvite, CaseLockSubmission
from business.outbound_mail import send_smtp_outbound
from business.support_inbox import (
    _decode_header,
    _detect_imap_host,
    _fetch_rfc822_bytes,
    _graph_access_token,
    _graph_list_unread_inbox,
    _graph_mark_read,
    _is_microsoft_host,
    parse_rfc5322_addr,
    stable_fallback_id,
)

logger = logging.getLogger(__name__)


def _public_base_url() -> str:
    base = (getattr(settings, "PUBLIC_BASE_URL", "") or "").strip().rstrip("/")
    if base:
        return base
    return "http://127.0.0.1:8000"


def invite_form_absolute_url(invite: CaseLockInvite) -> str:
    path = reverse("caselock_public_form", kwargs={"invite_id": str(invite.pk)})
    return f"{_public_base_url()}{path}"


def _graph_send_mail(
    access_token: str,
    mailbox_email: str,
    to_email: str,
    subject: str,
    body: str,
) -> None:
    url = f"https://graph.microsoft.com/v1.0/users/{quote(mailbox_email)}/sendMail"
    payload = {
        "message": {
            "subject": subject,
            "body": {"contentType": "Text", "content": body},
            "toRecipients": [
                {"emailAddress": {"address": to_email}},
            ],
        },
        "saveToSentItems": True,
    }
    resp = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=30,
    )
    resp.raise_for_status()


def _send_mail_via_inbox(inbox: CaseLockInbox, to_email: str, subject: str, body: str) -> bool:
    ok = False
    mailbox_host = _detect_imap_host(inbox.mailbox_email)
    if _is_microsoft_host(mailbox_host):
        try:
            token = _graph_access_token()
            _graph_send_mail(token, inbox.mailbox_email, to_email, subject, body)
            ok = True
        except Exception:
            logger.exception("Graph sendMail failed for mailbox %s", inbox.mailbox_email)
    if not ok:
        ok, err = send_smtp_outbound(
            inbox.mailbox_email,
            inbox.mailbox_app_password,
            [to_email],
            subject,
            body,
        )
        return ok


def _should_skip_message(msg) -> bool:
    auto = (msg.get("Auto-Submitted") or "").lower()
    if auto and auto not in ("no", ""):
        return True
    prec = (msg.get("Precedence") or "").lower()
    if prec in ("bulk", "junk", "list"):
        return True
    return False


def _send_invite_email(inbox: CaseLockInbox, invite: CaseLockInvite, sender: str) -> None:
    url = invite_form_absolute_url(invite)
    product_name = inbox.product.name
    body = (
        f"You emailed us about {product_name}.\n\n"
        f"Please submit your Case lock details using this secure link:\n{url}\n\n"
        "If you did not send that message, you can ignore this email.\n"
    )
    subj = f"Case lock details — {product_name}"
    ok = _send_mail_via_inbox(inbox, sender, subj, body)
    invite.email_sent_at = timezone.now() if ok else None
    invite.save(update_fields=["email_sent_at"])
    if not ok:
        logger.info(
            "Case lock invite %s — outbound mail failed for mailbox %s; link: %s",
            invite.pk,
            inbox.mailbox_email,
            url,
        )


def send_caselock_decision_email(submission: CaseLockSubmission) -> bool:
    inbox = submission.invite.inbox
    to_email = (submission.invite.sender_email or "").strip()
    if not to_email:
        return False
    if submission.status == CaseLockSubmission.Status.APPROVED:
        decision = "approved"
    elif submission.status == CaseLockSubmission.Status.DECLINED:
        decision = "declined"
    else:
        return False
    subject = f"Case lock {decision} — {submission.product_name}"
    body = (
        f"Your case lock request for product '{submission.product_name}' has been {decision}.\n\n"
        f"Vendor: {submission.vendor_name}\n"
        f"Client: {submission.client_name}\n"
        f"Quantity: {submission.product_quantity}\n"
    )
    return _send_mail_via_inbox(inbox, to_email, subject, body)


def _create_invite_and_notify(
    inbox: CaseLockInbox,
    mid: str,
    sender: str,
    subject: str,
) -> bool:
    if not (sender or "").strip():
        logger.info("Case lock: skip message without sender (inbox %s)", inbox.pk)
        return False
    sender_clean = sender.strip()[:254]
    if sender_clean.lower() == inbox.mailbox_email.strip().lower():
        return False
    try:
        invite = CaseLockInvite.objects.create(
            inbox=inbox,
            source_message_id=mid,
            sender_email=sender_clean,
            mail_subject=(subject or "")[:255],
        )
    except IntegrityError:
        return False
    _send_invite_email(inbox, invite, sender_clean)
    return True


def _fetch_caselock_via_graph(inbox: CaseLockInbox, mark_seen: bool) -> tuple[int, list[str]]:
    log: list[str] = []
    created = 0
    mailbox = inbox.mailbox_email.strip()
    access_token = _graph_access_token()
    items = _graph_list_unread_inbox(
        access_token,
        mailbox,
        select="id,subject,from,internetMessageId,bodyPreview,receivedDateTime",
        unread_only=True,
    )

    for item in items:
        mid = (item.get("internetMessageId") or "").strip()
        gid = (item.get("id") or "").strip()
        if not mid:
            mid = f"graph:{gid}"
        if not mid:
            continue

        if CaseLockInvite.objects.filter(inbox=inbox, source_message_id=mid).exists():
            if mark_seen and gid:
                try:
                    _graph_mark_read(access_token, mailbox, gid)
                except Exception:
                    pass
            continue

        subject = (item.get("subject") or "(no subject)").strip()[:255]
        from_obj = item.get("from") or {}
        sender = (
            ((from_obj.get("emailAddress") or {}).get("address") or "").strip()
        )
        if not sender or sender.lower() == mailbox.lower():
            if mark_seen and gid:
                try:
                    _graph_mark_read(access_token, mailbox, gid)
                except Exception:
                    pass
            continue

        try:
            invite = CaseLockInvite.objects.create(
                inbox=inbox,
                source_message_id=mid,
                sender_email=sender[:254],
                mail_subject=subject,
            )
        except IntegrityError:
            if mark_seen and gid:
                try:
                    _graph_mark_read(access_token, mailbox, gid)
                except Exception:
                    pass
            continue

        _send_invite_email(inbox, invite, sender)
        created += 1

        if mark_seen and gid:
            try:
                _graph_mark_read(access_token, mailbox, gid)
            except Exception:
                pass

    log.append(
        f"[CaseLock {mailbox}] Microsoft Graph: {created} new invite(s) "
        f"from {len(items)} unread."
    )
    return created, log


def fetch_caselock_for_inbox(inbox: CaseLockInbox) -> tuple[int, list[str]]:
    logs: list[str] = []
    if not inbox.is_active:
        return 0, logs
    if not (inbox.mailbox_email or "").strip():
        return 0, logs

    created_total = 0
    mark_seen = getattr(settings, "SUPPORT_IMAP_MARK_SEEN", True)
    host = _detect_imap_host(inbox.mailbox_email)
    port = 993
    mailbox_name = getattr(settings, "SUPPORT_IMAP_MAILBOX", "INBOX")

    if _is_microsoft_host(host):
        try:
            c, l = _fetch_caselock_via_graph(inbox, mark_seen=mark_seen)
            return c, l
        except Exception as e:
            logs.append(f"[CaseLock] Graph error ({inbox.mailbox_email}): {e}")
            if not (inbox.mailbox_app_password or "").strip():
                return 0, logs

    if not (inbox.mailbox_app_password or "").strip():
        logs.append(
            f"[CaseLock] {inbox.mailbox_email}: add a mailbox app password for IMAP, "
            "or use Microsoft 365 with Graph app credentials in settings."
        )
        return 0, logs

    imap: imaplib.IMAP4_SSL | None = None
    try:
        imap = imaplib.IMAP4_SSL(host, port, timeout=60)
        imap.login(inbox.mailbox_email, inbox.mailbox_app_password)
        typ, _ = imap.select(mailbox_name)
        if typ != "OK":
            logs.append(
                f"[CaseLock] could not select mailbox {mailbox_name!r} "
                f"for {inbox.mailbox_email}."
            )
            return 0, logs

        typ, data = imap.search(None, "UNSEEN")
        if typ != "OK" or not data or not data[0]:
            return 0, logs

        ids = data[0].split()
        for num in ids:
            raw = _fetch_rfc822_bytes(imap, num)
            if not raw:
                continue
            msg = message_from_bytes(raw, policy=email.policy.default)
            if _should_skip_message(msg):
                if mark_seen:
                    imap.store(num, "+FLAGS", "\\Seen")
                continue

            mid = (msg.get("Message-ID") or "").strip()
            if not mid:
                mid = stable_fallback_id(raw)

            if CaseLockInvite.objects.filter(inbox=inbox, source_message_id=mid).exists():
                if mark_seen:
                    imap.store(num, "+FLAGS", "\\Seen")
                continue

            subject = _decode_header(msg.get("Subject", "")) or "(no subject)"
            subject = subject.strip()[:255]
            from_decoded = _decode_header(msg.get("From", ""))
            sender = parse_rfc5322_addr(from_decoded) or from_decoded[:254]

            if _create_invite_and_notify(inbox, mid, sender, subject):
                created_total += 1

            if mark_seen:
                imap.store(num, "+FLAGS", "\\Seen")

        logs.append(
            f"[CaseLock {inbox.mailbox_email}] IMAP: {created_total} invite(s) "
            f"from {len(ids)} unread message(s)."
        )
    except imaplib.IMAP4.error as e:
        logs.append(f"[CaseLock] IMAP error ({inbox.mailbox_email}): {e}")
    except OSError as e:
        logs.append(f"[CaseLock] connection error ({inbox.mailbox_email}): {e}")
        logger.exception("Case lock IMAP connection error")
    finally:
        if imap is not None:
            try:
                imap.close()
            except Exception:
                pass
            try:
                imap.logout()
            except Exception:
                pass

    return created_total, logs


def fetch_all_caselock_inboxes() -> dict:
    created_total = 0
    logs: list[str] = []
    qs = CaseLockInbox.objects.filter(is_active=True).select_related(
        "company", "company__user", "product"
    )
    for inbox in qs.order_by("pk"):
        c, batch_logs = fetch_caselock_for_inbox(inbox)
        created_total += c
        logs.extend(batch_logs)
    return {"created": created_total, "logs": logs[-40:]}
