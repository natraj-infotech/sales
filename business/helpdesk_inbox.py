"""Inbound support mail → django-helpdesk tickets (Graph for M365, IMAP via helpdesk)."""

from __future__ import annotations

import base64
import logging
import os
import re

from django.conf import settings
from django.db import IntegrityError
from django.utils import timezone
from helpdesk.models import FollowUp, Ticket
from helpdesk.signals import new_ticket_done

from accounts.models import CompanyProfile
from business.email_body_clean import clean_inbound_email_body, plain_main_body
from business.helpdesk_bridge import (
    ensure_meta,
    get_company_queue,
    sync_company_queue,
)
from business.support_inbox import (
    _detect_imap_host,
    _graph_access_token,
    _graph_list_unread_inbox,
    _graph_mark_read,
    _is_microsoft_host,
)

logger = logging.getLogger(__name__)


_OPEN_FOR_REPLIES = (
    Ticket.OPEN_STATUS,
    Ticket.REOPENED_STATUS,
    Ticket.RESOLVED_STATUS,
)
_BLOCKED_ATTACHMENT_EXT = {
    ".html",
    ".htm",
    ".svg",
    ".js",
    ".exe",
    ".bat",
    ".cmd",
    ".msi",
    ".com",
    ".scr",
    ".xhtml",
}
_MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024


def _already_imported(message_id: str) -> bool:
    mid = (message_id or "").strip()
    if not mid:
        return False
    return FollowUp.objects.filter(message_id=mid).exists()


def _safe_filename(name: str) -> str:
    base = os.path.basename((name or "").replace("\\", "/"))
    base = re.sub(r"[^\w.\- ]+", "_", base).strip(" .") or "attachment"
    return base[:180]


def _norm_cid(value: str) -> str:
    return (value or "").strip().strip("<>").lower()


def _html_cid_keys(raw_html: str) -> set[str]:
    found: set[str] = set()
    for match in re.finditer(r"(?i)cid:([^\"'\s>]+)", raw_html or ""):
        cid = _norm_cid(match.group(1))
        if not cid:
            continue
        found.add(cid)
        found.add(cid.split("@", 1)[0])
    return found


def message_files_from_graph(raw_attachments, raw_html: str) -> list[tuple[str, bytes, str]]:
    """
    Paperclip files, plus images pasted in the main message.

    Letterhead, signature, and quoted-mail logos are left out, including
    when Outlook saves them as image001.png instead of marking them inline.
    """
    from business.email_body_clean import inline_cids_in_main_body, is_mail_chrome_filename

    html = raw_html or ""
    kept_cids = inline_cids_in_main_body(html)
    kept_locals = {cid.split("@", 1)[0] for cid in kept_cids}
    html_cids = _html_cid_keys(html)
    files: list[tuple[str, bytes, str]] = []
    for att in raw_attachments or []:
        odata = att.get("@odata.type") or ""
        if odata and "fileAttachment" not in odata:
            continue
        name = _safe_filename(att.get("name") or "attachment")
        if os.path.splitext(name)[1].lower() in _BLOCKED_ATTACHMENT_EXT:
            continue
        cid = _norm_cid(att.get("contentId") or "")
        local = cid.split("@", 1)[0] if cid else ""
        in_body = bool(cid) and (cid in kept_cids or local in kept_locals)
        referenced = bool(cid) and (cid in html_cids or local in html_cids)
        if in_body:
            pass
        elif att.get("isInline") or referenced or is_mail_chrome_filename(name):
            continue
        encoded = att.get("contentBytes") or ""
        if not encoded:
            continue
        try:
            data = base64.b64decode(encoded)
        except Exception:
            continue
        if not data or len(data) > _MAX_ATTACHMENT_BYTES:
            continue
        mime = (att.get("contentType") or "application/octet-stream")[:255]
        files.append((name, data, mime))
    return files


def _store_message_files(followup, files) -> None:
    from django.core.files.base import ContentFile
    from helpdesk.models import FollowUpAttachment

    for name, data, mime in files or []:
        attachment = FollowUpAttachment(
            followup=followup,
            filename=name,
            mime_type=mime or "application/octet-stream",
            size=len(data),
        )
        attachment.file.save(name, ContentFile(data), save=True)


def _is_our_notification(subject: str) -> bool:
    """Ticket-generated and engineer-assigned mails stay off the customer thread."""
    return bool(
        re.search(
            r"(?i)\[TK-\d+\]\s+(?:"
            r"We have generated a ticket"
            r"|Ticket assigned to you"
            r"|Your ticket has been assigned"
            r"|Your ticket is closed"
            r")",
            subject or "",
        )
    )


def _graph_header(item: dict, name: str) -> str:
    wanted = name.lower()
    for header in item.get("internetMessageHeaders") or []:
        if (header.get("name") or "").lower() == wanted:
            return (header.get("value") or "").strip()
    return ""


def _reference_ids(*header_values: str) -> list[str]:
    found: list[str] = []
    for value in header_values:
        for match in re.findall(r"<[^>]+>", value or ""):
            if match not in found:
                found.append(match)
        stripped = (value or "").strip()
        if stripped and stripped not in found:
            found.append(stripped)
    return [item[:256] for item in found]


def open_ticket_for_conversation(company: CompanyProfile, conversation_id: str):
    """Open ticket whose customer mail shares this Outlook conversation."""
    conv = (conversation_id or "").strip()[:512]
    if not conv:
        return None
    from business.models import SupportTicketMeta

    queue = get_company_queue(company)
    meta = (
        SupportTicketMeta.objects.filter(
            email_conversation_id=conv,
            ticket__queue=queue,
            ticket__status__in=_OPEN_FOR_REPLIES,
        )
        .select_related("ticket")
        .order_by("-ticket_id")
        .first()
    )
    return meta.ticket if meta else None


def open_ticket_for_references(company: CompanyProfile, *header_values: str):
    """Open ticket that already holds a message this mail is replying to."""
    ids = _reference_ids(*header_values)
    if not ids:
        return None
    queue = get_company_queue(company)
    followup = (
        FollowUp.objects.filter(message_id__in=ids, ticket__queue=queue)
        .select_related("ticket")
        .order_by("-date", "-id")
        .first()
    )
    if followup is None or followup.ticket.status not in _OPEN_FOR_REPLIES:
        return None
    return followup.ticket


def add_email_reply(
    ticket: Ticket,
    *,
    body: str,
    sender: str,
    message_id: str,
    attachments: list | None = None,
    when=None,
    sent: bool = False,
) -> FollowUp:
    """Append one message from the customer's original mail thread."""
    stamp = when or timezone.now()
    verb = "Sent by" if sent else "Received from"
    followup = FollowUp.objects.create(
        ticket=ticket,
        date=stamp,
        title=(f"E-Mail {verb} {sender}")[:200],
        comment=body or "",
        public=True,
        user=None,
        message_id=(message_id or "").strip() or None,
    )
    _store_message_files(followup, attachments)
    ticket.modified = timezone.now()
    ticket.save(update_fields=["modified"])
    if sent:
        return followup
    try:
        from business.team_notifications import notify_ticket_reply

        notify_ticket_reply(followup)
    except Exception:
        logger.exception("Reply notification failed ticket_id=%s", ticket.pk)
    return followup


def create_helpdesk_ticket_from_email(
    *,
    company: CompanyProfile,
    subject: str,
    body: str,
    sender: str,
    message_id: str,
    conversation_id: str = "",
    attachments: list | None = None,
    when=None,
) -> Ticket | None:
    from business.priority_rules import match_priority_from_subject
    from business.sla import apply_sla_to_ticket, get_or_create_sla_settings
    from business.helpdesk_bridge import PRIORITY_UI_TO_HD

    mid = (message_id or "").strip()
    if mid and _already_imported(mid):
        return None

    queue = get_company_queue(company)
    now = when or timezone.now()
    cleaned = clean_inbound_email_body(body or "")

    sla_settings = get_or_create_sla_settings(company)
    if sla_settings.email_auto_priority:
        ui_priority, matched = match_priority_from_subject(company, subject or "")
        priority_source = "rule" if matched else "default"
    else:
        ui_priority = "medium"
        priority_source = "default"

    ticket = Ticket(
        title=(subject or "(no subject)")[:200],
        queue=queue,
        created=now,
        modified=now,
        submitter_email=(sender or "")[:254] or None,
        status=Ticket.OPEN_STATUS,
        description=cleaned,
        priority=PRIORITY_UI_TO_HD.get(ui_priority, 3),
    )
    ticket.save()
    meta = ensure_meta(
        ticket,
        created_by=company.user,
        department="general",
        is_internal=False,
    )
    conv = (conversation_id or "").strip()[:512]
    if conv:
        meta.email_conversation_id = conv
        meta.save(update_fields=["email_conversation_id", "updated_at"])
    opening = FollowUp.objects.create(
        ticket=ticket,
        date=now,
        title="E-Mail Received",
        comment=cleaned,
        public=True,
        user=None,
        new_status=Ticket.OPEN_STATUS,
        message_id=mid or None,
    )
    _store_message_files(opening, attachments)
    apply_sla_to_ticket(
        company,
        ticket,
        priority=ui_priority,
        priority_source=priority_source,
    )
    new_ticket_done.send(sender="helpdesk_inbox", ticket=ticket)
    return ticket


def _graph_body_parts(item: dict) -> tuple[str, str, str]:
    """Return (source text, content type, html or "")."""
    body_obj = item.get("body") or {}
    content = (body_obj.get("content") or "").strip()
    content_type = (body_obj.get("contentType") or "").lower()
    if content:
        html = content if content_type == "html" else ""
        return content, content_type or "text", html
    return (item.get("bodyPreview") or "").strip(), "text", ""


def _graph_get_attachments(access_token: str, mailbox: str, graph_id: str) -> list[dict]:
    import requests
    from urllib.parse import quote

    url = (
        "https://graph.microsoft.com/v1.0/users/"
        f"{quote(mailbox)}/messages/{quote(graph_id)}/attachments"
    )
    resp = requests.get(
        url,
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=40,
    )
    if not resp.ok:
        logger.warning(
            "Graph attachments failed for %s: %s", graph_id, (resp.text or "")[:200]
        )
        return []
    return list((resp.json() or {}).get("value") or [])


def _files_for_graph_item(
    item: dict, html: str, *, access_token: str, mailbox: str
) -> list[tuple[str, bytes, str]]:
    if not item.get("hasAttachments"):
        return []
    graph_id = (item.get("id") or "").strip()
    if not graph_id:
        return []
    raw = _graph_get_attachments(access_token, mailbox, graph_id)
    return message_files_from_graph(raw, html)


def _backfill_imported_message(
    company: CompanyProfile, item: dict, *, access_token: str, mailbox: str
) -> None:
    """Remember the Outlook conversation and pull files for a mail already imported."""
    from business.models import SupportTicketMeta

    mid = (item.get("internetMessageId") or "").strip()
    graph_id = (item.get("id") or "").strip()
    if not mid:
        mid = f"graph:{graph_id}"
    followup = (
        FollowUp.objects.filter(message_id=mid)
        .select_related("ticket")
        .order_by("id")
        .first()
    )
    if followup is None:
        return

    conv = (item.get("conversationId") or "").strip()[:512]
    if conv:
        opening = (
            FollowUp.objects.filter(ticket_id=followup.ticket_id)
            .exclude(message_id__isnull=True)
            .exclude(message_id="")
            .order_by("date", "id")
            .first()
        )
        if opening and opening.pk == followup.pk:
            meta = SupportTicketMeta.objects.filter(ticket_id=followup.ticket_id).first()
            if meta and not (meta.email_conversation_id or "").strip():
                meta.email_conversation_id = conv
                meta.save(update_fields=["email_conversation_id", "updated_at"])

    if not item.get("hasAttachments") or followup.followupattachment_set.exists():
        return
    _store_message_files(
        followup,
        _files_for_graph_item(
            item,
            _graph_body_parts(item)[2],
            access_token=access_token,
            mailbox=mailbox,
        ),
    )


def _graph_message_id(item: dict) -> str:
    mid = (item.get("internetMessageId") or "").strip()
    gid = (item.get("id") or "").strip()
    if not mid and gid:
        mid = f"graph:{gid}"
    return mid


def _message_timestamp(item: dict, *, sent: bool = False):
    from datetime import timezone as dt_timezone

    from django.utils.dateparse import parse_datetime

    keys = (
        ("sentDateTime", "receivedDateTime")
        if sent
        else ("receivedDateTime", "sentDateTime")
    )
    for key in keys:
        parsed = parse_datetime(item.get(key) or "")
        if parsed is None:
            continue
        if timezone.is_naive(parsed):
            parsed = timezone.make_aware(parsed, dt_timezone.utc)
        return parsed
    return timezone.now()


def _import_graph_item(
    company: CompanyProfile,
    item: dict,
    *,
    access_token: str,
    mailbox: str,
    sent: bool,
) -> str:
    """
    Store one Graph message on its open ticket.

    Inbox mail with no matching ticket opens a ticket. Sent mail never does.
    Returns ``created``, ``reply``, or ``skipped``.
    """
    mid = _graph_message_id(item)
    if not mid:
        return "skipped"
    if _already_imported(mid):
        if not sent:
            try:
                _backfill_imported_message(
                    company, item, access_token=access_token, mailbox=mailbox
                )
            except Exception:
                logger.exception("Helpdesk attachment backfill failed for %s", mid)
        return "skipped"

    subject = (item.get("subject") or "(no subject)").strip()[:255]
    if _is_our_notification(subject):
        return "skipped"

    from_obj = item.get("from") or {}
    sender = (
        ((from_obj.get("emailAddress") or {}).get("address") or "").strip() or mailbox
    )
    conversation_id = (item.get("conversationId") or "").strip()
    reply_ticket = open_ticket_for_conversation(
        company, conversation_id
    ) or open_ticket_for_references(
        company,
        _graph_header(item, "In-Reply-To"),
        _graph_header(item, "References"),
    )
    if sent and reply_ticket is None:
        return "skipped"

    source, kind, html = _graph_body_parts(item)
    files = _files_for_graph_item(
        item, html, access_token=access_token, mailbox=mailbox
    )
    when = _message_timestamp(item, sent=sent)
    body = plain_main_body(
        source,
        kind,
        empty_fallback="" if reply_ticket or sent else None,
    )
    try:
        if reply_ticket is not None:
            add_email_reply(
                reply_ticket,
                body=body,
                sender=sender,
                message_id=mid,
                attachments=files,
                when=when,
                sent=sent,
            )
            return "reply"
        if sent:
            return "skipped"
        ticket = create_helpdesk_ticket_from_email(
            company=company,
            subject=subject,
            body=plain_main_body(source, kind),
            sender=sender,
            message_id=mid,
            conversation_id=conversation_id,
            attachments=files,
            when=when,
        )
    except IntegrityError:
        return "skipped"
    return "created" if ticket else "skipped"


_GRAPH_MESSAGE_SELECT = (
    "id,subject,from,internetMessageId,bodyPreview,body,"
    "internetMessageHeaders,receivedDateTime,sentDateTime,"
    "conversationId,hasAttachments"
)


def _graph_list_sent_items(
    access_token: str,
    mailbox_email: str,
    *,
    top: int = 50,
    since_days: int = 14,
) -> list[dict]:
    """Recent messages in the support mailbox Sent Items folder."""
    import requests
    from datetime import datetime, timedelta, timezone as dt_timezone
    from urllib.parse import quote

    from business.support_inbox import _graph_error_detail

    url = (
        "https://graph.microsoft.com/v1.0/users/"
        f"{quote(mailbox_email)}/mailFolders/SentItems/messages"
    )
    cutoff = (
        datetime.now(dt_timezone.utc) - timedelta(days=max(1, int(since_days)))
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    headers = {"Authorization": f"Bearer {access_token}"}
    resp = requests.get(
        url,
        headers=headers,
        params={
            "$filter": f"sentDateTime ge {cutoff}",
            "$top": str(top),
            "$select": _GRAPH_MESSAGE_SELECT,
        },
        timeout=30,
    )
    if not resp.ok:
        resp = requests.get(
            url,
            headers=headers,
            params={
                "$top": str(top),
                "$orderby": "sentDateTime desc",
                "$select": _GRAPH_MESSAGE_SELECT,
            },
            timeout=30,
        )
    if not resp.ok:
        raise RuntimeError(_graph_error_detail(resp))
    items = list((resp.json() or {}).get("value") or [])
    items.sort(key=lambda item: item.get("sentDateTime") or item.get("receivedDateTime") or "")
    return items


def _fetch_microsoft_via_graph(company: CompanyProfile) -> tuple[int, list[str]]:
    """
    Microsoft 365 basic IMAP auth is often blocked; use Graph, but write
    django-helpdesk Ticket rows (not legacy SupportTicket).

    Inbox mail opens tickets. Later inbox replies and Sent Items replies on
    that same Outlook conversation are appended to the open ticket.
    """
    created = 0
    replies = 0
    log: list[str] = []
    mailbox = company.support_email
    mark_seen = settings.SUPPORT_IMAP_MARK_SEEN
    access_token = _graph_access_token()
    recent = _graph_list_unread_inbox(
        access_token,
        mailbox,
        select=_GRAPH_MESSAGE_SELECT,
        unread_only=False,
    )
    unread = _graph_list_unread_inbox(
        access_token,
        mailbox,
        select=_GRAPH_MESSAGE_SELECT,
        unread_only=True,
    )
    seen_ids: set[str] = set()
    items: list[dict] = []
    for item in list(unread) + list(recent):
        gid = (item.get("id") or "").strip()
        key = gid or (item.get("internetMessageId") or "").strip()
        if not key or key in seen_ids:
            continue
        seen_ids.add(key)
        items.append(item)

    # Oldest first so the opening mail is stored before its replies.
    items.sort(key=lambda row: row.get("receivedDateTime") or "")

    for item in items:
        outcome = _import_graph_item(
            company,
            item,
            access_token=access_token,
            mailbox=mailbox,
            sent=False,
        )
        if outcome == "created":
            created += 1
        elif outcome == "reply":
            replies += 1
        gid = (item.get("id") or "").strip()
        if mark_seen and gid:
            try:
                _graph_mark_read(access_token, mailbox, gid)
            except Exception:
                pass

    sent_items: list[dict] = []
    try:
        sent_items = _graph_list_sent_items(access_token, mailbox)
    except Exception as exc:
        logger.exception("Sent Items sync failed for company %s", company.pk)
        log.append(f"[{company.company_name}] Sent mail was not imported: {exc}")
    else:
        for item in sent_items:
            outcome = _import_graph_item(
                company,
                item,
                access_token=access_token,
                mailbox=mailbox,
                sent=True,
            )
            if outcome == "reply":
                replies += 1

    log.append(
        f"[{company.company_name}] Graph->helpdesk imported {created} ticket(s) "
        f"and {replies} reply(s) from {len(items)} Inbox and "
        f"{len(sent_items)} Sent message(s)."
    )
    return created, log


def inbox_error_logs(result: dict | None) -> list[str]:
    """Log lines that should be shown to the user as mailbox-sync failures."""
    if not result:
        return []
    errors: list[str] = []
    for line in result.get("logs") or []:
        low = (line or "").lower()
        if any(
            token in low
            for token in (
                "error",
                "failed",
                "needs graph",
                "not set",
                "skipped",
                "app password",
            )
        ):
            errors.append(line)
    return errors


def fetch_helpdesk_mail_for_company(company: CompanyProfile) -> dict:
    """Import new support mails for one company into helpdesk tickets."""
    logs: list[str] = []
    created = 0
    queue = sync_company_queue(company)
    support = (company.support_email or "").strip()
    password = (company.support_email_app_password or "").strip()
    if not support:
        logs.append(
            f"[{company.company_name}] Support email is not set in Company profile."
        )
        return {"created": 0, "logs": logs, "mode": "skipped"}

    host = _detect_imap_host(support)
    if _is_microsoft_host(host):
        if queue.allow_email_submission:
            queue.allow_email_submission = False
            queue.save(update_fields=["allow_email_submission"])
        if not password and not (
            getattr(settings, "MICROSOFT_GRAPH_CLIENT_ID", "")
            and getattr(settings, "MICROSOFT_GRAPH_CLIENT_SECRET", "")
        ):
            logs.append(
                f"[{company.company_name}] M365 mailbox needs Graph app credentials."
            )
            return {"created": 0, "logs": logs, "mode": "graph"}
        try:
            created, company_logs = _fetch_microsoft_via_graph(company)
            logs.extend(company_logs)
        except Exception as exc:
            logs.append(f"[{company.company_name}] Graph sync error: {exc}")
            logger.exception("Graph helpdesk sync failed for company %s", company.pk)
        return {"created": created, "logs": logs, "mode": "graph"}

    from helpdesk.email import process_queue

    # Non-M365: let helpdesk poll this queue via IMAP.
    if not queue.allow_email_submission:
        logs.append(
            f"[{company.company_name}] IMAP skipped: set the support email "
            "app password in Company profile."
        )
        return {"created": 0, "logs": logs, "mode": "imap"}
    try:
        before = Ticket.objects.filter(queue=queue).count()
        process_queue(queue, logger=logger)
        created = max(0, Ticket.objects.filter(queue=queue).count() - before)
        if created:
            logs.append(
                f"[{company.company_name}] IMAP imported {created} new ticket(s)."
            )
    except Exception as exc:
        logs.append(f"[{company.company_name}] IMAP sync error: {exc}")
        logger.exception("IMAP helpdesk sync failed for company %s", company.pk)
    return {"created": created, "logs": logs, "mode": "imap"}


def maybe_fetch_on_dashboard(company: CompanyProfile, *, min_interval_seconds: int = 20) -> dict | None:
    """
    Throttled inbox poll shared by the ticket page and the background fetch.
    Returns fetch result dict, or None if this company was checked recently.
    """
    from django.core.cache import cache

    key = f"helpdesk_inbox_poll:{company.pk}"
    if cache.get(key):
        return None
    cache.set(key, 1, timeout=max(5, int(min_interval_seconds)))
    try:
        return fetch_helpdesk_mail_for_company(company)
    except Exception:
        logger.exception("Dashboard helpdesk poll failed for company %s", company.pk)
        return {"created": 0, "logs": ["poll failed"], "mode": "error"}


def fetch_helpdesk_mail_for_all_companies() -> dict:
    """
    Sync queues, then:
    - Microsoft 365 mailboxes → Graph → helpdesk tickets
    - Other providers → django-helpdesk IMAP (process_email)
    """
    logs: list[str] = []
    created_total = 0
    graph_created = 0
    ms_companies = 0
    other_imap = False

    for company in CompanyProfile.objects.select_related("user").order_by("pk"):
        support = (company.support_email or "").strip()
        if not support:
            continue
        # Same cache key as the open ticket page, so the two polls do not
        # hit the mailbox twice inside the throttle window.
        result = maybe_fetch_on_dashboard(company)
        if not result:
            continue
        logs.extend(result.get("logs") or [])
        created = int(result.get("created") or 0)
        created_total += created
        if result.get("mode") == "graph":
            ms_companies += 1
            graph_created += created
        elif result.get("mode") == "imap":
            other_imap = True

    return {
        "created": created_total,
        "graph_created": graph_created,
        "microsoft_companies": ms_companies,
        "imap_polled": other_imap,
        "logs": logs[-30:],
    }
