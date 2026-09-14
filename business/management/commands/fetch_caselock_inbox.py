"""Poll Case lock mailboxes (alternative to Celery beat)."""

import logging

from django.core.management.base import BaseCommand, CommandError

from business.caselock_inbox import fetch_all_caselock_inboxes

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Read unread mail from Case lock mailboxes and email form links to senders."

    def handle(self, *args, **options):
        try:
            result = fetch_all_caselock_inboxes()
        except Exception as exc:
            logger.exception("fetch_caselock_inbox command failed")
            raise CommandError(f"Case lock inbox fetch failed: {exc}") from exc
        for line in result.get("logs") or []:
            self.stdout.write(line)
        self.stdout.write(
            self.style.SUCCESS(
                f"Finished. {result.get('created', 0)} new invite(s) processed."
            )
        )
