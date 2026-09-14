import logging

from celery import shared_task

from .caselock_inbox import fetch_all_caselock_inboxes
from .expiry_notifications import send_expiry_notifications_for_all_companies
from .helpdesk_inbox import fetch_helpdesk_mail_for_all_companies
from .renewal_read_tracking import poll_outlook_read_receipts_for_all_companies

logger = logging.getLogger(__name__)


def _run_task(name, func):
    try:
        return func()
    except Exception:
        logger.exception("Celery task failed: %s", name)
        raise


@shared_task
def fetch_helpdesk_email_task():
    """Poll support mailboxes and create django-helpdesk tickets."""
    return _run_task(
        "fetch_helpdesk_email_task", fetch_helpdesk_mail_for_all_companies
    )


@shared_task
def fetch_support_inbox_task():
    """Deprecated alias — kept for old Celery beat entries."""
    return fetch_helpdesk_email_task()


@shared_task
def fetch_caselock_inbox_task():
    """Poll company Case lock mailboxes and email form links."""
    return _run_task("fetch_caselock_inbox_task", fetch_all_caselock_inboxes)


@shared_task
def send_expiry_notifications_task():
    """Send client expiry reminders and team digests for enabled companies."""
    return _run_task(
        "send_expiry_notifications_task",
        send_expiry_notifications_for_all_companies,
    )


@shared_task
def poll_renewal_read_receipts_task():
    """Check Outlook inboxes for read receipts on renewal reminder emails."""
    return _run_task(
        "poll_renewal_read_receipts_task",
        poll_outlook_read_receipts_for_all_companies,
    )
