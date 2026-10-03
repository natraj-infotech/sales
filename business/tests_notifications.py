from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from helpdesk.models import FollowUp, Queue, Ticket
from helpdesk.signals import update_ticket_done

from accounts.models import CompanyMember, CompanyProfile
from business.models import (
    CaseLockInvite,
    CaseLockSubmission,
    Client,
    ClientSubscription,
    DealRound,
    Lead,
    Product,
    SubscriptionRenewalInvite,
    SubscriptionRenewalInviteLine,
    SupportTicketMeta,
    TeamNotification,
    Vendor,
    VendorPriceSubmission,
)
from business.expiry_notifications import send_team_expiry_digest
from business.team_notifications import (
    notify_caselock_submitted,
    notify_email_failed,
    notify_new_lead,
    notify_new_ticket,
    notify_quotation_submitted,
    notify_renewal_confirmed,
    notify_ticket_assigned,
    purge_old_notifications,
    scan_overdue_tickets,
    scan_subscription_expiry,
)


class TeamNotificationTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_user("owner@example.com", "pass")
        self.member = User.objects.create_user("member@example.com", "pass")
        self.company = CompanyProfile.objects.create(
            user=self.owner,
            company_name="Co",
            expiry_notify_at_days="30,15,7",
        )
        CompanyMember.objects.create(
            company=self.company,
            user=self.member,
            sales_access=False,
        )
        self.vendor = Vendor.objects.create(company=self.company, name="ABC Traders")

    def test_lead_goes_to_sales_users_only(self):
        lead = Lead.objects.create(
            company=self.company,
            contact_name="Sharma",
            source=Lead.Source.MANUAL,
        )
        created = notify_new_lead(lead)
        self.assertEqual(created, 1)
        self.assertTrue(
            TeamNotification.objects.filter(
                recipient=self.owner,
                kind=TeamNotification.Kind.NEW_LEAD,
            ).exists()
        )
        self.assertFalse(
            TeamNotification.objects.filter(recipient=self.member).exists()
        )

    def test_ticket_goes_to_whole_support_team_and_opens_detail(self):
        queue = Queue.objects.create(title="Co", slug=f"c{self.company.pk}-co")
        ticket = Ticket.objects.create(
            title="Login not working",
            queue=queue,
            description="Cannot sign in",
            submitter_email="client@example.com",
        )
        created = notify_new_ticket(ticket)
        self.assertEqual(created, 2)
        note = TeamNotification.objects.get(recipient=self.owner, dedupe_key=f"support_ticket:{ticket.pk}")
        self.client.force_login(self.owner)
        response = self.client.get(reverse("notification_open", args=[note.pk]))
        self.assertRedirects(
            response,
            reverse("support_ticket_detail", args=[ticket.pk]),
            fetch_redirect_response=False,
        )
        note.refresh_from_db()
        self.assertTrue(note.is_read)

    def test_quotation_and_final_are_separate_kinds(self):
        normal = DealRound.objects.create(
            company=self.company, deal_no="D-18", round_number=1
        )
        final = DealRound.objects.create(
            company=self.company, deal_no="D-18", round_number=2, is_final=True
        )
        first = VendorPriceSubmission.objects.create(
            company=self.company,
            deal_no="D-18",
            vendor=self.vendor,
            deal_round=normal,
            expires_at=timezone.now() + timedelta(days=1),
            is_submitted=True,
        )
        second = VendorPriceSubmission.objects.create(
            company=self.company,
            deal_no="D-18",
            vendor=self.vendor,
            deal_round=final,
            expires_at=timezone.now() + timedelta(days=1),
            is_submitted=True,
            is_final_quotation=True,
        )
        notify_quotation_submitted(first)
        notify_quotation_submitted(second)
        kinds = set(
            TeamNotification.objects.filter(recipient=self.owner).values_list(
                "kind", flat=True
            )
        )
        self.assertEqual(
            kinds,
            {
                TeamNotification.Kind.QUOTATION_PRICE,
                TeamNotification.Kind.FINAL_QUOTATION,
            },
        )
        price_note = TeamNotification.objects.get(
            recipient=self.owner, kind=TeamNotification.Kind.QUOTATION_PRICE
        )
        self.assertIn(f"vendor={self.vendor.pk}", price_note.target_url)

    def test_caselock_form_notifies_owner(self):
        invite = CaseLockInvite.objects.create(
            company=self.company,
            source_message_id="msg-1",
            sender_email="client@example.com",
        )
        submission = CaseLockSubmission.objects.create(
            invite=invite,
            client_name="Acme",
            product_name="Lock",
            product_quantity=2,
        )
        notify_caselock_submitted(submission)
        note = TeamNotification.objects.get(recipient=self.owner)
        self.assertEqual(note.kind, TeamNotification.Kind.CASELOCK_SUBMIT)
        self.assertIn(f"submission={submission.pk}", note.target_url)
        self.assertFalse(
            TeamNotification.objects.filter(recipient=self.member).exists()
        )

    def test_overdue_and_expiry_scan_once(self):
        queue = Queue.objects.create(title="Co", slug=f"c{self.company.pk}-co")
        ticket = Ticket.objects.create(
            title="Stuck ticket",
            queue=queue,
            description="Still open",
            status=Ticket.OPEN_STATUS,
        )
        SupportTicketMeta.objects.create(
            ticket=ticket,
            resolve_due_at=timezone.now() - timedelta(hours=2),
        )
        client = Client.objects.create(company=self.company, name="OMSGuru")
        product = Product.objects.create(company=self.company, name="Microsoft 365")
        ClientSubscription.objects.create(
            company=self.company,
            client=client,
            product=product,
            status=ClientSubscription.Status.ACTIVE,
            start_date=timezone.localdate() - timedelta(days=300),
            end_date=timezone.localdate() + timedelta(days=6),
            quantity=1,
            billing_amount=Decimal("100.00"),
        )
        self.assertEqual(scan_overdue_tickets(), 2)
        self.assertEqual(scan_subscription_expiry(), 1)
        self.assertEqual(scan_overdue_tickets(), 0)
        self.assertEqual(scan_subscription_expiry(), 0)
        expiry = TeamNotification.objects.get(
            recipient=self.owner, kind=TeamNotification.Kind.SUBSCRIPTION_EXPIRY
        )
        self.assertIn("expires in 6 days", expiry.title)
        self.assertIn("focus=", expiry.target_url)

    def test_old_rows_are_deleted_after_a_week(self):
        note = TeamNotification.objects.create(
            company=self.company,
            recipient=self.owner,
            kind=TeamNotification.Kind.NEW_LEAD,
            title="Old",
            target_url="/leads/1/",
            dedupe_key="lead:old",
        )
        TeamNotification.objects.filter(pk=note.pk).update(
            created_at=timezone.now() - timedelta(days=8)
        )
        self.assertEqual(purge_old_notifications(), 1)
        self.assertFalse(TeamNotification.objects.filter(pk=note.pk).exists())

    def _ticket(self, **kwargs):
        queue = Queue.objects.create(title="Co", slug=f"c{self.company.pk}-co")
        return Ticket.objects.create(
            title="Printer down",
            queue=queue,
            description="Help",
            submitter_email="client@example.com",
            **kwargs,
        )

    def test_renewal_confirmed_goes_to_sales_and_opens_detail(self):
        client = Client.objects.create(company=self.company, name="OMSGuru")
        product = Product.objects.create(company=self.company, name="Microsoft 365")
        sub = ClientSubscription.objects.create(
            company=self.company,
            client=client,
            product=product,
            status=ClientSubscription.Status.ACTIVE,
            start_date=timezone.localdate() - timedelta(days=300),
            end_date=timezone.localdate() + timedelta(days=20),
            quantity=5,
            billing_amount=Decimal("100.00"),
        )
        invite = SubscriptionRenewalInvite.objects.create(
            company=self.company,
            client_email="it@omsguru.com",
            submitted_at=timezone.now(),
            submitted_by_email="it@omsguru.com",
        )
        SubscriptionRenewalInviteLine.objects.create(
            invite=invite,
            subscription=sub,
            company_name="OMSGuru",
            product_name="Microsoft 365",
            current_quantity=5,
            renewal_count=4,
        )
        self.assertEqual(notify_renewal_confirmed(invite), 1)
        self.assertEqual(notify_renewal_confirmed(invite), 0)
        note = TeamNotification.objects.get(recipient=self.owner)
        self.assertEqual(note.kind, TeamNotification.Kind.RENEWAL_CONFIRMED)
        self.assertEqual(note.title, "OMSGuru confirmed renewal quantities")
        self.assertIn("1 of 1 subscription(s) to renew", note.body)
        self.assertEqual(
            note.target_url, reverse("client_renewal_detail", args=[invite.pk])
        )
        self.assertFalse(
            TeamNotification.objects.filter(recipient=self.member).exists()
        )

    def test_ticket_assignment_notifies_only_the_assignee(self):
        ticket = self._ticket()
        self.assertEqual(
            notify_ticket_assigned(ticket, self.member, assigned_by=self.owner), 1
        )
        note = TeamNotification.objects.get(recipient=self.member)
        self.assertEqual(note.kind, TeamNotification.Kind.TICKET_ASSIGNED)
        self.assertIn("assigned to you", note.title)
        self.assertIn("owner@example.com", note.body)
        self.assertFalse(
            TeamNotification.objects.filter(recipient=self.owner).exists()
        )

    def test_self_assignment_is_silent(self):
        ticket = self._ticket()
        self.assertEqual(
            notify_ticket_assigned(ticket, self.owner, assigned_by=self.owner), 0
        )

    def test_customer_reply_goes_to_assignee(self):
        ticket = self._ticket(assigned_to=self.member)
        followup = FollowUp.objects.create(
            ticket=ticket, title="E-Mail Received", comment="Still broken", public=True
        )
        update_ticket_done.send(
            sender="create_object_from_email_message", followup=followup
        )
        note = TeamNotification.objects.get(recipient=self.member)
        self.assertEqual(note.kind, TeamNotification.Kind.TICKET_REPLY)
        self.assertIn("New reply on", note.title)
        self.assertEqual(note.body, "Still broken")
        self.assertFalse(
            TeamNotification.objects.filter(recipient=self.owner).exists()
        )

    def test_reopen_by_email_without_assignee_goes_to_support_team(self):
        ticket = self._ticket(status=Ticket.REOPENED_STATUS)
        followup = FollowUp.objects.create(
            ticket=ticket,
            title="Re-opened",
            comment="Back again",
            public=True,
            new_status=Ticket.REOPENED_STATUS,
        )
        update_ticket_done.send(
            sender="create_object_from_email_message", followup=followup
        )
        notes = TeamNotification.objects.filter(kind=TeamNotification.Kind.TICKET_REPLY)
        self.assertEqual(notes.count(), 2)
        self.assertIn("reopened by customer reply", notes.first().title)

    def test_staff_updates_do_not_count_as_customer_reply(self):
        ticket = self._ticket(assigned_to=self.member)
        followup = FollowUp.objects.create(ticket=ticket, title="Comment", comment="x")
        update_ticket_done.send(sender="update_ticket", followup=followup)
        self.assertFalse(TeamNotification.objects.exists())

    def test_email_failure_goes_to_owner_once_per_day(self):
        self.assertEqual(
            notify_email_failed(self.company, area="team_digest", title="Digest failed"),
            1,
        )
        self.assertEqual(
            notify_email_failed(self.company, area="team_digest", title="Digest failed"),
            0,
        )
        note = TeamNotification.objects.get(recipient=self.owner)
        self.assertEqual(note.kind, TeamNotification.Kind.EMAIL_FAILED)
        self.assertEqual(note.target_url, reverse("company_setup"))
        self.assertFalse(
            TeamNotification.objects.filter(recipient=self.member).exists()
        )

    def test_digest_without_smtp_alerts_owner(self):
        self.company.expiry_team_digest_enabled = True
        self.company.save(update_fields=["expiry_team_digest_enabled"])
        send_team_expiry_digest(self.company)
        note = TeamNotification.objects.get(recipient=self.owner)
        self.assertEqual(note.kind, TeamNotification.Kind.EMAIL_FAILED)
        self.assertIn("digest", note.title.lower())

    def test_digest_send_failure_alerts_owner(self):
        self.company.expiry_team_digest_enabled = True
        self.company.outgoing_email = "sales@example.com"
        self.company.outgoing_email_app_password = "secret"
        self.company.save()
        client = Client.objects.create(company=self.company, name="OMSGuru")
        product = Product.objects.create(company=self.company, name="Microsoft 365")
        ClientSubscription.objects.create(
            company=self.company,
            client=client,
            product=product,
            status=ClientSubscription.Status.ACTIVE,
            start_date=timezone.localdate() - timedelta(days=300),
            end_date=timezone.localdate() + timedelta(days=5),
            quantity=1,
            billing_amount=Decimal("100.00"),
        )
        with patch(
            "business.expiry_notifications.send_company_outbound",
            return_value=(False, "SMTP auth failed"),
        ):
            send_team_expiry_digest(self.company)
        note = TeamNotification.objects.get(recipient=self.owner)
        self.assertEqual(note.title, "Daily expiry digest failed")
        self.assertEqual(note.body, "SMTP auth failed")

    def test_open_rejects_external_target(self):
        note = TeamNotification.objects.create(
            company=self.company,
            recipient=self.owner,
            kind=TeamNotification.Kind.NEW_LEAD,
            title="Bad link",
            target_url="https://example.com",
            dedupe_key="bad-link",
        )
        self.client.force_login(self.owner)
        response = self.client.get(reverse("notification_open", args=[note.pk]))
        self.assertRedirects(
            response,
            reverse("notification_list"),
            fetch_redirect_response=False,
        )


class SupportDigestTests(TestCase):
    def setUp(self):
        from business import helpdesk_bridge

        User = get_user_model()
        self.owner = User.objects.create_user("owner@example.com", "pass")
        self.support = User.objects.create_user("support@example.com", "pass")
        self.company = CompanyProfile.objects.create(
            user=self.owner,
            company_name="Co",
            expiry_team_digest_enabled=True,
            outgoing_email="sales@example.com",
            outgoing_email_app_password="secret",
        )
        CompanyMember.objects.create(
            company=self.company,
            user=self.support,
            support_ticket_access=True,
        )
        queue = helpdesk_bridge.get_company_queue(self.company)
        for title, status in (
            ("Mail not working", Ticket.OPEN_STATUS),
            ("Teams issue", Ticket.REOPENED_STATUS),
            ("Fixed laptop", Ticket.RESOLVED_STATUS),
            ("Old ticket", Ticket.CLOSED_STATUS),
        ):
            Ticket.objects.create(
                queue=queue,
                title=title,
                status=status,
                submitter_email="client@example.com",
            )

    def test_digest_lists_pending_tickets_with_excel_once_per_day(self):
        from business.support_digest import send_support_ticket_digest

        with patch(
            "business.support_digest.send_company_outbound",
            return_value=(True, ""),
        ) as send:
            sent, _logs = send_support_ticket_digest(self.company)
            again, _ = send_support_ticket_digest(self.company)
        self.assertEqual((sent, again), (1, 0))
        send.assert_called_once()
        _company, recipients, subject, body = send.call_args.args[:4]
        html_body = send.call_args.kwargs["html_body"]
        self.assertIn("support@example.com", recipients)
        self.assertIn("Pending support tickets (2)", subject)
        for text in (body, html_body):
            self.assertIn("Mail not working", text)
            self.assertIn("Teams issue", text)
            self.assertNotIn("Fixed laptop", text)
            self.assertNotIn("Old ticket", text)
        self.assertNotIn("SLA", html_body)
        name, data, _mime = send.call_args.kwargs["attachments"][0]
        self.assertTrue(name.endswith(".xlsx"))
        self.assertTrue(data.startswith(b"PK"))
        self.company.refresh_from_db()
        self.assertEqual(self.company.support_digest_last_sent, timezone.localdate())


class TicketCreatedConfirmationTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_user("owner-confirm@example.com", "pass")
        self.company = CompanyProfile.objects.create(
            user=self.owner,
            company_name="Confirm Co",
            support_email="support@confirm.example",
            support_email_app_password="app-secret",
        )
        self.queue = Queue.objects.create(
            title="Confirm Co",
            slug=f"c{self.company.pk}-confirm",
        )

    def _ticket(self, sender="customer@example.com"):
        return Ticket.objects.create(
            title="Printer offline",
            queue=self.queue,
            description="It will not print",
            submitter_email=sender,
        )

    def test_email_import_queues_confirmation_to_sender(self):
        from business.helpdesk_signals import email_sender_ticket_confirmation

        ticket = self._ticket()
        with patch(
            "business.helpdesk_assign_mail.queue_ticket_created_email"
        ) as queued:
            email_sender_ticket_confirmation(sender="helpdesk_inbox", ticket=ticket)
        queued.assert_called_once()
        self.assertEqual(queued.call_args.kwargs["company"], self.company)
        self.assertEqual(queued.call_args.kwargs["ticket"], ticket)

    def test_manual_ticket_form_does_not_queue_confirmation(self):
        from business.helpdesk_signals import email_sender_ticket_confirmation

        ticket = self._ticket()
        with patch(
            "business.helpdesk_assign_mail.queue_ticket_created_email"
        ) as queued:
            email_sender_ticket_confirmation(sender="TicketForm", ticket=ticket)
        queued.assert_not_called()

    def test_confirmation_is_addressed_to_the_sender(self):
        from business.helpdesk_assign_mail import send_ticket_created_email

        ticket = self._ticket()
        with patch(
            "business.helpdesk_assign_mail.send_outbound",
            return_value=(True, None),
        ) as send:
            status, err = send_ticket_created_email(
                company=self.company, ticket=ticket
            )
        self.assertEqual((status, err), ("sent", None))
        self.assertEqual(send.call_args.kwargs["to_addresses"], ["customer@example.com"])
        self.assertIn("We have generated a ticket", send.call_args.kwargs["subject"])
        self.assertIn(f"TK-{ticket.pk:05d}", send.call_args.kwargs["body"])

    def test_support_mailbox_is_not_sent_a_confirmation(self):
        from business.helpdesk_assign_mail import send_ticket_created_email

        ticket = self._ticket(sender="support@confirm.example")
        with patch("business.helpdesk_assign_mail.send_outbound") as send:
            status, err = send_ticket_created_email(
                company=self.company, ticket=ticket
            )
        self.assertEqual((status, err), ("skipped_self", None))
        send.assert_not_called()
