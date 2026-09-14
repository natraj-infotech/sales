from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import CompanyProfile
from business.models import (
    CaseLockInvite,
    CaseLockProduct,
    CaseLockSubmission,
    Client,
    ClientSubscription,
    Product,
    Vendor,
    SubscriptionRenewalInvite,
    SubscriptionRenewalInviteLine,
    SubscriptionRenewalInviteNewProduct,
    SubscriptionRenewalInviteRecipient,
    annotate_internal_case_locks,
)

from business.expiry_notifications import (
    build_renewal_quote_items,
    send_renewal_confirmation_email,
    send_renewal_quotation_email,
)
from business.reminder_schedule import (
    MODE_DAILY,
    _phase,
    normalize_schedule,
    recommended_schedule,
)
from business.renewal_tokens import (
    sign_renewal_confirm_token,
    unsign_renewal_confirm_token,
)
from business.analytics import (
    empty_ticket_view,
    format_duration,
    sales_dashboard_charts,
    ticket_charts_from_views,
)
from business.subscription_import import import_subscriptions_from_file
from business.views import (
    _renewal_invite_queryset,
    _renewal_product_groups,
)


def _product(name="Antivirus", price="1200"):
    return SimpleNamespace(name=name, unit_price=Decimal(price))


def _line(product, *, qty, name=None, company="Client Co"):
    return SimpleNamespace(
        company_name=company,
        product_name=name or product.name,
        renewal_count=qty,
        subscription=SimpleNamespace(product=product),
    )


def _new_product(product, *, qty, name=None, company="Client Co"):
    return SimpleNamespace(
        company_name=company,
        product_name=name or product.name,
        quantity=qty,
        product=product,
    )


class RenewalQuoteItemsTests(SimpleTestCase):
    def test_includes_qty_and_price_and_computes_amount(self):
        product = _product()
        items = build_renewal_quote_items([_line(product, qty=10)])
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["product_name"], "Antivirus")
        self.assertEqual(items[0]["quantity"], 10)
        self.assertEqual(items[0]["unit_price"], Decimal("1200.00"))
        self.assertEqual(items[0]["amount"], Decimal("12000.00"))

    def test_skips_zero_quantity(self):
        items = build_renewal_quote_items([_line(_product(), qty=0)])
        self.assertEqual(items, [])

    def test_skips_zero_price(self):
        items = build_renewal_quote_items(
            [_line(_product(price="0"), qty=5)]
        )
        self.assertEqual(items, [])

    def test_skips_missing_price_product(self):
        line = SimpleNamespace(
            company_name="Co",
            product_name="Gone",
            renewal_count=2,
            subscription=SimpleNamespace(product=None),
        )
        self.assertEqual(build_renewal_quote_items([line]), [])

    def test_includes_new_products_with_price(self):
        vpn = _product("VPN", "500")
        items = build_renewal_quote_items(
            [],
            new_products=[_new_product(vpn, qty=3)],
        )
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["amount"], Decimal("1500.00"))

    def test_mixes_renewal_and_new_and_skips_unpriced(self):
        priced = _product("Antivirus", "1200")
        unpriced = _product("Firewall", "0")
        vpn = _product("VPN", "500")
        items = build_renewal_quote_items(
            [
                _line(priced, qty=10),
                _line(_product("Backup", "800"), qty=0),
                _line(unpriced, qty=2),
            ],
            new_products=[_new_product(vpn, qty=3)],
        )
        names = [item["product_name"] for item in items]
        self.assertEqual(names, ["Antivirus", "VPN"])
        self.assertEqual(sum((item["amount"] for item in items), Decimal("0")), Decimal("13500.00"))


class SendRenewalConfirmationEmailTests(SimpleTestCase):
    def _invite(self, **overrides):
        data = {
            "pk": "invite-1",
            "company": SimpleNamespace(
                pk=1,
                company_name="Co",
                outgoing_email="sales@example.com",
                outgoing_email_app_password="secret",
                company_logo=None,
                regards_contact="",
                regards_email="",
            ),
            "client_email": "one@example.com",
            "submitted_by_email": "two@example.com",
        }
        data.update(overrides)
        return SimpleNamespace(**data)

    def test_sends_only_to_submitter_without_cc(self):
        invite = self._invite()
        with patch(
            "business.expiry_notifications.send_renewal_email"
        ) as send_mail:
            send_mail.return_value = (True, "", None)
            sent = send_renewal_confirmation_email(
                invite, lines=[], new_products=[]
            )
        self.assertTrue(sent)
        send_mail.assert_called_once()
        args, kwargs = send_mail.call_args
        self.assertEqual(args[2], ["two@example.com"])
        self.assertFalse(kwargs.get("cc_addresses"))

    def test_skips_when_submitter_missing(self):
        invite = self._invite(submitted_by_email="")
        with patch(
            "business.expiry_notifications.send_renewal_email"
        ) as send_mail:
            sent = send_renewal_confirmation_email(
                invite, lines=[], new_products=[]
            )
        self.assertFalse(sent)
        send_mail.assert_not_called()


class SendRenewalQuotationEmailTests(SimpleTestCase):
    def test_skips_when_no_priced_lines(self):
        invite = SimpleNamespace(
            pk="invite-1",
            company=SimpleNamespace(
                outgoing_email="sales@example.com",
                outgoing_email_app_password="secret",
            ),
            client_email="client@example.com",
        )
        with patch(
            "business.expiry_notifications.send_renewal_email"
        ) as send_mail:
            sent = send_renewal_quotation_email(
                invite,
                lines=[_line(_product(price="0"), qty=4)],
                new_products=[],
            )
        self.assertFalse(sent)
        send_mail.assert_not_called()


class RenewalConfirmTokenTests(SimpleTestCase):
    def test_round_trip_normalizes_email(self):
        token = sign_renewal_confirm_token("abc-id", "  Client@Example.COM ")
        parsed = unsign_renewal_confirm_token(token)
        self.assertEqual(parsed, ("abc-id", "client@example.com"))

    def test_rejects_tampered_token(self):
        token = sign_renewal_confirm_token("abc-id", "client@example.com")
        self.assertIsNone(unsign_renewal_confirm_token(token + "x"))

    def test_rejects_empty_token(self):
        self.assertIsNone(unsign_renewal_confirm_token(""))
        self.assertIsNone(unsign_renewal_confirm_token("not-a-token"))


class RenewalConfirmSignedLinkTests(TestCase):
    def setUp(self):
        user = get_user_model().objects.create_user("owner@example.com", "pass")
        company = CompanyProfile.objects.create(user=user, company_name="Co")
        self.invite = SubscriptionRenewalInvite.objects.create(
            company=company,
            client_email="one@example.com",
        )
        now = timezone.now()
        SubscriptionRenewalInviteRecipient.objects.create(
            invite=self.invite, email="one@example.com", sent_at=now
        )
        SubscriptionRenewalInviteRecipient.objects.create(
            invite=self.invite, email="two@example.com", sent_at=now
        )

    def _url(self, email=None):
        if email is None:
            return reverse(
                "renewal_confirm_public_legacy",
                kwargs={"invite_id": self.invite.pk},
            )
        token = sign_renewal_confirm_token(self.invite.pk, email)
        return reverse(
            "renewal_confirm_public",
            kwargs={"invite_id": str(self.invite.pk), "token": token},
        )

    def test_missing_token_rejected_when_recipients_exist(self):
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 403)
        self.assertContains(response, "This link is not valid", status_code=403)

    def test_valid_tokens_open_form(self):
        first = self.client.get(self._url("one@example.com"))
        self.assertEqual(first.status_code, 200)
        self.assertContains(first, "Confirm subscription renewal")
        second = self.client.get(self._url("two@example.com"))
        self.assertEqual(second.status_code, 200)
        self.assertContains(second, "Confirm subscription renewal")
        self.invite.recipients.get(email="one@example.com").refresh_from_db()
        self.assertIsNotNone(
            self.invite.recipients.get(email="one@example.com").opened_at
        )

    def test_unsent_email_token_rejected(self):
        response = self.client.get(self._url("three@example.com"))
        self.assertEqual(response.status_code, 403)

    def test_second_link_locked_after_submit(self):
        self.invite.submitted_at = timezone.now()
        self.invite.submitted_by_email = "one@example.com"
        self.invite.save(update_fields=["submitted_at", "submitted_by_email"])
        response = self.client.get(self._url("two@example.com"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Already submitted")
        self.assertContains(response, "one@example.com")


class ClientRenewalTabRowsTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            "owner@example.com", "pass"
        )
        self.company = CompanyProfile.objects.create(
            user=self.user, company_name="Co"
        )
        client = Client.objects.create(
            company=self.company, name="Acme", email="acme@example.com"
        )
        existing_product = Product.objects.create(
            company=self.company, name="Antivirus"
        )
        new_product = Product.objects.create(company=self.company, name="VPN")
        subscription = ClientSubscription.objects.create(
            company=self.company,
            client=client,
            product=existing_product,
            start_date=date.today(),
            end_date=date.today() + timedelta(days=30),
            quantity=5,
        )
        now = timezone.now()
        self.invite = SubscriptionRenewalInvite.objects.create(
            company=self.company,
            client_email="acme@example.com",
            email_sent_at=now,
            submitted_at=now,
            submitted_by_email="acme@example.com",
        )
        SubscriptionRenewalInviteLine.objects.create(
            invite=self.invite,
            subscription=subscription,
            company_name="Acme",
            product_name="Antivirus",
            current_quantity=5,
            renewal_count=8,
        )
        SubscriptionRenewalInviteNewProduct.objects.create(
            invite=self.invite,
            product=new_product,
            company_name="Acme",
            product_name="VPN",
            quantity=3,
        )
        pending = SubscriptionRenewalInvite.objects.create(
            company=self.company,
            client_email="pending@example.com",
            email_sent_at=now,
        )
        SubscriptionRenewalInviteLine.objects.create(
            invite=pending,
            subscription=subscription,
            company_name="Acme",
            product_name="Antivirus",
            current_quantity=5,
        )

    def _invites(self):
        return list(_renewal_invite_queryset(self.company))

    def _rows(self):
        groups = _renewal_product_groups(self._invites())
        return [row for group in groups for row in group["rows"]]

    def test_groups_include_new_and_existing_products(self):
        rows = {row["product_name"]: row for row in self._rows()}
        self.assertEqual(rows["VPN"]["client_email"], "acme@example.com")
        self.assertTrue(rows["VPN"]["is_new"])
        self.assertEqual(rows["VPN"]["renewal_quantity"], 3)
        self.assertIsNone(rows["VPN"]["previous_quantity"])
        self.assertEqual(rows["Antivirus"]["previous_quantity"], 5)
        self.assertEqual(rows["Antivirus"]["renewal_quantity"], 8)
        self.assertFalse(rows["Antivirus"]["is_new"])

    def test_list_page_uses_single_grouped_table(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("client_renewal_list"))
        self.assertEqual(response.status_code, 200)
        groups = response.context["renewal_groups"]
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["company_name"], "Acme")
        self.assertEqual(len(groups[0]["rows"]), 3)
        names = [row["product_name"] for row in groups[0]["rows"]]
        self.assertEqual(names.count("VPN"), 1)
        self.assertEqual(names.count("Antivirus"), 2)
        self.assertContains(response, "Company")
        self.assertContains(response, "Prev qty")
        self.assertContains(response, "Renewal qty")
        self.assertContains(response, "VPN")
        self.assertContains(response, "pending@example.com")
        self.assertContains(response, "Connect Zoho")
        self.assertContains(response, ">Win</button>")
        self.assertContains(response, ">Lost</button>")
        self.assertNotContains(response, "cr-deal-pill cr-status--pending")
        self.assertContains(response, '<td class="cr-company-cell">Acme</td>', html=False)
        self.assertContains(response, 'class="cr-products"')
        self.assertNotContains(response, "New subscriptions")
        self.assertNotContains(response, "Renewal requests")
        self.assertNotContains(response, "All emails")
        self.assertNotContains(response, "Set status")
        self.assertNotContains(response, "Average time to close")

    def test_live_json_includes_grouped_rows(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("client_renewal_live"))
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(len(data["groups"]), 1)
        rows = {row["product_name"]: row for row in data["groups"][0]["rows"]}
        self.assertEqual(rows["VPN"]["renewal_quantity"], 3)
        self.assertTrue(rows["VPN"]["is_new"])
        self.assertEqual(rows["Antivirus"]["previous_quantity"], 5)
        self.assertEqual(rows["Antivirus"]["renewal_quantity"], 8)
        self.assertTrue(rows["Antivirus"]["subscription_id"])
        self.assertIn("sent", rows["Antivirus"]["timestamps"])

    def test_zoho_payload_includes_open_quote_url(self):
        self.invite.zoho_quote_url = "https://crm.zoho.com/crm/quote/1"
        self.invite.quotation_sent_at = timezone.now()
        self.invite.save(update_fields=["zoho_quote_url", "quotation_sent_at"])
        groups = _renewal_product_groups(
            self._invites(), zoho_connected=True, zoho_configured=True
        )
        rows = [row for group in groups for row in group["rows"]]
        antivirus = next(
            row
            for row in rows
            if row["product_name"] == "Antivirus"
            and row["client_email"] == "acme@example.com"
        )
        self.assertEqual(antivirus["zoho"]["kind"], "send")
        self.assertEqual(
            antivirus["zoho"]["quote_url"], "https://crm.zoho.com/crm/quote/1"
        )
        vpn = next(row for row in rows if row["product_name"] == "VPN")
        self.assertEqual(vpn["zoho"]["quote_url"], "https://crm.zoho.com/crm/quote/1")


class ReminderScheduleOverlapTests(SimpleTestCase):
    def test_recommended_schedule_has_no_overlap(self):
        normalize_schedule(recommended_schedule())

    def test_overlapping_before_expiry_windows_are_rejected(self):
        raw = [
            _phase("Early window", 90, 1, MODE_DAILY, ["09:00"]),
            _phase("Final days", 7, 1, MODE_DAILY, ["09:00"]),
        ]
        with self.assertRaises(ValidationError) as ctx:
            normalize_schedule(raw)
        msg = " ".join(ctx.exception.messages)
        self.assertIn("overlap", msg.lower())
        self.assertIn("days 7–1 left", msg)
        self.assertIn("90 to 8 days left", msg)

    def test_adjacent_before_expiry_windows_are_allowed(self):
        raw = [
            _phase("Early window", 90, 8, MODE_DAILY, ["09:00"]),
            _phase("Final days", 7, 1, MODE_DAILY, ["09:00"]),
        ]
        normalize_schedule(raw)

    def test_three_overlapping_before_windows_are_rejected(self):
        raw = [
            _phase("Early window", 90, 1, MODE_DAILY, ["09:00"]),
            _phase("Mid window", 30, 1, MODE_DAILY, ["09:00"]),
            _phase("Final days", 7, 1, MODE_DAILY, ["09:00"]),
        ]
        with self.assertRaises(ValidationError) as ctx:
            normalize_schedule(raw)
        self.assertGreaterEqual(len(ctx.exception.messages), 2)

    def test_overdue_overlap_is_rejected(self):
        raw = [
            _phase("Overdue", -1, -7, MODE_DAILY, ["09:00"]),
            _phase("Late push", -4, -10, MODE_DAILY, ["09:00"]),
        ]
        with self.assertRaises(ValidationError) as ctx:
            normalize_schedule(raw)
        msg = " ".join(ctx.exception.messages)
        self.assertIn("overlap", msg.lower())
        self.assertIn("days 4–7 late", msg)
        self.assertIn("1 to 3 days late", msg)

    def test_before_and_overdue_windows_do_not_conflict(self):
        raw = [
            _phase("Early window", 90, 1, MODE_DAILY, ["09:00"]),
            _phase("Overdue", -1, -7, MODE_DAILY, ["09:00"]),
        ]
        normalize_schedule(raw)

    def test_two_expiry_day_windows_are_rejected(self):
        raw = [
            _phase("Expiry day", 0, 0, MODE_DAILY, ["09:00"]),
            _phase("Expiry again", 0, 0, MODE_DAILY, ["09:00"]),
        ]
        with self.assertRaises(ValidationError) as ctx:
            normalize_schedule(raw)
        self.assertIn("expiry day", " ".join(ctx.exception.messages).lower())


class CaseLockFeatureTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(
            email="owner@example.com", password="pass12345"
        )
        self.company = CompanyProfile.objects.create(
            user=self.user,
            company_name="Acme",
            caselock_email="caselock@example.com",
            outgoing_email="outgoing@example.com",
            outgoing_email_app_password="out-secret",
        )
        self.client.login(email="owner@example.com", password="pass12345")
        mailbox_patch = patch(
            "business.views.maybe_fetch_on_dashboard",
            return_value={"created": 0, "logs": []},
        )
        mailbox_patch.start()
        self.addCleanup(mailbox_patch.stop)

    def test_company_setup_shows_caselock_email(self):
        response = self.client.get(reverse("company_setup"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Case lock email")
        self.assertContains(response, "caselock_email")

    def test_dashboard_has_tabs(self):
        response = self.client.get(reverse("caselock_inbox_list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Dashboard")
        self.assertContains(response, "Incoming mails")
        self.assertContains(response, "caselock@example.com")
        self.assertContains(response, "subnav-tab")

    def test_invite_and_decision_emails_use_html_layout(self):
        from business.caselock_inbox import (
            _send_invite_email,
            send_caselock_decision_email,
        )

        invite = CaseLockInvite.objects.create(
            company=self.company,
            source_message_id="<html-invite@test>",
            sender_email="buyer@example.com",
        )
        with patch(
            "business.caselock_inbox.send_outbound", return_value=(True, None)
        ) as send:
            _send_invite_email(self.company, invite, "buyer@example.com")
        self.assertEqual(send.call_args.args[0], "outgoing@example.com")
        self.assertEqual(send.call_args.args[2], ["buyer@example.com"])
        html = send.call_args.kwargs["html_body"]
        self.assertIn("SUBMIT CASE LOCK DETAILS", html)
        self.assertIn("/caselock/fill/", html)
        self.assertIn("href=", html)

        product = CaseLockProduct.objects.create(company=self.company, name="Backup")
        sub = CaseLockSubmission.objects.create(
            invite=invite,
            client_name="Buyer",
            client_gst="24AAFCN2771H2ZH",
            product=product,
            product_name="Backup",
            product_quantity=2,
            status=CaseLockSubmission.Status.APPROVED,
        )
        with patch(
            "business.caselock_inbox.send_outbound", return_value=(True, None)
        ) as send:
            self.assertTrue(send_caselock_decision_email(sub))
        html = send.call_args.kwargs["html_body"]
        self.assertIn("finalized", html.lower())
        self.assertIn("24AAFCN2771H2ZH", html)
        self.assertIn("Buyer", html)

        sub.status = CaseLockSubmission.Status.DECLINED
        sub.save(update_fields=["status"])
        with patch(
            "business.caselock_inbox.send_outbound", return_value=(True, None)
        ) as send:
            self.assertTrue(send_caselock_decision_email(sub))
        html = send.call_args.kwargs["html_body"]
        self.assertIn("declined", html.lower())
        self.assertIn("Backup", html)

    def test_incoming_mails_tab(self):
        invite = CaseLockInvite.objects.create(
            company=self.company,
            source_message_id="<one@test>",
            sender_email="buyer@example.com",
            mail_subject="Need lock",
            received_at=timezone.now(),
        )
        response = self.client.get(reverse("caselock_inbox_list") + "?tab=mails")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "buyer@example.com")
        self.assertContains(response, "Need lock")
        self.assertContains(response, invite.sender_email)

    def test_product_add_and_public_form(self):
        add = self.client.post(
            reverse("caselock_product_add"),
            {"name": "Firewall"},
            follow=True,
        )
        self.assertEqual(add.status_code, 200)
        product = CaseLockProduct.objects.get(company=self.company, name="Firewall")
        invite = CaseLockInvite.objects.create(
            company=self.company,
            source_message_id="<two@test>",
            sender_email="buyer@example.com",
        )
        url = reverse("caselock_public_form", kwargs={"invite_id": invite.pk})
        get = self.client.get(url)
        self.assertEqual(get.status_code, 200)
        self.assertContains(get, "Search products")
        self.assertContains(get, "Firewall")
        self.assertContains(get, "cl-product-trigger")
        post = self.client.post(
            url,
            {
                "product": product.pk,
                "client_name": "Buyer Co",
                "client_gst": "22AAAAA0000A1Z5",
                "product_quantity": 3,
            },
        )
        self.assertEqual(post.status_code, 200)
        sub = CaseLockSubmission.objects.get(invite=invite)
        self.assertEqual(sub.client_name, "Buyer Co")
        self.assertEqual(sub.product_name, "Firewall")
        self.assertEqual(sub.product_quantity, 3)

        dash = self.client.get(reverse("caselock_inbox_list"))
        self.assertContains(dash, "Buyer Co")
        self.assertContains(dash, "Case lock")

    def test_public_form_requires_all_lock_fields(self):
        product = CaseLockProduct.objects.create(company=self.company, name="Laptop")
        invite = CaseLockInvite.objects.create(
            company=self.company,
            source_message_id="<req@test>",
            sender_email="buyer@example.com",
        )
        url = reverse("caselock_public_form", kwargs={"invite_id": invite.pk})
        complete = {
            "product": product.pk,
            "client_name": "Acme Corp",
            "client_gst": "22AAAAA0000A1Z5",
            "product_quantity": 10,
        }
        missing_cases = [
            {**complete, "product": ""},
            {**complete, "client_name": "  "},
            {**complete, "client_gst": ""},
            {**complete, "product_quantity": ""},
            {**complete, "product_quantity": 0},
        ]
        for payload in missing_cases:
            post = self.client.post(url, payload)
            self.assertEqual(post.status_code, 200)
            self.assertFalse(CaseLockSubmission.objects.filter(invite=invite).exists())

    def test_internal_lock_goes_to_first_submitter(self):
        product = CaseLockProduct.objects.create(company=self.company, name="Antivirus")
        first_invite = CaseLockInvite.objects.create(
            company=self.company,
            source_message_id="<a@test>",
            sender_email="first@example.com",
        )
        second_invite = CaseLockInvite.objects.create(
            company=self.company,
            source_message_id="<b@test>",
            sender_email="second@example.com",
        )
        first = CaseLockSubmission.objects.create(
            invite=first_invite,
            client_name="Acme Corp",
            client_gst="22AAAAA0000A1Z5",
            product=product,
            product_name="Antivirus",
            product_quantity=5,
        )
        second = CaseLockSubmission.objects.create(
            invite=second_invite,
            client_name="Acme Pvt Ltd",
            client_gst="22aaaaa0000a1z5",
            product=product,
            product_name="Antivirus",
            product_quantity=5,
        )
        annotate_internal_case_locks([first, second])
        self.assertTrue(first.has_internal_case_lock)
        self.assertFalse(second.has_internal_case_lock)

    def test_internal_lock_groups_by_gst_not_company_name(self):
        product = CaseLockProduct.objects.create(company=self.company, name="Firewall")
        invite_a = CaseLockInvite.objects.create(
            company=self.company,
            source_message_id="<lock-a@test>",
            sender_email="a@example.com",
        )
        invite_b = CaseLockInvite.objects.create(
            company=self.company,
            source_message_id="<lock-b@test>",
            sender_email="b@example.com",
        )
        invite_c = CaseLockInvite.objects.create(
            company=self.company,
            source_message_id="<lock-c@test>",
            sender_email="c@example.com",
        )
        same_product_qty = dict(
            product=product,
            product_name="Firewall",
            product_quantity=2,
        )
        by_company = CaseLockSubmission.objects.create(
            invite=invite_a,
            client_name="Acme Corp",
            client_gst="22AAAAA0000A1Z5",
            **same_product_qty,
        )
        other_company = CaseLockSubmission.objects.create(
            invite=invite_b,
            client_name="Beta Ltd",
            client_gst="22AAAAA0000A1Z5",
            **same_product_qty,
        )
        other_gst = CaseLockSubmission.objects.create(
            invite=invite_c,
            client_name="Acme Corp",
            client_gst="27BBBBB0000B1Z5",
            **same_product_qty,
        )
        annotate_internal_case_locks([by_company, other_company, other_gst])
        self.assertTrue(by_company.has_internal_case_lock)
        self.assertFalse(other_company.has_internal_case_lock)
        self.assertTrue(other_gst.has_internal_case_lock)

    def test_internal_lock_without_gst_uses_company_name(self):
        product = CaseLockProduct.objects.create(company=self.company, name="Router")
        invite_a = CaseLockInvite.objects.create(
            company=self.company,
            source_message_id="<nogst-a@test>",
            sender_email="a@example.com",
        )
        invite_b = CaseLockInvite.objects.create(
            company=self.company,
            source_message_id="<nogst-b@test>",
            sender_email="b@example.com",
        )
        first = CaseLockSubmission.objects.create(
            invite=invite_a,
            client_name="Acme Corp",
            client_gst="",
            product=product,
            product_name="Router",
            product_quantity=1,
        )
        second = CaseLockSubmission.objects.create(
            invite=invite_b,
            client_name="acme corp",
            client_gst="",
            product=product,
            product_name="Router",
            product_quantity=1,
        )
        annotate_internal_case_locks([first, second])
        self.assertTrue(first.has_internal_case_lock)
        self.assertFalse(second.has_internal_case_lock)

    def test_final_unblocks_same_combination_for_new_request(self):
        product = CaseLockProduct.objects.create(company=self.company, name="VPN")
        now = timezone.now()
        fields = dict(
            client_name="Acme Corp",
            client_gst="22AAAAA0000A1Z5",
            product=product,
            product_name="VPN",
            product_quantity=4,
        )
        first_invite = CaseLockInvite.objects.create(
            company=self.company,
            source_message_id="<final-a@test>",
            sender_email="first@example.com",
        )
        waiting_invite = CaseLockInvite.objects.create(
            company=self.company,
            source_message_id="<final-b@test>",
            sender_email="waiting@example.com",
        )
        new_invite = CaseLockInvite.objects.create(
            company=self.company,
            source_message_id="<final-c@test>",
            sender_email="new@example.com",
        )
        first = CaseLockSubmission.objects.create(invite=first_invite, **fields)
        waiting = CaseLockSubmission.objects.create(invite=waiting_invite, **fields)
        CaseLockSubmission.objects.filter(pk=first.pk).update(
            timestamp=now - timedelta(hours=2)
        )
        CaseLockSubmission.objects.filter(pk=waiting.pk).update(
            timestamp=now - timedelta(hours=1)
        )
        first.refresh_from_db()
        waiting.refresh_from_db()

        first.status = CaseLockSubmission.Status.APPROVED
        first.status_changed_at = now - timedelta(minutes=10)
        first.save(update_fields=["status", "status_changed_at"])

        new_sub = CaseLockSubmission.objects.create(invite=new_invite, **fields)
        CaseLockSubmission.objects.filter(pk=new_sub.pk).update(
            timestamp=now - timedelta(minutes=1)
        )
        new_sub.refresh_from_db()

        annotate_internal_case_locks([first, waiting, new_sub])
        self.assertFalse(first.has_internal_case_lock)
        self.assertFalse(waiting.has_internal_case_lock)
        self.assertTrue(new_sub.has_internal_case_lock)

    def test_decline_does_not_unblock_combination(self):
        product = CaseLockProduct.objects.create(company=self.company, name="Mail")
        now = timezone.now()
        fields = dict(
            client_name="Acme Corp",
            client_gst="22AAAAA0000A1Z5",
            product=product,
            product_name="Mail",
            product_quantity=1,
        )
        first_invite = CaseLockInvite.objects.create(
            company=self.company,
            source_message_id="<decl-a@test>",
            sender_email="first@example.com",
        )
        later_invite = CaseLockInvite.objects.create(
            company=self.company,
            source_message_id="<decl-b@test>",
            sender_email="later@example.com",
        )
        first = CaseLockSubmission.objects.create(invite=first_invite, **fields)
        later = CaseLockSubmission.objects.create(invite=later_invite, **fields)
        CaseLockSubmission.objects.filter(pk=first.pk).update(
            timestamp=now - timedelta(hours=2)
        )
        CaseLockSubmission.objects.filter(pk=later.pk).update(
            timestamp=now - timedelta(minutes=1)
        )
        first.refresh_from_db()
        later.refresh_from_db()
        first.status = CaseLockSubmission.Status.DECLINED
        first.status_changed_at = now - timedelta(minutes=30)
        first.save(update_fields=["status", "status_changed_at"])

        annotate_internal_case_locks([first, later])
        self.assertTrue(first.has_internal_case_lock)
        self.assertFalse(later.has_internal_case_lock)

    def test_final_and_decline_send_mail(self):
        product = CaseLockProduct.objects.create(company=self.company, name="Backup")
        invite = CaseLockInvite.objects.create(
            company=self.company,
            source_message_id="<c@test>",
            sender_email="buyer@example.com",
        )
        sub = CaseLockSubmission.objects.create(
            invite=invite,
            client_name="Buyer",
            product=product,
            product_name="Backup",
            product_quantity=1,
        )
        with patch("business.views.send_caselock_decision_email", return_value=True) as send:
            response = self.client.post(
                reverse("caselock_submission_status_update", args=[sub.pk]),
                {"action": "final"},
            )
            self.assertEqual(response.status_code, 302)
            send.assert_called_once()
        sub.refresh_from_db()
        self.assertEqual(sub.status, CaseLockSubmission.Status.APPROVED)
        self.assertIsNotNone(sub.status_changed_at)

        invite2 = CaseLockInvite.objects.create(
            company=self.company,
            source_message_id="<d@test>",
            sender_email="buyer2@example.com",
        )
        sub2 = CaseLockSubmission.objects.create(
            invite=invite2,
            client_name="Other",
            product=product,
            product_name="Backup",
            product_quantity=2,
        )
        with patch("business.views.send_caselock_decision_email", return_value=True) as send:
            response = self.client.post(
                reverse("caselock_submission_status_update", args=[sub2.pk]),
                {"action": "decline"},
            )
            self.assertEqual(response.status_code, 302)
            send.assert_called_once()
        sub2.refresh_from_db()
        self.assertEqual(sub2.status, CaseLockSubmission.Status.DECLINED)

    def test_live_json_returns_incoming_mails(self):
        invite = CaseLockInvite.objects.create(
            company=self.company,
            source_message_id="<live@test>",
            sender_email="buyer@example.com",
            mail_subject="Need lock",
            received_at=timezone.now(),
        )
        response = self.client.get(reverse("caselock_inbox_live"))
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["ok"])
        self.assertEqual(len(data["mail_rows"]), 1)
        self.assertEqual(data["mail_rows"][0]["from"], "buyer@example.com")
        self.assertEqual(data["mail_rows"][0]["subject"], "Need lock")
        self.assertEqual(data["mail_rows"][0]["id"], str(invite.pk))
        self.assertFalse(data["mail_rows"][0]["form_submitted"])

    def test_incoming_mails_page_has_live_script(self):
        response = self.client.get(reverse("caselock_inbox_list") + "?tab=mails")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse("caselock_inbox_live"))
        self.assertContains(response, "cl-mails-tbody")
        self.assertContains(response, "cl-live-meta")

    @override_settings(DEBUG=False)
    def test_production_live_does_not_poll_mailbox(self):
        with patch("business.views.maybe_fetch_on_dashboard") as fetch:
            response = self.client.get(reverse("caselock_inbox_live"))
        self.assertEqual(response.status_code, 200)
        fetch.assert_not_called()
        self.assertFalse(response.json()["mailbox_poll_on_request"])

    @override_settings(DEBUG=True)
    def test_local_live_polls_mailbox(self):
        with patch(
            "business.views.maybe_fetch_on_dashboard",
            return_value={"created": 0, "logs": []},
        ) as fetch:
            response = self.client.get(reverse("caselock_inbox_live"))
        self.assertEqual(response.status_code, 200)
        fetch.assert_called_once()
        self.assertTrue(response.json()["mailbox_poll_on_request"])

    def test_skips_exchange_bounce_inbound(self):
        from business.caselock_inbox import (
            _create_invite_and_notify,
            _should_skip_inbound,
        )

        bounce = (
            "MicrosoftExchange329e71ec88ae4615bbc36ab6ce41109e@natrajinfo.in"
        )
        self.assertTrue(
            _should_skip_inbound(
                self.company, bounce, "Undeliverable: Case lock details"
            )
        )
        self.assertTrue(
            _should_skip_inbound(
                self.company, "caselock@example.com", "Need lock"
            )
        )
        self.assertTrue(
            _should_skip_inbound(
                self.company, "outgoing@example.com", "Need lock"
            )
        )
        self.assertFalse(
            _should_skip_inbound(self.company, "buyer@example.com", "Need lock")
        )
        created = _create_invite_and_notify(
            self.company,
            "<bounce@test>",
            bounce,
            "Undeliverable: Case lock details",
        )
        self.assertFalse(created)
        self.assertFalse(
            CaseLockInvite.objects.filter(source_message_id="<bounce@test>").exists()
        )

    def test_invite_send_uses_outgoing_not_caselock_mailbox(self):
        from business.caselock_inbox import _create_invite_and_notify

        with patch(
            "business.caselock_inbox.send_outbound", return_value=(True, None)
        ) as send:
            created = _create_invite_and_notify(
                self.company,
                "<real@test>",
                "buyer@example.com",
                "Need lock",
            )
        self.assertTrue(created)
        self.assertEqual(send.call_args.args[0], "outgoing@example.com")
        self.assertNotEqual(send.call_args.args[0], "caselock@example.com")
        invite = CaseLockInvite.objects.get(source_message_id="<real@test>")
        self.assertIsNotNone(invite.email_sent_at)

    def test_no_outbound_without_outgoing_email(self):
        from business.caselock_inbox import _send_mail_via_company

        self.company.outgoing_email = ""
        self.company.save(update_fields=["outgoing_email"])
        with patch("business.caselock_inbox.send_outbound") as send:
            ok = _send_mail_via_company(
                self.company,
                "buyer@example.com",
                "Case lock details",
                "body",
                "<p>html</p>",
            )
        self.assertFalse(ok)
        send.assert_not_called()


class SubscriptionRenewVendorTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            "owner@example.com", "pass"
        )
        self.company = CompanyProfile.objects.create(
            user=self.user, company_name="Co"
        )
        self.client_obj = Client.objects.create(
            company=self.company, name="OMSGuru"
        )
        self.product = Product.objects.create(
            company=self.company, name="Microsoft 365"
        )
        self.vendor = Vendor.objects.create(
            company=self.company, name="Redington"
        )
        self.other_vendor = Vendor.objects.create(
            company=self.company, name="Ingram"
        )
        self.subscription = ClientSubscription.objects.create(
            company=self.company,
            client=self.client_obj,
            product=self.product,
            vendor=self.vendor,
            start_date=date(2025, 9, 4),
            end_date=date(2026, 9, 3),
            quantity=2,
            billing_amount=Decimal("3137.29"),
        )
        self.client.force_login(self.user)

    def _post(self, vendor):
        return self.client.post(
            reverse("subscription_renew", kwargs={"pk": self.subscription.pk}),
            {
                "start_date": "2026-09-04",
                "end_date": "2027-09-03",
                "quantity": "2",
                "billing_amount": "3137.29",
                "vendor": vendor,
            },
        )

    def test_form_lists_existing_vendors(self):
        response = self.client.get(
            reverse("subscription_renew", kwargs={"pk": self.subscription.pk})
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "New vendor")
        self.assertContains(response, "Redington")
        self.assertContains(response, "Ingram")
        self.assertContains(response, "+ Add new vendor")
        self.assertNotContains(response, "New vendor name")
        self.assertContains(
            response,
            f'<option value="{self.vendor.pk}" selected>',
            html=False,
        )

    def test_select_existing_vendor(self):
        response = self._post(str(self.other_vendor.pk))
        self.assertEqual(response.status_code, 302)
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.vendor_id, self.other_vendor.pk)
        self.assertEqual(self.subscription.deal_status, ClientSubscription.DealStatus.WIN)
        self.assertTrue(self.subscription.win_form_completed)
        self.assertEqual(self.subscription.renewal_draft_vendor_id, self.other_vendor.pk)
        self.assertEqual(self.subscription.renewal_draft_quantity, 2)

    def test_add_new_vendor_redirects_to_add_page(self):
        response = self._post("__new__")
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("vendor_add"), response["Location"])
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.vendor_id, self.vendor.pk)
        self.assertFalse(
            Vendor.objects.filter(
                company=self.company, name="ABC Distributors"
            ).exists()
        )

    def test_add_vendor_returns_to_renew_with_new_vendor(self):
        renew_url = reverse(
            "subscription_renew", kwargs={"pk": self.subscription.pk}
        )
        response = self.client.post(
            reverse("vendor_add"),
            {"name": "ABC Distributors", "next": renew_url},
        )
        self.assertEqual(response.status_code, 302)
        created = Vendor.objects.get(
            company=self.company, name="ABC Distributors"
        )
        self.assertIn(renew_url, response["Location"])
        self.assertIn(f"preselect_vendor={created.pk}", response["Location"])
        renew = self.client.get(response["Location"])
        self.assertEqual(renew.status_code, 200)
        self.assertContains(
            renew,
            f'<option value="{created.pk}" selected>',
            html=False,
        )


class ClientRenewalDealStatusTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            "owner@example.com", "pass"
        )
        self.company = CompanyProfile.objects.create(
            user=self.user, company_name="Co"
        )
        self.client_obj = Client.objects.create(
            company=self.company, name="Hyfun", email="hyfun@example.com"
        )
        self.product = Product.objects.create(
            company=self.company, name="Microsoft 365 Business Basic"
        )
        self.vendor = Vendor.objects.create(
            company=self.company, name="Redington"
        )
        self.subscription = ClientSubscription.objects.create(
            company=self.company,
            client=self.client_obj,
            product=self.product,
            vendor=self.vendor,
            start_date=date(2024, 9, 11),
            end_date=date(2026, 9, 10),
            quantity=1,
            billing_amount=Decimal("7529.80"),
        )
        self.invite = SubscriptionRenewalInvite.objects.create(
            company=self.company,
            client_email="hyfun@example.com",
            email_sent_at=timezone.now(),
        )
        SubscriptionRenewalInviteLine.objects.create(
            invite=self.invite,
            subscription=self.subscription,
            company_name="Hyfun Agrilink Private Limited",
            product_name=self.product.name,
            current_quantity=1,
        )
        self.client.force_login(self.user)
        self.url = reverse(
            "client_renewal_set_deal_status", kwargs={"pk": self.invite.pk}
        )

    def test_win_marks_status_without_changing_subscription(self):
        response = self.client.post(
            self.url,
            {
                "deal_status": "win",
                "subscription_id": self.subscription.pk,
            },
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["open_win_form"], self.subscription.pk)
        self.assertTrue(data["deal_form_empty"])
        self.assertEqual(data["deal_form_empty_count"], 1)
        item = data["items"][0]
        self.assertEqual(item["deal_status"], "win")
        self.assertTrue(item["form_empty"])
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.deal_status, "win")
        self.assertFalse(self.subscription.win_form_completed)
        self.assertEqual(self.subscription.quantity, 1)
        self.assertEqual(self.subscription.start_date, date(2024, 9, 11))

    def test_cancel_is_rejected(self):
        response = self.client.post(
            self.url,
            {
                "deal_status": "cancel",
                "subscription_id": self.subscription.pk,
            },
        )
        self.assertEqual(response.status_code, 400)
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.deal_status, "")

    def test_win_form_saves_draft_only(self):
        self.client.post(
            self.url,
            {
                "deal_status": "win",
                "subscription_id": self.subscription.pk,
            },
        )
        response = self.client.post(
            self.url,
            {
                "action": "save_win_form",
                "subscription_id": self.subscription.pk,
                "start_date": "2026-09-11",
                "end_date": "2028-09-09",
                "quantity": "3",
                "billing_amount": "22589.40",
                "vendor": str(self.vendor.pk),
            },
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertFalse(data["deal_form_empty"])
        self.assertFalse(data["items"][0]["form_empty"])
        self.subscription.refresh_from_db()
        self.assertTrue(self.subscription.win_form_completed)
        self.assertEqual(self.subscription.renewal_draft_start_date, date(2026, 9, 11))
        self.assertEqual(self.subscription.renewal_draft_end_date, date(2028, 9, 9))
        self.assertEqual(self.subscription.renewal_draft_quantity, 3)
        self.assertEqual(
            self.subscription.renewal_draft_billing_amount, Decimal("22589.40")
        )
        self.assertEqual(self.subscription.quantity, 1)
        self.assertEqual(self.subscription.start_date, date(2024, 9, 11))
        self.assertEqual(self.subscription.billing_amount, Decimal("7529.80"))

        renew = self.client.get(
            reverse("subscription_renew", kwargs={"pk": self.subscription.pk})
        )
        self.assertEqual(renew.status_code, 200)
        form = renew.context["form"]
        self.assertEqual(form.initial["start_date"], date(2026, 9, 11))
        self.assertEqual(form.initial["end_date"], date(2028, 9, 9))
        self.assertEqual(form.initial["quantity"], 3)
        self.assertEqual(form.initial["billing_amount"], Decimal("22589.40"))
        self.assertEqual(form.initial["vendor"], str(self.vendor.pk))

    def test_renew_save_sets_win_and_clears_form_empty(self):
        self.subscription.deal_status = ClientSubscription.DealStatus.WIN
        self.subscription.win_form_completed = False
        self.subscription.save(update_fields=["deal_status", "win_form_completed"])
        response = self.client.post(
            reverse("subscription_renew", kwargs={"pk": self.subscription.pk}),
            {
                "start_date": "2026-09-11",
                "end_date": "2028-09-09",
                "quantity": "3",
                "billing_amount": "22589.40",
                "vendor": str(self.vendor.pk),
            },
        )
        self.assertEqual(response.status_code, 302)
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.deal_status, "win")
        self.assertTrue(self.subscription.win_form_completed)
        self.assertEqual(self.subscription.quantity, 3)
        self.assertEqual(self.subscription.start_date, date(2026, 9, 11))
        self.assertEqual(self.subscription.renewal_draft_quantity, 3)
        state = self.invite.deal_table_state()
        self.assertFalse(state["form_empty"])
        self.assertFalse(state["pending"])


class DashboardChartTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            "owner@example.com", "pass"
        )
        self.company = CompanyProfile.objects.create(
            user=self.user, company_name="Co"
        )
        self.client_obj = Client.objects.create(
            company=self.company, name="Hyfun", email="hyfun@example.com"
        )
        self.product = Product.objects.create(
            company=self.company, name="Microsoft 365 Business Basic"
        )
        self.vendor = Vendor.objects.create(company=self.company, name="Redington")
        today = timezone.localdate()
        self.win_sub = ClientSubscription.objects.create(
            company=self.company,
            client=self.client_obj,
            product=self.product,
            vendor=self.vendor,
            start_date=today - timedelta(days=365),
            end_date=today + timedelta(days=5),
            quantity=10,
            billing_amount=Decimal("1000.00"),
            deal_status=ClientSubscription.DealStatus.WIN,
        )
        self.lost_sub = ClientSubscription.objects.create(
            company=self.company,
            client=self.client_obj,
            product=self.product,
            vendor=self.vendor,
            start_date=today - timedelta(days=365),
            end_date=today + timedelta(days=8),
            quantity=2,
            billing_amount=Decimal("200.00"),
            deal_status=ClientSubscription.DealStatus.LOST,
        )
        self.pending_sub = ClientSubscription.objects.create(
            company=self.company,
            client=self.client_obj,
            product=self.product,
            vendor=self.vendor,
            start_date=today - timedelta(days=365),
            end_date=today + timedelta(days=3),
            quantity=4,
            billing_amount=Decimal("400.00"),
        )
        invite = SubscriptionRenewalInvite.objects.create(
            company=self.company,
            client_email="hyfun@example.com",
            email_sent_at=timezone.now(),
            submitted_at=timezone.now(),
        )
        SubscriptionRenewalInviteLine.objects.create(
            invite=invite,
            subscription=self.pending_sub,
            company_name="Hyfun",
            product_name=self.product.name,
            current_quantity=4,
        )
        SubscriptionRenewalInviteNewProduct.objects.create(
            invite=invite,
            product=self.product,
            company_name="Hyfun",
            product_name="VPN",
            quantity=7,
        )
        self.client.force_login(self.user)

    def test_sales_charts_count_win_lost_pending_and_new_qty(self):
        charts = sales_dashboard_charts(self.company, today=timezone.localdate())
        self.assertEqual(charts["win"], 1)
        self.assertEqual(charts["lost"], 1)
        self.assertEqual(charts["pending_deals"], 1)
        self.assertEqual(charts["win_rate"], 50)
        self.assertGreaterEqual(charts["new_qty_month"], 7)
        self.assertEqual(len(charts["pipeline_labels"]), 14)
        self.assertEqual(len(charts["qty_month_labels"]), 6)

    def test_dashboard_renders_chart_markup(self):
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Deal outcome")
        self.assertContains(response, "Pending renewals")
        self.assertContains(response, "New subscription qty")
        self.assertContains(response, "chart-deal-outcome")
        self.assertContains(response, "sales-charts-data")

    @patch(
        "business.helpdesk_inbox.maybe_fetch_on_dashboard",
        return_value={"created": 0, "logs": []},
    )
    def test_support_tickets_renders_charts(self, _fetch):
        response = self.client.get(reverse("support_ticket_list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Avg resolution time")
        self.assertContains(response, "SLA breached")
        self.assertContains(response, "chart-status-mix")
        self.assertContains(response, "ticket-charts-data")


class TicketChartHelperTests(SimpleTestCase):
    def test_format_duration(self):
        self.assertEqual(format_duration(None), "0m")
        self.assertEqual(format_duration(45), "1m")
        self.assertEqual(format_duration(0), "0m")
        self.assertEqual(format_duration(120), "2m")
        self.assertEqual(format_duration(4 * 3600 + 12 * 60), "4h 12m")

    def test_ticket_charts_sla_and_status(self):
        now = timezone.now()
        tickets = [
            empty_ticket_view(
                status="closed",
                created_at=now - timedelta(hours=5),
                completed_at=now - timedelta(hours=1),
                first_responded_at=now - timedelta(hours=4),
                sla={
                    "tracks_resolve": True,
                    "tracks_response": True,
                    "resolve_status": "met",
                    "response_status": "met",
                    "any_breached": False,
                },
            ),
            empty_ticket_view(
                status="open",
                created_at=now - timedelta(hours=10),
                priority="high",
                assignee_id=None,
                sla={"any_breached": True, "tracks_resolve": True, "resolve_status": "breached"},
            ),
        ]
        charts = ticket_charts_from_views(tickets, now=now)
        self.assertEqual(charts["total"], 2)
        self.assertEqual(charts["closed"], 1)
        self.assertEqual(charts["pending"], 1)
        self.assertEqual(charts["sla_breached"], 1)
        self.assertEqual(charts["solved_on_time"], 1)
        self.assertEqual(charts["solved_with_sla"], 1)
        self.assertEqual(charts["solved_on_time_pct"], 100)
        self.assertEqual(charts["queue"]["high_unassigned"], 1)
        self.assertEqual(charts["queue"]["waiting_8h"], 1)
        self.assertEqual(charts["avg_resolution"], "4h")
        self.assertEqual(charts["first_response"], "1h")

    def test_resolution_uses_updated_at_when_completed_missing(self):
        now = timezone.now()
        tickets = [
            empty_ticket_view(
                status="closed",
                created_at=now - timedelta(hours=3),
                completed_at=None,
                updated_at=now - timedelta(hours=1),
            )
        ]
        charts = ticket_charts_from_views(tickets, now=now)
        self.assertEqual(charts["avg_resolution"], "2h")

    def test_resolution_is_zero_minutes_when_none_closed(self):
        now = timezone.now()
        tickets = [
            empty_ticket_view(status="open", created_at=now - timedelta(hours=2))
        ]
        charts = ticket_charts_from_views(tickets, now=now)
        self.assertEqual(charts["avg_resolution"], "0m")
        self.assertEqual(charts["first_response"], "0m")


class SubscriptionImportIdentityTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            "import-owner@example.com", "pass"
        )
        self.company = CompanyProfile.objects.create(
            user=self.user, company_name="Import Co"
        )
        self.client_obj = Client.objects.create(
            company=self.company, name="Acme"
        )
        self.product = Product.objects.create(
            company=self.company, name="Microsoft 365"
        )
        self.vendor = Vendor.objects.create(
            company=self.company, name="Redington"
        )
        self.subscription = ClientSubscription.objects.create(
            company=self.company,
            client=self.client_obj,
            product=self.product,
            vendor=self.vendor,
            subscription_code="SUB-001",
            start_date=date(2026, 1, 1),
            end_date=date(2027, 1, 1),
            quantity=1,
            billing_amount=Decimal("1200"),
        )

    def test_reimport_matches_subscription_id_case_insensitively(self):
        csv_data = (
            "Subscription Id,Product Name,Customer Name,Start Date,"
            "End Date,Quantity,Price\n"
            "sub-001,Microsoft 365,Acme,2026-02-01,2027-02-01,3,2400\n"
        ).encode()

        created, updated, errors = import_subscriptions_from_file(
            self.company,
            csv_data,
            "subscriptions.csv",
            import_vendor=self.vendor,
        )

        self.assertEqual((created, updated, errors), (0, 1, []))
        self.assertEqual(
            ClientSubscription.objects.filter(company=self.company).count(),
            1,
        )
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.quantity, 3)
        self.assertEqual(self.subscription.billing_amount, Decimal("2400"))

    def test_database_rejects_case_only_duplicate_subscription_id(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ClientSubscription.objects.create(
                    company=self.company,
                    client=self.client_obj,
                    product=self.product,
                    subscription_code="sub-001",
                    start_date=date(2026, 1, 1),
                )

