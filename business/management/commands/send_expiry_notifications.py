"""Send subscription expiry reminder emails and team digests."""

import logging

from django.core.management.base import BaseCommand, CommandError

from accounts.models import CompanyProfile

from business.expiry_notifications import send_expiry_notifications_for_all_companies

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        "Send enabled client expiry reminders and team digests for all companies. "
        "Normally scheduled every minute via Celery beat."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--company-id",
            type=int,
            default=None,
            help="Only process this CompanyProfile primary key.",
        )

    def handle(self, *args, **options):
        company_id = options.get("company_id")
        try:
            if company_id is not None:
                company = CompanyProfile.objects.select_related("user").filter(
                    pk=company_id
                ).first()
                if company is None:
                    self.stderr.write(
                        self.style.ERROR(f"Company {company_id} not found.")
                    )
                    return
                from business.expiry_notifications import (
                    send_expiry_notifications_for_company,
                )

                result = send_expiry_notifications_for_company(company)
            else:
                result = send_expiry_notifications_for_all_companies()
        except Exception as exc:
            logger.exception(
                "send_expiry_notifications command failed company_id=%s", company_id
            )
            raise CommandError(f"Expiry notification send failed: {exc}") from exc

        for line in result.get("logs", []):
            self.stdout.write(line)
        self.stdout.write(
            self.style.SUCCESS(
                "Client emails sent: {client}; team digests sent: {team}.".format(
                    client=result.get("client_emails_sent", 0),
                    team=result.get("team_digests_sent", 0),
                )
            )
        )
