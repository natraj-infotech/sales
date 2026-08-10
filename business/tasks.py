from celery import shared_task

from accounts.models import CompanyProfile

from .caselock_inbox import fetch_all_caselock_inboxes
from .expiry_notifications import send_expiry_notifications_for_all_companies
from .renewal_read_tracking import poll_outlook_read_receipts_for_all_companies
from .support_inbox import fetch_tickets_for_company


@shared_task
def fetch_support_inbox_task():
    """
    Poll all company support inboxes and create tickets from unread mail.
    Returns a compact summary for logs/monitoring.
    """
    created_total = 0
    companies_processed = 0
    logs = []
    for company in CompanyProfile.objects.select_related("user").order_by("pk"):
        created, company_logs = fetch_tickets_for_company(company)
        companies_processed += 1
        created_total += created
        logs.extend(company_logs)
    return {
        "companies": companies_processed,
        "created": created_total,
        "logs": logs[-20:],
    }


@shared_task
def fetch_caselock_inbox_task():
    """Poll all configured Case lock mailboxes and email form links."""
    return fetch_all_caselock_inboxes()


@shared_task
def send_expiry_notifications_task():
    """Send client expiry reminders and team digests for enabled companies."""
    return send_expiry_notifications_for_all_companies()


@shared_task
def poll_renewal_read_receipts_task():
    """Check Outlook inboxes for read receipts on renewal reminder emails."""
    return poll_outlook_read_receipts_for_all_companies()
