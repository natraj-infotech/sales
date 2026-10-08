import shutil
import tempfile
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from accounts.models import CompanyProfile
from business.helpdesk_bridge import add_comment
from business.helpdesk_inbox import create_helpdesk_ticket_from_email
from business.ticket_summary_mail import build_conversation_summary

_MEDIA_ROOT = tempfile.mkdtemp(prefix="ticket-summary-")


@override_settings(MEDIA_ROOT=_MEDIA_ROOT)
@patch("business.team_notifications.notify_new_ticket")
@patch("business.helpdesk_assign_mail.queue_assignment_emails")
@patch("business.helpdesk_assign_mail.queue_ticket_created_email")
class TicketSummaryEmailTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_user("owner-summary@example.com", "pass")
        self.company = CompanyProfile.objects.create(
            user=self.owner,
            company_name="Summary Co",
            support_email="support@example.com",
            support_email_app_password="app-password",
        )
        self.client.force_login(self.owner)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(_MEDIA_ROOT, ignore_errors=True)
        super().tearDownClass()

    def _open_ticket(self):
        return create_helpdesk_ticket_from_email(
            company=self.company,
            subject="Checking mails",
            body="hello checking mails",
            sender="client@example.com",
            message_id="<summary-open@example.com>",
            attachments=[("screenshot.png", b"png-bytes", "image/png")],
        )

    def test_summary_keeps_public_conversation_and_drops_internal_notes(self, *_mocks):
        ticket = self._open_ticket()
        add_comment(
            ticket=ticket,
            user=self.owner,
            body="please retry the export",
            internal_note=False,
        )
        add_comment(
            ticket=ticket,
            user=self.owner,
            body="secret staff note",
            internal_note=True,
        )

        text, html, files, _omitted, has_messages = build_conversation_summary(ticket)

        self.assertTrue(has_messages)
        self.assertIn("hello checking mails", text)
        self.assertIn("please retry the export", text)
        self.assertNotIn("secret staff note", text)
        self.assertNotIn("secret staff note", html)
        self.assertIn("hello checking mails", html)
        names = [name for name, _content, _mime in files]
        self.assertEqual(names, ["screenshot.png"])
        self.assertEqual(files[0][1], b"png-bytes")

    @patch("business.outbound_mail._graph_configured", return_value=False)
    @patch("business.ticket_summary_mail.send_outbound", return_value=(True, None))
    def test_summary_email_goes_to_the_typed_address(self, send_outbound, *_mocks):
        ticket = self._open_ticket()
        response = self.client.post(
            reverse("support_ticket_summary_email", args=[ticket.pk]),
            {"to": "manager@example.com"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertIn("manager@example.com", response.json()["message"])
        kwargs = send_outbound.call_args.kwargs
        self.assertEqual(kwargs["to_addresses"], ["manager@example.com"])
        self.assertIn("hello checking mails", kwargs["body"])
        self.assertEqual(kwargs["attachments"][0][0], "screenshot.png")

    @patch("business.ticket_summary_mail.send_outbound", return_value=(True, None))
    def test_summary_email_rejects_a_bad_address(self, send_outbound, *_mocks):
        ticket = self._open_ticket()
        response = self.client.post(
            reverse("support_ticket_summary_email", args=[ticket.pk]),
            {"to": "not-an-email"},
        )
        self.assertEqual(response.status_code, 400)
        send_outbound.assert_not_called()

    @patch("business.helpdesk_inbox.maybe_fetch_on_dashboard", return_value=None)
    def test_both_ticket_tables_show_summary_and_whatsapp_choice(self, *_mocks):
        self._open_ticket()
        internal = self.client.get(reverse("support_ticket_list"))
        self.assertEqual(internal.status_code, 200)
        self.assertContains(internal, "ticket360-summary-btn")
        self.assertContains(internal, "data-summary-channel=\"whatsapp\"")
        self.assertContains(internal, "WhatsApp sending is not available yet.")
        self.assertContains(internal, 'id="st-summary-to"')
        self.assertContains(internal, "/summary-email/")

        oem = self.client.get(reverse("support_ticket_list") + "?source=oem")
        self.assertEqual(oem.status_code, 200)
        self.assertContains(oem, ">Summary</th>")
        self.assertContains(oem, "data-summary-channel=\"email\"")
