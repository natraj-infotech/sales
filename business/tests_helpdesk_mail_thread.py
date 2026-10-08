import base64
import shutil
import tempfile

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

_MEDIA_ROOT = tempfile.mkdtemp(prefix="ticket-mail-")
from django.urls import reverse
from helpdesk.models import FollowUp, Ticket
from unittest.mock import patch

from accounts.models import CompanyProfile
from business.email_body_clean import (
    clean_inbound_email_body,
    inline_cids_in_main_body,
    plain_main_body,
)
from business.helpdesk_bridge import followups_as_comments
from business.helpdesk_inbox import (
    _import_graph_item,
    add_email_reply,
    create_helpdesk_ticket_from_email,
    message_files_from_graph,
    open_ticket_for_conversation,
)


_SAMPLE_HTML = """
<html><body>
<img src="cid:logo@company">
<p>Change email ids sales module</p>
<img src="cid:screen@company">
<p>Thank you.</p>
<p>Best Regards,<br>Natraj Infotech Pvt Ltd<br>Mob: - 9909022627</p>
<img src="cid:footer@company">
</body></html>
"""


class InboundBodyCleanTests(TestCase):
    def test_glued_signature_is_removed(self):
        raw = (
            "Change email ids sales moduleThank you.Best Regards, "
            "Natraj Infotech Pvt Ltd. Dharmendra Bhankhodiya Mob: - 9909022627"
        )
        self.assertEqual(
            clean_inbound_email_body(raw, empty_fallback=""),
            "Change email ids sales module",
        )

    def test_html_keeps_the_message_and_drops_header_footer(self):
        text = plain_main_body(_SAMPLE_HTML, "html", empty_fallback="")
        self.assertIn("Change email ids sales module", text)
        self.assertNotIn("Best Regards", text)
        self.assertNotIn("9909022627", text)
        self.assertNotIn("Thank you", text)

    def test_main_body_image_is_kept_and_logo_is_not(self):
        kept = inline_cids_in_main_body(_SAMPLE_HTML)
        self.assertIn("screen@company", kept)
        self.assertNotIn("logo@company", kept)
        self.assertNotIn("footer@company", kept)

    def test_paperclip_file_is_kept_and_signature_logo_is_not(self):
        png = base64.b64encode(b"png-bytes").decode()
        files = message_files_from_graph(
            [
                {
                    "@odata.type": "#microsoft.graph.fileAttachment",
                    "name": "screenshot.png",
                    "contentType": "image/png",
                    "contentBytes": png,
                    "isInline": False,
                },
                {
                    "@odata.type": "#microsoft.graph.fileAttachment",
                    "name": "logo.png",
                    "contentType": "image/png",
                    "contentBytes": png,
                    "isInline": True,
                    "contentId": "logo@company",
                },
                {
                    "@odata.type": "#microsoft.graph.fileAttachment",
                    "name": "screen.png",
                    "contentType": "image/png",
                    "contentBytes": png,
                    "isInline": True,
                    "contentId": "screen@company",
                },
            ],
            _SAMPLE_HTML,
        )
        names = [name for name, _data, _mime in files]
        self.assertIn("screenshot.png", names)
        self.assertIn("screen.png", names)
        self.assertNotIn("logo.png", names)

    def test_outlook_image001_logo_is_dropped_and_body_screenshot_is_kept(self):
        png = base64.b64encode(b"png-bytes").decode()
        html = """
        <html><body>
        <img src="cid:image001.png@01">
        <p>Natraj Infotech Pvt Ltd</p>
        <p>Mob: - 9909022627</p>
        <p>Change email ids sales module</p>
        <img src="cid:image002.png@01">
        <p>Best Regards,<br>Natraj Infotech Pvt Ltd</p>
        <img src="cid:image003.png@01">
        </body></html>
        """
        text = plain_main_body(html, "html", empty_fallback="")
        self.assertIn("Change email ids sales module", text)
        self.assertNotIn("Natraj", text)
        self.assertNotIn("9909022627", text)
        self.assertNotIn("Best Regards", text)
        files = message_files_from_graph(
            [
                {
                    "@odata.type": "#microsoft.graph.fileAttachment",
                    "name": "image001.png",
                    "contentType": "image/png",
                    "contentBytes": png,
                    "isInline": False,
                    "contentId": "image001.png@01",
                },
                {
                    "@odata.type": "#microsoft.graph.fileAttachment",
                    "name": "image002.png",
                    "contentType": "image/png",
                    "contentBytes": png,
                    "isInline": True,
                    "contentId": "image002.png@01",
                },
                {
                    "@odata.type": "#microsoft.graph.fileAttachment",
                    "name": "image003.png",
                    "contentType": "image/png",
                    "contentBytes": png,
                    "isInline": True,
                    "contentId": "image003.png@01",
                },
                {
                    "@odata.type": "#microsoft.graph.fileAttachment",
                    "name": "invoice.pdf",
                    "contentType": "application/pdf",
                    "contentBytes": base64.b64encode(b"pdf").decode(),
                    "isInline": False,
                },
            ],
            html,
        )
        names = [name for name, _data, _mime in files]
        self.assertEqual(names, ["image002.png", "invoice.pdf"])


@override_settings(MEDIA_ROOT=_MEDIA_ROOT)
@patch("business.team_notifications.notify_new_ticket")
@patch("business.helpdesk_assign_mail.queue_assignment_emails")
@patch("business.helpdesk_assign_mail.queue_ticket_created_email")
class TicketMailThreadTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_user("owner@mailthread.example", "pass")
        self.company = CompanyProfile.objects.create(
            user=self.owner, company_name="Mail Thread Co"
        )
        self.client.force_login(self.owner)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(_MEDIA_ROOT, ignore_errors=True)
        super().tearDownClass()

    def _open_ticket(self):
        return create_helpdesk_ticket_from_email(
            company=self.company,
            subject="Change Email ids",
            body=plain_main_body(_SAMPLE_HTML, "html"),
            sender="client@example.com",
            message_id="<open@example.com>",
            conversation_id="conv-original",
            attachments=message_files_from_graph(
                [
                    {
                        "@odata.type": "#microsoft.graph.fileAttachment",
                        "name": "screenshot.png",
                        "contentType": "image/png",
                        "contentBytes": base64.b64encode(b"png-bytes").decode(),
                        "isInline": False,
                    }
                ],
                _SAMPLE_HTML,
            ),
        )

    def test_ticket_page_shows_main_text_and_attached_image(self, *_mocks):
        ticket = self._open_ticket()
        response = self.client.get(reverse("support_ticket_detail", args=[ticket.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Change email ids sales module")
        self.assertNotContains(response, "Best Regards")
        self.assertNotContains(response, "9909022627")
        opening = FollowUp.objects.get(ticket=ticket, message_id="<open@example.com>")
        attachment = opening.followupattachment_set.get()
        self.assertContains(
            response,
            reverse("support_ticket_attachment", args=[ticket.pk, attachment.pk]),
        )
        file_response = self.client.get(
            reverse(
                "support_ticket_attachment",
                args=[ticket.pk, attachment.pk],
            )
        )
        self.assertEqual(file_response.status_code, 200)
        self.assertIn("image/png", file_response["Content-Type"])

    def test_reply_on_the_same_mail_shows_under_the_ticket(self, *_mocks):
        ticket = self._open_ticket()
        same = open_ticket_for_conversation(self.company, "conv-original")
        self.assertEqual(same.pk, ticket.pk)
        add_email_reply(
            ticket,
            body="The id is still wrong.",
            sender="client@example.com",
            message_id="<reply@example.com>",
            attachments=[
                ("followup.png", b"png-bytes", "image/png"),
            ],
        )
        comments = followups_as_comments(ticket)
        bodies = [c.body for c in comments]
        self.assertIn("The id is still wrong.", bodies)
        self.assertIn("Change email ids sales module", bodies)
        self.assertLess(
            bodies.index("Change email ids sales module"),
            bodies.index("The id is still wrong."),
        )
        self.assertTrue(any(c.attachments for c in comments))

        response = self.client.get(reverse("support_ticket_detail", args=[ticket.pk]))
        self.assertContains(response, "The id is still wrong.")
        self.assertContains(response, "followup.png")

    def test_sent_reply_joins_the_thread_without_letterhead(self, *_mocks):
        ticket = self._open_ticket()
        html = """
        <html><body>
        <img src="cid:logo@company">
        <p>Natraj Infotech Pvt Ltd</p>
        <p>We have updated the email id.</p>
        <p>Best Regards,<br>Natraj Infotech Pvt Ltd</p>
        <img src="cid:footer@company">
        <div id="divRplyFwdMsg">
        <p>From: client@example.com</p>
        <p>Change email ids sales module</p>
        </div>
        </body></html>
        """
        png = base64.b64encode(b"png-bytes").decode()
        item = {
            "id": "sent-1",
            "subject": "Re: Change Email ids",
            "from": {"emailAddress": {"address": "support@mailthread.example"}},
            "internetMessageId": "<sent@example.com>",
            "conversationId": "conv-original",
            "sentDateTime": "2099-01-01T00:00:00Z",
            "body": {"contentType": "html", "content": html},
            "hasAttachments": True,
        }
        with patch(
            "business.helpdesk_inbox._files_for_graph_item",
            return_value=message_files_from_graph(
                [
                    {
                        "@odata.type": "#microsoft.graph.fileAttachment",
                        "name": "logo.png",
                        "contentType": "image/png",
                        "contentBytes": png,
                        "isInline": True,
                        "contentId": "logo@company",
                    },
                    {
                        "@odata.type": "#microsoft.graph.fileAttachment",
                        "name": "proof.pdf",
                        "contentType": "application/pdf",
                        "contentBytes": base64.b64encode(b"pdf").decode(),
                        "isInline": False,
                    },
                ],
                html,
            ),
        ):
            outcome = _import_graph_item(
                self.company,
                item,
                access_token="token",
                mailbox="support@mailthread.example",
                sent=True,
            )
        self.assertEqual(outcome, "reply")
        comments = followups_as_comments(ticket)
        sent = next(c for c in comments if c.outbound)
        self.assertEqual(sent.user_email, "support@mailthread.example")
        self.assertIn("We have updated the email id.", sent.body)
        self.assertNotIn("Best Regards", sent.body)
        self.assertNotIn("Natraj", sent.body)
        self.assertNotIn("Change email ids", sent.body)
        self.assertEqual([a.name for a in sent.attachments], ["proof.pdf"])

        stray = {
            "id": "sent-other",
            "subject": "Hello someone else",
            "from": {"emailAddress": {"address": "support@mailthread.example"}},
            "internetMessageId": "<other@example.com>",
            "conversationId": "some-other-conversation",
            "sentDateTime": "2099-01-02T00:00:00Z",
            "body": {"contentType": "text", "content": "Not this ticket."},
            "hasAttachments": False,
        }
        self.assertEqual(
            _import_graph_item(
                self.company,
                stray,
                access_token="token",
                mailbox="support@mailthread.example",
                sent=True,
            ),
            "skipped",
        )
        self.assertFalse(
            FollowUp.objects.filter(message_id="<other@example.com>").exists()
        )

        response = self.client.get(reverse("support_ticket_detail", args=[ticket.pk]))
        self.assertContains(response, "We have updated the email id.")
        self.assertContains(response, "proof.pdf")
        self.assertContains(response, "Sent")
        self.assertNotContains(response, "logo.png")

    def test_reply_after_close_does_not_join_the_ticket(self, *_mocks):
        ticket = self._open_ticket()
        Ticket.objects.filter(pk=ticket.pk).update(status=Ticket.CLOSED_STATUS)
        self.assertIsNone(
            open_ticket_for_conversation(self.company, "conv-original")
        )
        self.assertFalse(
            FollowUp.objects.filter(
                ticket=ticket, message_id="<after-close@example.com>"
            ).exists()
        )
