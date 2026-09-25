"""Tests for WhatsApp multi-step chat flows."""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from accounts.models import CompanyProfile
from business.models import CaseLockProduct, Lead, Product, SupportTicket
from whatsapp.conversation_state import get_or_create_session, reset_session, start_flow
from whatsapp.inbound_chat import handle_inbound_customer_message, parse_inbound_message
from whatsapp.models import WhatsAppAccount, WhatsAppConversation

User = get_user_model()


class ParseInboundTests(TestCase):
    def test_button_reply(self):
        event = {
            "message": {
                "type": "interactive",
                "interactive": {
                    "type": "button_reply",
                    "button_reply": {"id": "procure", "title": "Procure"},
                },
            }
        }
        parsed = parse_inbound_message(event)
        self.assertEqual(parsed["reply_id"], "procure")


class InboundChatWelcomeTests(TestCase):
    def setUp(self):
        user = User.objects.create_user(email="chat@example.com", password="x")
        self.company = CompanyProfile.objects.create(user=user, company_name="Chat Co")
        self.account = WhatsAppAccount.objects.create(
            company=self.company,
            phone_number_id="333444",
            label="Main",
            access_token="tok",
        )

    def _text_event(self, wa_from="919900000001", message_id="wamid.1", body="Hi"):
        return {
            "type": "text",
            "message": {
                "from": wa_from,
                "id": message_id,
                "type": "text",
                "text": {"body": body},
            },
            "contacts": [{"profile": {"name": "Sam"}, "wa_id": wa_from}],
        }

    @patch("whatsapp.inbound_chat.send_main_welcome")
    def test_idle_inbound_sends_welcome_every_time(self, mock_welcome):
        handle_inbound_customer_message(self.account, self._text_event("wamid.1"))
        handle_inbound_customer_message(self.account, self._text_event("wamid.2", body="Again"))
        self.assertEqual(mock_welcome.call_count, 2)

    @patch("whatsapp.chat_flows.send_flow_prompt")
    @patch("whatsapp.inbound_chat.send_main_welcome")
    def test_procure_button_starts_flow(self, mock_welcome, mock_prompt):
        event = {
            "type": "interactive",
            "message": {
                "from": "919900000001",
                "id": "wamid.btn",
                "type": "interactive",
                "interactive": {
                    "type": "list_reply",
                    "list_reply": {"id": "procure", "title": "Procure products"},
                },
            },
            "contacts": [{"profile": {"name": "Sam"}, "wa_id": "919900000001"}],
        }
        handle_inbound_customer_message(self.account, event)
        mock_welcome.assert_not_called()
        mock_prompt.assert_called()
        lead = Lead.objects.get(whatsapp_wa_id="919900000001")
        session = get_or_create_session(lead)
        self.assertEqual(session.flow, "procure")
        self.assertEqual(session.step, "procure_company_name")


class ProcureFlowSubmitTests(TestCase):
    def setUp(self):
        user = User.objects.create_user(email="proc@example.com", password="x")
        self.company = CompanyProfile.objects.create(user=user, company_name="Proc Co")
        self.account = WhatsAppAccount.objects.create(
            company=self.company,
            phone_number_id="555666",
            access_token="tok",
        )
        self.product = Product.objects.create(
            company=self.company,
            name="Laptop Pro",
            category="hardware",
        )
        self.lead = Lead.objects.create(
            company=self.company,
            contact_name="Buyer",
            source=Lead.Source.WHATSAPP,
            whatsapp_wa_id="919911122233",
            phone="919911122233",
        )
        self.session = get_or_create_session(self.lead)

    @patch("whatsapp.chat_flows._send_end_menu")
    @patch("whatsapp.chat_flows.send_text_message")
    def test_submit_creates_lead_with_lines(self, mock_text, mock_end):
        from whatsapp.chat_flows import _handle_procure

        start_flow(
            self.session,
            flow="procure",
            step="procure_add_submit",
            draft={
                "company_name": "Acme Ltd",
                "lines": [
                    {
                        "product_id": self.product.pk,
                        "product_name": self.product.name,
                        "quantity": 2,
                        "category": "hardware",
                    }
                ],
            },
        )
        _handle_procure(
            account=self.account,
            lead=self.lead,
            session=self.session,
            to="919911122233",
            contact_name="Buyer",
            text="",
            reply_id="submit_req",
        )
        lead = Lead.objects.filter(company=self.company, company_name="Acme Ltd").latest("id")
        self.assertIn("Laptop Pro", lead.requirement)


class SupportFlowTests(TestCase):
    def setUp(self):
        user = User.objects.create_user(email="sup@example.com", password="x")
        self.company = CompanyProfile.objects.create(user=user, company_name="Sup Co")
        self.account = WhatsAppAccount.objects.create(
            company=self.company,
            phone_number_id="777888",
            access_token="tok",
        )
        self.lead = Lead.objects.create(
            company=self.company,
            contact_name="User",
            source=Lead.Source.WHATSAPP,
            whatsapp_wa_id="919922233344",
        )
        self.session = get_or_create_session(self.lead)

    @patch("whatsapp.chat_flows._send_end_menu")
    @patch("whatsapp.chat_flows.send_text_message")
    def test_support_ticket_created(self, mock_text, mock_end):
        from whatsapp.chat_flows import _create_support_ticket

        start_flow(
            self.session,
            flow="support",
            step="support_submit",
            draft={
                "subject": "Billing issue",
                "description": "Invoice wrong",
                "department": SupportTicket.Department.BILLING,
                "priority": SupportTicket.Priority.HIGH,
            },
        )
        _create_support_ticket(
            account=self.account,
            lead=self.lead,
            session=self.session,
            to="919922233344",
        )
        self.assertEqual(SupportTicket.objects.filter(company=self.company).count(), 1)


class CaseLockFlowTests(TestCase):
    def setUp(self):
        user = User.objects.create_user(email="cl@example.com", password="x")
        self.company = CompanyProfile.objects.create(user=user, company_name="CL Co")
        self.account = WhatsAppAccount.objects.create(
            company=self.company,
            phone_number_id="999000",
            access_token="tok",
        )
        self.cl_product = CaseLockProduct.objects.create(
            company=self.company, name="Office 365"
        )
        self.lead = Lead.objects.create(
            company=self.company,
            contact_name="Vendor",
            source=Lead.Source.WHATSAPP,
            whatsapp_wa_id="919933344455",
        )
        self.session = get_or_create_session(self.lead)

    @patch("whatsapp.chat_flows._send_end_menu")
    @patch("whatsapp.chat_flows.send_text_message")
    def test_caselock_submission(self, mock_text, mock_end):
        from whatsapp.chat_flows import _create_caselock_submission

        start_flow(
            self.session,
            flow="caselock",
            step="caselock_submit",
            draft={
                "client_name": "Hyfun",
                "client_gst": "27AAAAA0000A1Z5",
                "caselock_product_id": self.cl_product.pk,
                "quantity": 3,
            },
        )
        _create_caselock_submission(
            account=self.account,
            session=self.session,
            to="919933344455",
        )
        from business.models import CaseLockSubmission

        self.assertEqual(CaseLockSubmission.objects.filter(client_name="Hyfun").count(), 1)


class EndMenuTests(TestCase):
    def setUp(self):
        user = User.objects.create_user(email="end@example.com", password="x")
        self.company = CompanyProfile.objects.create(user=user, company_name="End Co")
        self.account = WhatsAppAccount.objects.create(
            company=self.company,
            phone_number_id="111000",
            access_token="tok",
        )
        self.lead = Lead.objects.create(
            company=self.company,
            contact_name="X",
            source=Lead.Source.WHATSAPP,
            whatsapp_wa_id="919944455566",
        )
        self.session = get_or_create_session(self.lead)
        start_flow(self.session, flow="procure", step="await_end_choice", draft={})

    @patch("whatsapp.chat_messaging.send_text_message")
    def test_bye_resets_idle_and_thanks(self, mock_text):
        from whatsapp.chat_messaging import BYE_THANK_YOU
        from whatsapp.chat_flows import handle_await_end_choice

        handle_await_end_choice(
            account=self.account,
            session=self.session,
            to="919944455566",
            text="",
            reply_id="bye_now",
        )
        self.session.refresh_from_db()
        self.assertEqual(self.session.phase, WhatsAppConversation.Phase.IDLE)
        mock_text.assert_called_once()
        self.assertEqual(mock_text.call_args.kwargs["body"], BYE_THANK_YOU)

    @patch("whatsapp.chat_flows.send_flow_prompt")
    @patch("whatsapp.chat_flows.send_main_welcome")
    def test_something_else_does_not_show_welcome(self, mock_welcome, mock_prompt):
        from whatsapp.chat_flows import handle_await_end_choice

        handle_await_end_choice(
            account=self.account,
            session=self.session,
            to="919944455566",
            text="",
            reply_id="something_else",
        )
        mock_welcome.assert_not_called()
        self.session.refresh_from_db()
        self.assertEqual(self.session.phase, WhatsAppConversation.Phase.IDLE)

    @patch("whatsapp.inbound_chat.send_main_welcome")
    @patch("whatsapp.chat_messaging.send_text_message")
    def test_after_bye_next_message_shows_welcome(self, mock_text, mock_welcome):
        from whatsapp.chat_flows import handle_await_end_choice
        from whatsapp.inbound_chat import handle_inbound_customer_message

        handle_await_end_choice(
            account=self.account,
            session=self.session,
            to="919944455566",
            text="",
            reply_id="bye_now",
        )
        mock_welcome.reset_mock()
        result = handle_inbound_customer_message(
            self.account,
            {
                "type": "text",
                "message": {
                    "from": "919944455566",
                    "id": "wamid.after.bye",
                    "type": "text",
                    "text": {"body": "Hello again"},
                },
                "contacts": [{"profile": {"name": "X"}, "wa_id": "919944455566"}],
            },
        )
        self.assertTrue(result["welcome_sent"])
        mock_welcome.assert_called_once()
