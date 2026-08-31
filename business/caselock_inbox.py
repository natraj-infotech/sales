"""Poll the company Case lock mailbox and email one-time form links to senders."""

from __future__ import annotations

import email.policy
import imaplib
import logging
from email import message_from_bytes
from email.utils import parsedate_to_datetime
from urllib.parse import quote

import requests
from django.conf import settings
from django.db import IntegrityError
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from accounts.models import CompanyProfile
from business.models import CaseLockInvite, CaseLockSubmission
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


def _aware(dt):
    if dt is None:
        return timezone.now()
    if timezone.is_naive(dt):
        return timezone.make_aware(dt, timezone.get_current_timezone())
    return dt


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


def _send_mail_via_company(company: CompanyProfile, to_email: str, subject: str, body: str) -> bool:
    mailbox = (company.caselock_email or "").strip()
    password = company.caselock_email_app_password or ""
    if not mailbox:
        return False
    ok = False
    mailbox_host = _detect_imap_host(mailbox)
    if _is_microsoft_host(mailbox_host):
        try:
            token = _graph_access_token()
            _graph_send_mail(token, mailbox, to_email, subject, body)
            ok = True
        except Exception:
            logger.exception("Graph sendMail failed for Case lock mailbox %s", mailbox)
    if not ok:
        ok, err = send_smtp_outbound(
            mailbox,
            password,
            [to_email],
            subject,
            body,
        )
        return ok
    return ok


def _should_skip_message(msg) -> bool:
    auto = (msg.get("Auto-Submitted") or "").lower()
    if auto and auto not in ("no", ""):
        return True
    prec = (msg.get("Precedence") or "").lower()
    if prec in ("bulk", "junk", "list"):
        return True
    return False


def _send_invite_email(company: CompanyProfile, invite: CaseLockInvite, sender: str) -> None:
    url = invite_form_absolute_url(invite)
    body = (
        "Thank you for contacting us.\n\n"
        f"Please submit your Case lock details using this secure link:\n{url}\n\n"
        "If you did not send that message, you can ignore this email.\n"
    )
    subj = "Case lock details"
    ok = _send_mail_via_company(company, sender, subj, body)
    invite.email_sent_at = timezone.now() if ok else None
    invite.save(update_fields=["email_sent_at"])
    if not ok:
        logger.info(
            "Case lock invite %s — outbound mail failed for mailbox %s; link: %s",
            invite.pk,
            company.caselock_email,
            url,
        )


def send_caselock_decision_email(submission: CaseLockSubmission) -> bool:
    invite = submission.invite
    company = invite.company
    if company is None and invite.inbox_id:
        company = invite.inbox.company
    to_email = (invite.sender_email or "").strip()
    if not to_email or company is None:
        return False
    if submission.status == CaseLockSubmission.Status.APPROVED:
        subject = f"Final case lock — {submission.product_name}"
        body = (
            f"Your case lock request for product '{submission.product_name}' "
            "has been finalized.\n\n"
            f"Customer: {submission.client_name}\n"
            f"GST: {submission.client_gst or '—'}\n"
            f"Quantity: {submission.product_quantity}\n"
        )
    elif submission.status == CaseLockSubmission.Status.DECLINED:
        subject = f"Case lock declined — {submission.product_name}"
        body = (
            f"Your case lock request for product '{submission.product_name}' "
            "has been declined.\n\n"
            f"Customer: {submission.client_name}\n"
            f"Quantity: {submission.product_quantity}\n"
        )
    else:
        return False
    return _send_mail_via_company(company, to_email, subject, body)


def _create_invite_and_notify(
    company: CompanyProfile,
    mid: str,
    sender: str,
    subject: str,
    received_at=None,
) -> bool:
    mailbox = (company.caselock_email or "").strip()
    if not (sender or "").strip():
        logger.info("Case lock: skip message without sender (company %s)", company.pk)
        return False
    sender_clean = sender.strip()[:254]
    if mailbox and sender_clean.lower() == mailbox.lower():
        return False
    try:
        invite = CaseLockInvite.objects.create(
            company=company,
            source_message_id=mid,
            sender_email=sender_clean,
            mail_subject=(subject or "")[:255],
            received_at=_aware(received_at),
        )
    except IntegrityError:
        return False
    _send_invite_email(company, invite, sender_clean)
    return True


def _invite_exists(company: CompanyProfile, mid: str) -> bool:
    return CaseLockInvite.objects.filter(company=company, source_message_id=mid).exists()


def _fetch_caselock_via_graph(company: CompanyProfile, mark_seen: bool) -> tuple[int, list[str]]:
    log: list[str] = []
    created = 0
    mailbox = (company.caselock_email or "").strip()
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

        if _invite_exists(company, mid):
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
        received_raw = item.get("receivedDateTime") or ""
        received_at = parse_datetime(received_raw) if received_raw else None
        if not sender or sender.lower() == mailbox.lower():
            if mark_seen and gid:
                try:
                    _graph_mark_read(access_token, mailbox, gid)
                except Exception:
                    pass
            continue

        if _create_invite_and_notify(company, mid, sender, subject, received_at=received_at):
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


def fetch_caselock_for_company(company: CompanyProfile) -> tuple[int, list[str]]:
    logs: list[str] = []
    mailbox = (company.caselock_email or "").strip()
    password = company.caselock_email_app_password or ""
    if not mailbox:
        return 0, logs

    created_total = 0
    mark_seen = getattr(settings, "SUPPORT_IMAP_MARK_SEEN", True)
    host = _detect_imap_host(mailbox)
    port = 993
    mailbox_name = getattr(settings, "SUPPORT_IMAP_MAILBOX", "INBOX")

    if _is_microsoft_host(host):
        try:
            c, l = _fetch_caselock_via_graph(company, mark_seen=mark_seen)
            return c, l
        except Exception as e:
            logs.append(f"[CaseLock] Graph error ({mailbox}): {e}")
            if not password.strip():
                return 0, logs

    if not password.strip():
        logs.append(
            f"[CaseLock] {mailbox}: add a mailbox app password for IMAP, "
            "or use Microsoft 365 with Graph app credentials in settings."
        )
        return 0, logs

    imap: imaplib.IMAP4_SSL | None = None
    try:
        imap = imaplib.IMAP4_SSL(host, port, timeout=60)
        imap.login(mailbox, password)
        typ, _ = imap.select(mailbox_name)
        if typ != "OK":
            logs.append(
                f"[CaseLock] could not select mailbox {mailbox_name!r} for {mailbox}."
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

            if _invite_exists(company, mid):
                if mark_seen:
                    imap.store(num, "+FLAGS", "\\Seen")
                continue

            subject = _decode_header(msg.get("Subject", "")) or "(no subject)"
            subject = subject.strip()[:255]
            from_decoded = _decode_header(msg.get("From", ""))
            sender = parse_rfc5322_addr(from_decoded) or from_decoded[:254]
            received_at = None
            try:
                date_hdr = msg.get("Date")
                if date_hdr:
                    received_at = parsedate_to_datetime(str(date_hdr))
            except (TypeError, ValueError, OverflowError):
                received_at = None

            if _create_invite_and_notify(
                company, mid, sender, subject, received_at=received_at
            ):
                created_total += 1

            if mark_seen:
                imap.store(num, "+FLAGS", "\\Seen")

        logs.append(
            f"[CaseLock {mailbox}] IMAP: {created_total} invite(s) "
            f"from {len(ids)} unread message(s)."
        )
    except imaplib.IMAP4.error as e:
        logs.append(f"[CaseLock] IMAP error ({mailbox}): {e}")
    except OSError as e:
        logs.append(f"[CaseLock] connection error ({mailbox}): {e}")
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


def maybe_fetch_on_dashboard(company: CompanyProfile, *, min_interval_seconds: int = 20) -> dict | None:
    from django.core.cache import cache

    key = f"caselock_inbox_poll:{company.pk}"
    if cache.get(key):
        return None
    cache.set(key, 1, timeout=max(5, int(min_interval_seconds)))
    try:
        created, logs = fetch_caselock_for_company(company)
        return {"created": created, "logs": logs}
    except Exception:
        logger.exception("Dashboard Case lock poll failed for company %s", company.pk)
        return {"created": 0, "logs": ["poll failed"]}


def fetch_all_caselock_inboxes() -> dict:
    created_total = 0
    logs: list[str] = []
    qs = CompanyProfile.objects.exclude(caselock_email="").order_by("pk")
    for company in qs:
        c, batch_logs = fetch_caselock_for_company(company)
        created_total += c
        logs.extend(batch_logs)
    return {"created": created_total, "logs": logs[-40:]}
