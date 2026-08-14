from django.core.management.base import BaseCommand

from business.helpdesk_inbox import fetch_helpdesk_mail_for_all_companies


class Command(BaseCommand):
    help = (
        "Import support mailbox messages into django-helpdesk tickets. "
        "Uses Microsoft Graph for M365 mailboxes; IMAP (helpdesk) for others."
    )

    def handle(self, *args, **options):
        result = fetch_helpdesk_mail_for_all_companies()
        for line in result.get("logs") or []:
            self.stdout.write(line)
        self.stdout.write(
            self.style.SUCCESS(
                "Done. graph_created=%(graph_created)s microsoft_companies=%(microsoft_companies)s "
                "imap_polled=%(imap_polled)s"
                % result
            )
        )
