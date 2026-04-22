"""
Poll each company support mailbox over IMAP and create SupportTicket rows.

Schedule (examples):
  Windows Task Scheduler / cron (Linux):
    */5 * * * * cd /path/to/project && venv/bin/python manage.py fetch_support_inbox

Requires per company (Company profile in app):
  - Support email = IMAP login address
  - Support email app password = IMAP password (e.g. Gmail app password)

Server-wide IMAP host/port: SUPPORT_IMAP_HOST, SUPPORT_IMAP_PORT in .env
(Gmail: imap.gmail.com:993; Microsoft 365: outlook.office365.com:993)
"""

from django.core.management.base import BaseCommand

from accounts.models import CompanyProfile

from business.support_inbox import fetch_tickets_for_company


class Command(BaseCommand):
    help = "Read UNSEEN messages from company support IMAP mailboxes and create tickets."

    def add_arguments(self, parser):
        parser.add_argument(
            "--company-id",
            type=int,
            default=None,
            help="Only process this CompanyProfile primary key.",
        )

    def handle(self, *args, **options):
        qs = CompanyProfile.objects.select_related("user").order_by("pk")
        cid = options.get("company_id")
        if cid is not None:
            qs = qs.filter(pk=cid)

        total = 0
        for company in qs:
            n, logs = fetch_tickets_for_company(company)
            total += n
            for line in logs:
                self.stdout.write(line)

        self.stdout.write(self.style.SUCCESS(f"Finished. {total} new ticket(s) created."))
