"""Tests for Stop button helpers."""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from accounts.models import CompanyProfile
from business.models import Lead
from whatsapp.chat_messaging import STOP_ID, end_session, send_flow_buttons
from whatsapp.conversation_state import get_or_create_session, start_flow
from whatsapp.models import WhatsAppAccount, WhatsAppConversation

User = get_user_model()


class StopButtonTests(TestCase):
    def setUp(self):
        user = User.objects.create_user(email="stop@example.com", password="x")
        self.company = CompanyProfile.objects.create(user=user, company_name="Stop Co")
        self.account = WhatsAppAccount.objects.create(
            company=self.company,
            phone_number_id="121212",
            access_token="tok",
        )
        self.lead = Lead.objects.create(
            company=self.company,
            contact_name="U",
            whatsapp_wa_id="919900000099",
        )
        self.session = get_or_create_session(self.lead)

    @patch("whatsapp.chat_messaging.send_interactive_reply_buttons")
    def test_flow_buttons_always_includes_stop(self, mock_send):
        send_flow_buttons(
            account=self.account,
            to="919900000099",
            body_text="Pick one",
            buttons=[{"id": "a", "title": "Alpha"}, {"id": "b", "title": "Beta"}],
        )
        buttons = mock_send.call_args.kwargs["buttons"]
        self.assertEqual(len(buttons), 3)
        self.assertEqual(buttons[-1]["id"], STOP_ID)

    @patch("whatsapp.chat_messaging.send_text_message")
    def test_end_session_resets_to_idle(self, mock_text):
        start_flow(self.session, flow="procure", step="procure_company_name", draft={})
        end_session(account=self.account, session=self.session, to="919900000099")
        self.session.refresh_from_db()
        self.assertEqual(self.session.phase, WhatsAppConversation.Phase.IDLE)
        mock_text.assert_called_once()
