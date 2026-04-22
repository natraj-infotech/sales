"""Poll Case lock mailboxes (alternative to Celery beat)."""

from django.core.management.base import BaseCommand

from business.caselock_inbox import fetch_all_caselock_inboxes


class Command(BaseCommand):
    help = "Read unread mail from Case lock mailboxes and email form links to senders."

    def handle(self, *args, **options):
        result = fetch_all_caselock_inboxes()
        for line in result.get("logs") or []:
            self.stdout.write(line)
        self.stdout.write(
            self.style.SUCCESS(
                f"Finished. {result.get('created', 0)} new invite(s) processed."
            )
        )
