"""In-app team notifications for the sidebar bell.

One row is stored per recipient. Sales events go to people who can open the
sales module, support events to people who can open tickets, and case lock
and email failure events to the company owner (only they can open those pages).
"""

from __future__ import annotations

import logging
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import IntegrityError
from django.urls import reverse
from django.utils import timezone

from accounts.models import CompanyProfile
from accounts.permissions import (
    user_has_sales_access,
    user_has_support_ticket_access,
    user_is_owner,
)
from business.models import (
    ClientSubscription,
    SupportTicketMeta,
    TeamNotification,
)

logger = logging.getLogger(__name__)

RETENTION_DAYS = 7
SCAN_LOCK_SECONDS = 50

SALES_KINDS = {
    TeamNotification.Kind.QUOTATION_PRICE,
    TeamNotification.Kind.FINAL_QUOTATION,
    TeamNotification.Kind.NEW_LEAD,
    TeamNotification.Kind.SUBSCRIPTION_EXPIRY,
    TeamNotification.Kind.RENEWAL_CONFIRMED,
}
SUPPORT_KINDS = {
    TeamNotification.Kind.SUPPORT_TICKET,
    TeamNotification.Kind.TICKET_OVERDUE,
    TeamNotification.Kind.TICKET_ASSIGNED,
    TeamNotification.Kind.TICKET_REPLY,
}
OWNER_KINDS = {
    TeamNotification.Kind.CASELOCK_SUBMIT,
    TeamNotification.Kind.EMAIL_FAILED,
}


def visible_since():
    return timezone.now() - timedelta(days=RETENTION_DAYS)


def safe_target_path(url: str) -> str:
    path = (url or "").strip()
    if not path.startswith("/") or path.startswith("//") or "\\" in path:
        return ""
    return path[:500]


def company_for_ticket(ticket):
    queue = getattr(ticket, "queue", None)
    slug = (getattr(queue, "slug", "") or "").strip()
    if not slug.startswith("c"):
        return None
    head = slug[1:].split("-", 1)[0]
    if not head.isdigit():
        return None
    return CompanyProfile.objects.filter(pk=int(head)).first()


def _user_allowed(user, company: CompanyProfile, kind: str) -> bool:
    if kind in SUPPORT_KINDS:
        return user_has_support_ticket_access(user, company)
    if kind in OWNER_KINDS:
        return user_is_owner(user, company)
    if kind in SALES_KINDS:
        return user_has_sales_access(user, company)
    return False


def _recipients(company: CompanyProfile, kind: str, users=None):
    company_user_ids = company.company_user_ids()
    if users is None:
        users = get_user_model().objects.filter(id__in=company_user_ids)
    return [
        user
        for user in users
        if user is not None
        and user.pk in company_user_ids
        and _user_allowed(user, company, kind)
    ]


def notify_team(
    *,
    company,
    kind: str,
    title: str,
    body: str,
    target_url: str,
    dedupe_key: str,
    users=None,
) -> int:
    """Store one bell item per allowed recipient. `users` narrows the audience."""
    if company is None:
        return 0
    path = safe_target_path(target_url)
    key = (dedupe_key or "").strip()[:190]
    if not path or not key:
        return 0
    title = (title or "Notification")[:255]
    body = (body or "")[:500]
    created = 0
    for user in _recipients(company, kind, users):
        try:
            _, was_created = TeamNotification.objects.get_or_create(
                recipient=user,
                dedupe_key=key,
                defaults={
                    "company": company,
                    "kind": kind,
                    "title": title,
                    "body": body,
                    "target_url": path,
                },
            )
        except IntegrityError:
            continue
        if was_created:
            created += 1
    return created


def _ticket_ref(ticket) -> str:
    return f"TK-{ticket.pk:05d}"


def notify_new_ticket(ticket) -> int:
    if ticket is None or not getattr(ticket, "pk", None):
        return 0
    meta = SupportTicketMeta.objects.filter(ticket=ticket).first()
    if meta is not None and meta.is_oem:
        return 0
    company = company_for_ticket(ticket)
    if company is None:
        return 0
    sender = (getattr(ticket, "submitter_email", None) or "").strip()
    if meta is not None and (meta.customer_phone or "").strip():
        body = f"WhatsApp {meta.customer_phone.strip()}"
        if (meta.customer_firm_name or "").strip():
            body += f" ({meta.customer_firm_name.strip()})"
    elif sender:
        body = f"From {sender}"
    elif meta is not None and meta.created_by_id:
        body = f"Added by {meta.created_by.email}"
    else:
        body = "New support ticket"
    subject = (getattr(ticket, "title", None) or "Support ticket").strip()
    return notify_team(
        company=company,
        kind=TeamNotification.Kind.SUPPORT_TICKET,
        title=f"New ticket {_ticket_ref(ticket)} — {subject}"[:255],
        body=body,
        target_url=reverse("support_ticket_detail", args=[ticket.pk]),
        dedupe_key=f"support_ticket:{ticket.pk}",
    )


def notify_quotation_submitted(submission) -> int:
    if submission is None or not submission.is_submitted:
        return 0
    vendor_name = submission.vendor.name if submission.vendor_id else "Vendor"
    deal_no = submission.deal_no
    final = bool(submission.is_final_quotation)
    kind = (
        TeamNotification.Kind.FINAL_QUOTATION
        if final
        else TeamNotification.Kind.QUOTATION_PRICE
    )
    if final:
        title = f"{vendor_name} sent final quotation for {deal_no}"
    else:
        title = f"{vendor_name} submitted prices for {deal_no}"
    url = reverse("procurement_detail", args=[deal_no])
    if submission.vendor_id:
        url = f"{url}?vendor={submission.vendor_id}"
    return notify_team(
        company=submission.company,
        kind=kind,
        title=title,
        body=submission.deal_round.display_name if submission.deal_round_id else "",
        target_url=url,
        dedupe_key=f"quotation:{submission.pk}",
    )


def notify_caselock_submitted(submission) -> int:
    invite = submission.invite
    company = invite.company
    if company is None and invite.inbox_id:
        company = invite.inbox.company
    if company is None:
        return 0
    client = (submission.client_name or "").strip() or "Customer"
    product = (submission.product_name or "").strip()
    body = product
    if submission.product_quantity:
        body = f"{product} · qty {submission.product_quantity}".strip(" ·")
    return notify_team(
        company=company,
        kind=TeamNotification.Kind.CASELOCK_SUBMIT,
        title=f"Case lock form submitted by {client}",
        body=body,
        target_url=(
            f"{reverse('caselock_inbox_list')}?tab=dashboard&submission={submission.pk}"
        ),
        dedupe_key=f"caselock_submit:{submission.pk}",
    )


def notify_new_lead(lead) -> int:
    if lead is None or lead.company_id is None:
        return 0
    who = (lead.contact_name or "").strip() or "New contact"
    extra = (lead.company_name or lead.email or lead.phone or "").strip()
    if lead.source:
        source = lead.get_source_display()
        extra = f"{source}" + (f" · {extra}" if extra else "")
    return notify_team(
        company=lead.company,
        kind=TeamNotification.Kind.NEW_LEAD,
        title=f"New lead — {who}",
        body=extra,
        target_url=reverse("lead_detail", args=[lead.pk]),
        dedupe_key=f"lead:{lead.pk}",
    )


def notify_renewal_confirmed(invite, lines=None, new_products=None) -> int:
    if invite is None or not invite.submitted_at:
        return 0
    if lines is None:
        lines = list(invite.lines.order_by("id"))
    if new_products is None:
        new_products = list(invite.new_products.order_by("id"))
    client = (lines[0].company_name if lines else "") or invite.client_email or "Client"
    confirmed = sum(1 for line in lines if (line.renewal_count or 0) > 0)
    parts = [f"{confirmed} of {len(lines)} subscription(s) to renew"]
    if new_products:
        parts.append(f"{len(new_products)} new product(s)")
    if invite.submitted_by_email:
        parts.append(f"by {invite.submitted_by_email}")
    return notify_team(
        company=invite.company,
        kind=TeamNotification.Kind.RENEWAL_CONFIRMED,
        title=f"{client} confirmed renewal quantities",
        body=" · ".join(parts),
        target_url=reverse("client_renewal_detail", args=[invite.pk]),
        dedupe_key=f"renewal_confirmed:{invite.pk}",
    )


def notify_ticket_assigned(ticket, assignee, assigned_by=None) -> int:
    if ticket is None or assignee is None:
        return 0
    if assigned_by is not None and assigned_by.pk == assignee.pk:
        return 0
    company = company_for_ticket(ticket)
    if company is None:
        return 0
    by = (getattr(assigned_by, "email", "") or "").strip()
    stamp = int(timezone.now().timestamp())
    return notify_team(
        company=company,
        kind=TeamNotification.Kind.TICKET_ASSIGNED,
        title=f"{_ticket_ref(ticket)} assigned to you — {ticket.title}"[:255],
        body=f"Assigned by {by}" if by else "",
        target_url=reverse("support_ticket_detail", args=[ticket.pk]),
        dedupe_key=f"ticket_assigned:{ticket.pk}:{assignee.pk}:{stamp}",
        users=[assignee],
    )


def notify_ticket_reply(followup) -> int:
    """Customer emailed on an existing ticket. Goes to the assignee, else the support team."""
    from helpdesk.models import Ticket

    ticket = getattr(followup, "ticket", None)
    if ticket is None or not followup.pk:
        return 0
    company = company_for_ticket(ticket)
    if company is None:
        return 0
    if followup.new_status == Ticket.REOPENED_STATUS:
        title = f"{_ticket_ref(ticket)} reopened by customer reply — {ticket.title}"
    else:
        title = f"New reply on {_ticket_ref(ticket)} — {ticket.title}"
    snippet = " ".join((followup.comment or "").split())[:200]
    return notify_team(
        company=company,
        kind=TeamNotification.Kind.TICKET_REPLY,
        title=title[:255],
        body=snippet,
        target_url=reverse("support_ticket_detail", args=[ticket.pk]),
        dedupe_key=f"ticket_reply:{followup.pk}",
        users=[ticket.assigned_to] if ticket.assigned_to_id else None,
    )


def notify_email_failed(company, *, area: str, title: str, body: str = "") -> int:
    """Tell the owner outgoing mail is failing. At most one item per area per day."""
    if company is None:
        return 0
    try:
        return notify_team(
            company=company,
            kind=TeamNotification.Kind.EMAIL_FAILED,
            title=title,
            body=body,
            target_url=reverse("company_setup"),
            dedupe_key=f"email_failed:{company.pk}:{area}:{timezone.localdate().isoformat()}",
        )
    except Exception:
        logger.exception("Email failure notification failed company_id=%s", company.pk)
        return 0


def purge_old_notifications() -> int:
    deleted, _ = TeamNotification.objects.filter(created_at__lt=visible_since()).delete()
    return deleted


def scan_overdue_tickets() -> int:
    from helpdesk.models import Ticket

    now = timezone.now()
    open_statuses = (Ticket.OPEN_STATUS, Ticket.REOPENED_STATUS)
    metas = (
        SupportTicketMeta.objects.filter(
            is_oem=False,
            ticket__status__in=open_statuses,
        )
        .filter(resolve_due_at__lt=now)
        .filter(completed_at__isnull=True)
        .select_related("ticket", "ticket__queue")
    )
    response_metas = (
        SupportTicketMeta.objects.filter(
            is_oem=False,
            ticket__status__in=open_statuses,
            first_response_due_at__lt=now,
            first_responded_at__isnull=True,
        )
        .select_related("ticket", "ticket__queue")
    )
    seen = set()
    created = 0
    for meta in list(metas) + list(response_metas):
        ticket = meta.ticket
        if ticket.pk in seen:
            continue
        seen.add(ticket.pk)
        company = company_for_ticket(ticket)
        if company is None:
            continue
        reason = "Resolve time has passed"
        if (
            meta.first_response_due_at
            and meta.first_responded_at is None
            and meta.first_response_due_at < now
            and (meta.resolve_due_at is None or meta.resolve_due_at >= now or meta.completed_at)
        ):
            reason = "First response is overdue"
        created += notify_team(
            company=company,
            kind=TeamNotification.Kind.TICKET_OVERDUE,
            title=f"{_ticket_ref(ticket)} is overdue — {ticket.title}"[:255],
            body=reason,
            target_url=reverse("support_ticket_detail", args=[ticket.pk]),
            dedupe_key=f"ticket_overdue:{ticket.pk}",
        )
    return created


def scan_subscription_expiry() -> int:
    from business.expiry_notifications import parse_notify_days

    today = timezone.localdate()
    created = 0
    companies = CompanyProfile.objects.all().only("id", "expiry_notify_at_days", "user_id")
    for company in companies:
        thresholds = parse_notify_days(company.expiry_notify_at_days)
        if not thresholds:
            continue
        window = max(thresholds)
        subs = (
            ClientSubscription.objects.filter(
                company=company,
                status=ClientSubscription.Status.ACTIVE,
                end_date__isnull=False,
                end_date__gte=today,
                end_date__lte=today + timedelta(days=window),
            )
            .select_related("client", "product")
        )
        for sub in subs:
            days_left = (sub.end_date - today).days
            bucket = min((day for day in thresholds if day >= days_left), default=None)
            if bucket is None:
                continue
            if days_left == 0:
                when = "expires today"
            elif days_left == 1:
                when = "expires tomorrow"
            else:
                when = f"expires in {days_left} days"
            client_name = sub.client.name if sub.client_id else "Client"
            product_name = sub.product.name if sub.product_id else "Subscription"
            url = (
                f"{reverse('subscription_list')}"
                f"?client={sub.client_id}&expiring_soon={bucket}&focus={sub.pk}"
            )
            created += notify_team(
                company=company,
                kind=TeamNotification.Kind.SUBSCRIPTION_EXPIRY,
                title=f"{client_name} — {product_name} {when}",
                body=f"End date {sub.end_date.strftime('%d/%m/%Y')}",
                target_url=url,
                dedupe_key=f"subscription_expiry:{sub.pk}:{bucket}",
            )
    return created


def scan_scheduled_notifications() -> dict:
    """Overdue tickets, subscription expiry, and 7-day cleanup. Safe to call often."""
    if not cache.add("team_notification_scan_lock", 1, timeout=SCAN_LOCK_SECONDS):
        return {"skipped": True}
    try:
        purged = purge_old_notifications()
        overdue = scan_overdue_tickets()
        expiry = scan_subscription_expiry()
        return {"purged": purged, "overdue": overdue, "expiry": expiry}
    except Exception:
        logger.exception("Team notification scan failed")
        raise
