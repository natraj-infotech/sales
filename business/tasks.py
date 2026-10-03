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


@shared_task
def scan_team_notifications_task():
    """Bell items for overdue tickets and subscriptions nearing expiry."""
    from .team_notifications import scan_scheduled_notifications

    return _run_task("scan_team_notifications_task", scan_scheduled_notifications)


@shared_task
def create_backup_task():
    """Create combined PostgreSQL + media backup (see business.backup)."""
    from django.conf import settings

    from .backup import BackupError, create_backup

    if not getattr(settings, "BACKUP_ENABLED", True):
        logger.info("create_backup_task skipped (BACKUP_ENABLED=false)")
        return {"skipped": True}

    def _run():
        result = create_backup(prune=True)
        return result.to_dict()

    try:
        return _run_task("create_backup_task", _run)
    except BackupError:
        raise
