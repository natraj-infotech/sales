"""Helpdesk tickets created from WhatsApp support intake."""

from django.contrib.auth import get_user_model
from django.test import TestCase

from accounts.models import CompanyProfile
from business.helpdesk_bridge import company_queue_slug, tickets_for_company, wrap_ticket
from helpdesk.models import Queue, Ticket
from business.helpdesk_whatsapp import (
    create_helpdesk_ticket_from_whatsapp,
    format_whatsapp_phone,
)
from business.models import SupportTicketMeta

User = get_user_model()


class HelpdeskWhatsAppTests(TestCase):
    def setUp(self):
        user = User.objects.create_user(email="wa-ticket@example.com", password="x")
        self.company = CompanyProfile.objects.create(user=user, company_name="WA Co")

    def test_format_phone(self):
        self.assertEqual(format_whatsapp_phone("919876543210"), "+919876543210")

    def test_create_ticket_shows_phone_on_dashboard_view(self):
        ticket = create_helpdesk_ticket_from_whatsapp(
            company=self.company,
            firm_name="Beta Corp",
            description="App not loading",
            phone_wa_id="919911122233",
        )
        wrapped = wrap_ticket(ticket)
        self.assertEqual(wrapped.contact_display, "+919911122233")
        self.assertEqual(wrapped.customer_firm_name, "Beta Corp")
        self.assertEqual(wrapped.source_channel, SupportTicketMeta.SourceChannel.WHATSAPP)
        self.assertEqual(Ticket.objects.filter(pk=ticket.pk).count(), 1)

    def test_tickets_visible_after_company_rename_queue_consolidation(self):
        ticket = create_helpdesk_ticket_from_whatsapp(
            company=self.company,
            firm_name="Gamma",
            description="Issue",
            phone_wa_id="919900000001",
        )
        old_queue = ticket.queue
        self.company.company_name = "Renamed Company Ltd"
        self.company.save(update_fields=["company_name"])
        new_slug = company_queue_slug(self.company)
        self.assertNotEqual(old_queue.slug, new_slug)

        stale = Queue.objects.create(
            slug=old_queue.slug + "-stale-test",
            title="Stale",
        )
        Ticket.objects.filter(pk=ticket.pk).update(queue=stale)

        listed = list(tickets_for_company(self.company))
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0].pk, ticket.pk)
        ticket.refresh_from_db()
        self.assertEqual(ticket.queue.slug, new_slug)
