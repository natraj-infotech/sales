"""Inbound support mail → django-helpdesk tickets (Graph for M365, IMAP via helpdesk)."""

from __future__ import annotations

import logging

from django.conf import settings
from django.db import IntegrityError
from django.utils import timezone
from django.utils.html import strip_tags
from helpdesk.models import FollowUp, Ticket
from helpdesk.signals import new_ticket_done

from accounts.models import CompanyProfile
from business.email_body_clean import clean_inbound_email_body
from business.helpdesk_bridge import (
    ensure_meta,
    get_company_queue,
    sync_company_queue,
)
from business.support_inbox import (
    _detect_imap_host,
    _graph_access_token,
    _graph_mark_read,
    _is_microsoft_host,
)

logger = logging.getLogger(__name__)


def _already_imported(message_id: str) -> bool:
    mid = (message_id or "").strip()
    if not mid:
        return False
    return FollowUp.objects.filter(message_id=mid).exists()


def create_helpdesk_ticket_from_email(
    *,
    company: CompanyProfile,
    subject: str,
    body: str,
    sender: str,
    message_id: str,
) -> Ticket | None:
    from business.priority_rules import match_priority_from_subject
    from business.sla import apply_sla_to_ticket, get_or_create_sla_settings
    from business.helpdesk_bridge import PRIORITY_UI_TO_HD

    mid = (message_id or "").strip()
    if mid and _already_imported(mid):
        return None

    queue = get_company_queue(company)
    now = timezone.now()
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
    ensure_meta(
        ticket,
        created_by=company.user,
        department="general",
        is_internal=False,
    )
    FollowUp.objects.create(
        ticket=ticket,
        date=now,
        title="E-Mail Received",
        comment=cleaned,
        public=True,
        user=None,
        new_status=Ticket.OPEN_STATUS,
        message_id=mid or None,
    )
    apply_sla_to_ticket(
        company,
        ticket,
        priority=ui_priority,
        priority_source=priority_source,
    )
    new_ticket_done.send(sender="helpdesk_inbox", ticket=ticket)
    return ticket


def _fetch_microsoft_via_graph(company: CompanyProfile) -> tuple[int, list[str]]:
    """
    Microsoft 365 basic IMAP auth is often blocked; use Graph, but write
    django-helpdesk Ticket rows (not legacy SupportTicket).
    """
    from urllib.parse import quote

    import requests

    created = 0
    log: list[str] = []
    mailbox = company.support_email
    mark_seen = settings.SUPPORT_IMAP_MARK_SEEN
    access_token = _graph_access_token()

    url = (
        "https://graph.microsoft.com/v1.0/users/"
        f"{quote(mailbox)}/mailFolders/Inbox/messages"
    )
    params = {
        "$filter": "isRead eq false",
        "$top": "25",
        "$select": "id,subject,from,internetMessageId,bodyPreview,body",
        "$orderby": "receivedDateTime asc",
    }
    resp = requests.get(
        url,
        headers={"Authorization": f"Bearer {access_token}"},
        params=params,
        timeout=30,
    )
    resp.raise_for_status()
    items = (resp.json().get("value") or [])

    for item in items:
        mid = (item.get("internetMessageId") or "").strip()
        gid = (item.get("id") or "").strip()
        if not mid:
            mid = f"graph:{gid}"
        if not mid:
            continue
        if _already_imported(mid):
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
            or mailbox
        )
        body = ""
        body_obj = item.get("body") or {}
        content = (body_obj.get("content") or "").strip()
        if content:
            if (body_obj.get("contentType") or "").lower() == "html":
                body = strip_tags(content)
            else:
                body = content
        if not body:
            body = (item.get("bodyPreview") or "").strip()
        body = clean_inbound_email_body(body)

        try:
            ticket = create_helpdesk_ticket_from_email(
                company=company,
                subject=subject,
                body=body,
                sender=sender,
                message_id=mid,
            )
            if ticket:
                created += 1
        except IntegrityError:
            pass

        if mark_seen and gid:
            try:
                _graph_mark_read(access_token, mailbox, gid)
            except Exception:
                pass

    log.append(
        f"[{company.company_name}] Graph->helpdesk imported {created} ticket(s) "
        f"from {len(items)} unread message(s)."
    )
    return created, log


def fetch_helpdesk_mail_for_company(company: CompanyProfile) -> dict:
    """Import new support mails for one company into helpdesk tickets."""
    logs: list[str] = []
    created = 0
    queue = sync_company_queue(company)
    support = (company.support_email or "").strip()
    password = (company.support_email_app_password or "").strip()
    if not support:
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
        return {"created": 0, "logs": logs, "mode": "imap"}
    try:
        process_queue(queue, logger=logger)
    except Exception as exc:
        logs.append(f"[{company.company_name}] IMAP sync error: {exc}")
        logger.exception("IMAP helpdesk sync failed for company %s", company.pk)
    return {"created": created, "logs": logs, "mode": "imap"}


def maybe_fetch_on_dashboard(company: CompanyProfile, *, min_interval_seconds: int = 20) -> dict | None:
    """
    Throttled inbox poll for page refresh (runserver-only workflow).
    Returns fetch result dict, or None if skipped due to throttle.
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
    graph_created = 0
    ms_companies = 0
    other_imap = False

    for company in CompanyProfile.objects.select_related("user").order_by("pk"):
        support = (company.support_email or "").strip()
        if not support:
            continue
        host = _detect_imap_host(support)
        result = fetch_helpdesk_mail_for_company(company)
        logs.extend(result.get("logs") or [])
        if result.get("mode") == "graph":
            ms_companies += 1
            graph_created += int(result.get("created") or 0)
        elif result.get("mode") == "imap":
            other_imap = True

    return {
        "graph_created": graph_created,
        "microsoft_companies": ms_companies,
        "imap_polled": other_imap,
        "logs": logs[-30:],
    }
