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
def send_client_bulk_emails_task(
    company_id: int,
    client_ids: list[int],
    subject_template: str,
    body_template: str,
    attachment_payload: list[dict[str, str]] | None = None,
):
    """Send bulk client email outside the HTTP request (avoids Gunicorn worker timeout)."""

    def _run():
        from accounts.models import CompanyProfile

        from business.client_bulk_email import (
            attachments_from_task,
            send_client_bulk_emails,
        )

        company = CompanyProfile.objects.filter(pk=company_id).first()
        if company is None:
            logger.error(
                "send_client_bulk_emails_task: company_id=%s not found",
                company_id,
            )
            return {"sent": 0, "skipped": 0, "failures": 1}

        clients = list(
            company.clients.filter(pk__in=client_ids).order_by("name").distinct()
        )
        if not clients:
            logger.warning(
                "send_client_bulk_emails_task: no clients company_id=%s ids=%s",
                company_id,
                client_ids,
            )
            return {"sent": 0, "skipped": 0, "failures": 0}

        attachments = attachments_from_task(attachment_payload)
        sent, skipped, failures = send_client_bulk_emails(
            company,
            clients,
            subject_template,
            body_template,
            attachments=attachments or None,
        )
        for line in skipped:
            logger.warning(
                "client bulk email skipped company_id=%s: %s", company_id, line
            )
        for line in failures:
            logger.error(
                "client bulk email failed company_id=%s: %s", company_id, line
            )
        logger.info(
            "client bulk email task finished company_id=%s sent=%s skipped=%s failures=%s",
            company_id,
            sent,
            len(skipped),
            len(failures),
        )
        return {
            "sent": sent,
            "skipped": len(skipped),
            "failures": len(failures),
        }

    return _run_task("send_client_bulk_emails_task", _run)


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
