"""Helpdesk tickets created from WhatsApp support intake."""

from django.contrib.auth import get_user_model
from django.test import TestCase

from accounts.models import CompanyProfile
from business.helpdesk_bridge import wrap_ticket
from business.helpdesk_whatsapp import (
    create_helpdesk_ticket_from_whatsapp,
    format_whatsapp_phone,
)
from business.models import SupportTicketMeta
from helpdesk.models import Ticket

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
