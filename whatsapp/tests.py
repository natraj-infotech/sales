"""Tests for Lead ingest and WhatsApp lead flow handler."""

from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import CompanyProfile
from business.lead_ingest import create_lead_from_email, create_lead_from_whatsapp
from business.models import Lead, Product
from whatsapp.handlers import lead_flow_handler
from whatsapp.models import WhatsAppAccount

User = get_user_model()


class LeadIngestTests(TestCase):
    def setUp(self):
        user = User.objects.create_user(email="owner@example.com", password="x")
        self.company = CompanyProfile.objects.create(user=user, company_name="Acme")
        self.product = Product.objects.create(company=self.company, name="Tracker Pro")

    def test_whatsapp_lead_created_with_products(self):
        lead, created = create_lead_from_whatsapp(
            company=self.company,
            contact_name="Riya",
            phone="919999999999",
            company_name="Buyer Co",
            product_ids=[self.product.pk],
            whatsapp_wa_id="919999999999",
            whatsapp_message_id="wamid.ABC",
        )
        self.assertTrue(created)
        self.assertEqual(lead.source, Lead.Source.WHATSAPP)
        self.assertEqual(list(lead.products.all()), [self.product])

    def test_whatsapp_idempotent_on_message_id(self):
        create_lead_from_whatsapp(
            company=self.company,
            contact_name="Riya",
            whatsapp_message_id="wamid.DUP",
        )
        lead2, created = create_lead_from_whatsapp(
            company=self.company,
            contact_name="Other",
            whatsapp_message_id="wamid.DUP",
        )
        self.assertFalse(created)
        self.assertEqual(lead2.contact_name, "Riya")
        self.assertEqual(Lead.objects.filter(company=self.company).count(), 1)

    def test_email_lead_idempotent(self):
        create_lead_from_email(
            company=self.company,
            contact_name="Mail User",
            email="a@b.com",
            email_message_id="<msg-1@mail>",
            requirement="Need licenses",
        )
        lead2, created = create_lead_from_email(
            company=self.company,
            contact_name="Mail User 2",
            email="a@b.com",
            email_message_id="<msg-1@mail>",
        )
        self.assertFalse(created)
        self.assertEqual(Lead.objects.filter(source=Lead.Source.EMAIL).count(), 1)


class LeadFlowHandlerTests(TestCase):
    def setUp(self):
        user = User.objects.create_user(email="owner2@example.com", password="x")
        self.company = CompanyProfile.objects.create(user=user, company_name="Beta")
        self.product = Product.objects.create(company=self.company, name="Suite")
        self.account = WhatsAppAccount.objects.create(
            company=self.company,
            phone_number_id="123456",
            label="Main",
        )

    def test_ping(self):
        resp = lead_flow_handler(self.account, {"version": "3.0", "action": "ping"})
        self.assertEqual(resp["data"]["status"], "active")

    def test_complete_creates_lead(self):
        resp = lead_flow_handler(
            self.account,
            {
                "version": "3.0",
                "action": "data_exchange",
                "data": {
                    "contact_name": "Sam",
                    "company_name": "Sam Corp",
                    "email": "sam@ex.com",
                    "phone": "911",
                    "requirement": "Demo",
                    "product_ids": [self.product.pk],
                    "message_id": "wamid.FLOW1",
                    "wa_id": "911",
                },
            },
        )
        self.assertEqual(resp["screen"], "SUCCESS")
        lead = Lead.objects.get(whatsapp_message_id="wamid.FLOW1")
        self.assertEqual(lead.contact_name, "Sam")
        self.assertEqual(lead.source, Lead.Source.WHATSAPP)
        self.assertEqual(lead.products.get(), self.product)


@override_settings(WHATSAPP_VERIFY_TOKEN="secret-token")
class WhatsAppWebhookVerifyTests(TestCase):
    def test_verify_ok(self):
        url = reverse("whatsapp_webhook")
        resp = self.client.get(
            url,
            {
                "hub.mode": "subscribe",
                "hub.verify_token": "secret-token",
                "hub.challenge": "12345",
            },
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.content.decode(), "12345")

    def test_verify_forbidden(self):
        url = reverse("whatsapp_webhook")
        resp = self.client.get(
            url,
            {
                "hub.mode": "subscribe",
                "hub.verify_token": "wrong",
                "hub.challenge": "12345",
            },
        )
        self.assertEqual(resp.status_code, 403)


class WhatsAppConversationWelcomeTests(TestCase):
    def setUp(self):
        user = User.objects.create_user(email="owner3@example.com", password="x")
        self.company = CompanyProfile.objects.create(user=user, company_name="Gamma")
        self.account = WhatsAppAccount.objects.create(
            company=self.company,
            phone_number_id="999888",
            label="Prod",
            access_token="test-token",
        )

    def _event(self, message_id="wamid.1", wa_from="919876543210", text="Hi"):
        return {
            "type": "text",
            "message": {
                "from": wa_from,
                "id": message_id,
                "timestamp": "1700000000",
                "type": "text",
                "text": {"body": text},
            },
            "contacts": [{"profile": {"name": "Asha"}, "wa_id": wa_from}],
            "metadata": {"phone_number_id": self.account.phone_number_id},
        }

    @patch("whatsapp.inbound_chat.send_main_welcome")
    def test_first_message_sends_welcome(self, mock_welcome):
        from whatsapp.inbound_chat import handle_inbound_customer_message

        result = handle_inbound_customer_message(self.account, self._event())
        self.assertTrue(result["welcome_sent"])
        mock_welcome.assert_called_once()
        lead = Lead.objects.get(whatsapp_wa_id="919876543210")
        self.assertIsNotNone(lead.last_inbound_at)
        self.assertIsNotNone(lead.last_welcome_sent_at)

    @patch("whatsapp.inbound_chat.send_main_welcome")
    def test_second_message_while_idle_sends_welcome_again(self, mock_welcome):
        from whatsapp.inbound_chat import handle_inbound_customer_message

        handle_inbound_customer_message(self.account, self._event("wamid.1"))
        mock_welcome.reset_mock()
        result = handle_inbound_customer_message(
            self.account, self._event("wamid.2", text="Still here")
        )
        self.assertTrue(result["welcome_sent"])
        mock_welcome.assert_called_once()

    @patch("whatsapp.inbound_chat.send_main_welcome")
    def test_duplicate_message_id_skipped(self, mock_welcome):
        from whatsapp.inbound_chat import handle_inbound_customer_message

        handle_inbound_customer_message(self.account, self._event("wamid.dup"))
        mock_welcome.reset_mock()
        result = handle_inbound_customer_message(
            self.account, self._event("wamid.dup")
        )
        self.assertEqual(result["skipped"], "duplicate_message")
        mock_welcome.assert_not_called()


class EnvWhatsAppAccountInboundTests(TestCase):
    """Inbound without a DB WhatsAppAccount row still creates leads + welcome."""

    def setUp(self):
        user = User.objects.create_user(email="env@example.com", password="x")
        self.company = CompanyProfile.objects.create(user=user, company_name="Env Co")

    def _event(self, message_id="wamid.env1", wa_from="919111222333"):
        return {
            "type": "text",
            "message": {
                "from": wa_from,
                "id": message_id,
                "timestamp": "1700000000",
                "type": "text",
                "text": {"body": "Hi"},
            },
            "contacts": [{"profile": {"name": "Ravi"}, "wa_id": wa_from}],
            "metadata": {"phone_number_id": "555444"},
        }

    @patch("whatsapp.inbound_chat.send_main_welcome")
    def test_env_account_creates_lead_and_welcome(self, mock_welcome):
        from whatsapp.account_resolve import EnvWhatsAppAccount
        from whatsapp.inbound_chat import handle_inbound_customer_message

        account = EnvWhatsAppAccount(phone_number_id="555444", company=self.company)
        result = handle_inbound_customer_message(account, self._event())
        self.assertTrue(result["handled"])
        self.assertTrue(result["welcome_sent"])
        mock_welcome.assert_called_once()
        lead = Lead.objects.get(whatsapp_wa_id="919111222333", company=self.company)
        self.assertEqual(lead.contact_name, "Ravi")
        self.assertEqual(lead.source, Lead.Source.WHATSAPP)

    @patch("whatsapp.inbound_chat.send_main_welcome")
    def test_welcome_and_lead_when_no_company_in_database(self, mock_welcome):
        from whatsapp.account_resolve import EnvWhatsAppAccount
        from whatsapp.inbound_chat import handle_inbound_customer_message

        CompanyProfile.objects.all().delete()
        account = EnvWhatsAppAccount(phone_number_id="555444", company=None)
        result = handle_inbound_customer_message(account, self._event())
        self.assertTrue(result["welcome_sent"])
        self.assertTrue(result["handled"])
        lead = Lead.objects.get(whatsapp_wa_id="919111222333")
        self.assertIsNone(lead.company_id)
        mock_welcome.assert_called_once()
