import logging

from django.core.management.base import BaseCommand, CommandError

from business.helpdesk_inbox import fetch_helpdesk_mail_for_all_companies

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        "Import support mailbox messages into django-helpdesk tickets. "
        "Uses Microsoft Graph for M365 mailboxes; IMAP (helpdesk) for others."
    )

    def handle(self, *args, **options):
        try:
            result = fetch_helpdesk_mail_for_all_companies()
        except Exception as exc:
            logger.exception("fetch_helpdesk_email command failed")
            raise CommandError(f"Helpdesk email fetch failed: {exc}") from exc
        for line in result.get("logs") or []:
            self.stdout.write(line)
        self.stdout.write(
            self.style.SUCCESS(
                "Done. graph_created=%(graph_created)s microsoft_companies=%(microsoft_companies)s "
                "imap_polled=%(imap_polled)s"
                % result
            )
        )
