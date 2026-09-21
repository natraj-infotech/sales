from datetime import date, datetime, time, timedelta
from decimal import Decimal
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import patch

from openpyxl import load_workbook

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
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
    renewal_report_charts,
    sales_dashboard_charts,
    ticket_charts_from_views,
)
from business.subscription_export import (
    NO_COMPANY_VALUE,
    build_subscription_export_bytes,
    normalize_column_keys,
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
        self.assertTrue(rows["Antivirus"]["start_date"])
        self.assertTrue(rows["Antivirus"]["end_date"])
        self.assertEqual(rows["VPN"]["start_date"], "")
        self.assertEqual(rows["VPN"]["end_date"], "")
        self.assertEqual(rows["Antivirus"]["ref_id"], "")
        self.assertEqual(rows["VPN"]["ref_id"], "")

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
        self.assertContains(response, "Quotation ID")
        self.assertContains(response, 'class="cr-th-ref"')
        self.assertNotContains(response, 'class="cr-th-email"')
        self.assertContains(response, "Connect Zoho")
        self.assertContains(response, ">Win</button>")
        self.assertContains(response, ">Lost</button>")
        self.assertNotContains(response, "cr-deal-pill cr-status--pending")
        self.assertContains(response, "Acme")
        self.assertContains(response, 'class="cr-expand-btn"')
        self.assertContains(response, 'data-company="Acme"')
        self.assertContains(response, 'id="cr-info-modal"')
        self.assertContains(response, "Product info")
        self.assertContains(response, "Email info")
        self.assertContains(response, 'class="cr-products"')
        self.assertNotContains(response, "Show timestamps")
        self.assertNotContains(response, "cr-ts-row")
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
        self.assertTrue(rows["Antivirus"]["start_date"])
        self.assertTrue(rows["Antivirus"]["end_date"])
        self.assertEqual(rows["VPN"]["start_date"], "")
        self.assertEqual(rows["VPN"]["end_date"], "")

    def test_zoho_payload_includes_open_quote_url(self):
        self.invite.zoho_quote_url = "https://crm.zoho.com/crm/quote/1"
        self.invite.zoho_quote_id = "zquote-1"
        self.invite.zoho_quote_number = "QT-91"
        self.invite.quotation_sent_at = timezone.now()
        self.invite.save(
            update_fields=[
                "zoho_quote_url",
                "zoho_quote_id",
                "zoho_quote_number",
                "quotation_sent_at",
            ]
        )
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
        self.assertEqual(antivirus["ref_id"], "QT-91")
        vpn = next(row for row in rows if row["product_name"] == "VPN")
        self.assertEqual(vpn["zoho"]["quote_url"], "https://crm.zoho.com/crm/quote/1")
        self.assertEqual(vpn["ref_id"], "QT-91")
        pending = next(
            row
            for row in rows
            if row["product_name"] == "Antivirus"
            and row["client_email"] == "pending@example.com"
        )
        self.assertEqual(pending["zoho"]["kind"], "send")
        self.assertTrue(pending["zoho"]["send_url"])

    def _quote_payload(self, invite):
        from business.zoho_crm import create_quote_from_renewal_invite

        self.company.zoho_refresh_token = "refresh-token"
        self.company.save(update_fields=["zoho_refresh_token"])
        with (
            patch("business.zoho_crm.zoho_configured", return_value=True),
            patch("business.zoho_crm.find_or_create_account", return_value="acc-1"),
            patch("business.zoho_crm.find_or_create_product", return_value="prod-1"),
            patch(
                "business.zoho_crm._create_record",
                return_value={"id": "q-1", "Quoted_Items": "QT-1"},
            ) as create_record,
            patch("business.zoho_crm.requests.get") as get_quote,
        ):
            get_quote.return_value.ok = False
            create_quote_from_renewal_invite(self.company, invite)
        return create_record.call_args.args[2]

    def test_quote_uses_renewal_qty_when_present(self):
        payload = self._quote_payload(self.invite)
        self.assertEqual(payload["Product_Details"][0]["quantity"], 8)

    def test_quote_uses_current_qty_when_renewal_qty_missing(self):
        pending = SubscriptionRenewalInvite.objects.get(
            client_email="pending@example.com"
        )
        payload = self._quote_payload(pending)
        self.assertEqual(payload["Product_Details"][0]["quantity"], 5)
        self.assertIn("no renewal qty yet", payload["Description"])


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
                "invoice_number": "INV-778",
                "invoice_pdf": SimpleUploadedFile(
                    "renewal.pdf",
                    b"%PDF-1.4 test",
                    content_type="application/pdf",
                ),
            },
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertFalse(data["deal_form_empty"])
        self.assertFalse(data["items"][0]["form_empty"])
        self.assertEqual(data["items"][0]["draft"]["invoice_number"], "INV-778")
        self.subscription.refresh_from_db()
        self.assertTrue(self.subscription.win_form_completed)
        self.assertEqual(self.subscription.renewal_draft_start_date, date(2026, 9, 11))
        self.assertEqual(self.subscription.renewal_draft_end_date, date(2028, 9, 9))
        self.assertEqual(self.subscription.renewal_draft_quantity, 3)
        self.assertEqual(
            self.subscription.renewal_draft_billing_amount, Decimal("22589.40")
        )
        self.assertEqual(self.subscription.renewal_draft_invoice_number, "INV-778")
        self.assertTrue(self.subscription.renewal_draft_invoice_pdf)
        self.assertEqual(self.subscription.quantity, 1)
        self.assertEqual(self.subscription.start_date, date(2024, 9, 11))
        self.assertEqual(self.subscription.billing_amount, Decimal("7529.80"))
        self.assertEqual(self.subscription.invoice_number, "")

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
        self.assertEqual(form.initial["invoice_number"], "INV-778")
        self.assertContains(renew, "New invoice number")
        self.assertContains(renew, "New invoice PDF")
        self.assertContains(renew, "New vendor")

    def test_win_form_can_draft_other_plan_without_changing_live_product(self):
        cheap = Product.objects.create(
            company=self.company,
            name="Microsoft 365 Business Standard",
            unit_price=Decimal("100.00"),
        )
        premium = Product.objects.create(
            company=self.company,
            name="Microsoft 365 E5",
            unit_price=Decimal("5000.00"),
        )
        self.product.unit_price = Decimal("50.00")
        self.product.save(update_fields=["unit_price"])
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
                "end_date": "2027-09-10",
                "quantity": "2",
                "billing_amount": "9000.00",
                "vendor": str(self.vendor.pk),
                "renew_other_plan": "1",
                "product": str(cheap.pk),
            },
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(
            [row["name"] for row in data["products"]],
            [
                self.product.name,
                cheap.name,
                premium.name,
            ],
        )
        draft = data["items"][0]["draft"]
        self.assertTrue(draft["renew_other_plan"])
        self.assertEqual(draft["product"], str(cheap.pk))
        self.assertEqual(draft["product_name"], cheap.name)
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.product_id, self.product.pk)
        self.assertEqual(self.subscription.renewal_draft_product_id, cheap.pk)

        renew = self.client.get(
            reverse("subscription_renew", kwargs={"pk": self.subscription.pk})
        )
        self.assertEqual(renew.status_code, 200)
        self.assertContains(renew, "New product")
        self.assertContains(renew, cheap.name)
        self.assertContains(renew, self.product.name)

        saved = self.client.post(
            reverse("subscription_renew", kwargs={"pk": self.subscription.pk}),
            {
                "start_date": "2026-09-11",
                "end_date": "2027-09-10",
                "quantity": "2",
                "billing_amount": "9000.00",
                "vendor": str(self.vendor.pk),
                "product": str(cheap.pk),
                "renew_other_plan": "1",
            },
        )
        self.assertEqual(saved.status_code, 302)
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.product_id, cheap.pk)
        self.assertEqual(self.subscription.product.name, cheap.name)

    def test_add_subscription_form_has_optional_invoice_fields(self):
        response = self.client.get(reverse("subscription_add"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Invoice number")
        self.assertContains(response, "Invoice PDF copy")
        self.assertContains(response, 'enctype="multipart/form-data"')

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
                "invoice_number": "INV-900",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.deal_status, "win")
        self.assertTrue(self.subscription.win_form_completed)
        self.assertEqual(self.subscription.quantity, 3)
        self.assertEqual(self.subscription.start_date, date(2026, 9, 11))
        self.assertEqual(self.subscription.renewal_draft_quantity, 3)
        self.assertEqual(self.subscription.invoice_number, "INV-900")
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


class RenewalReportTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            "renewal-report@example.com", "pass"
        )
        self.company = CompanyProfile.objects.create(
            user=self.user, company_name="Report Co"
        )
        self.client_obj = Client.objects.create(
            company=self.company, name="Acme Steel", email="acme@example.com"
        )
        self.product = Product.objects.create(company=self.company, name="Firewall")
        self.add_on = Product.objects.create(company=self.company, name="Backup Cloud")
        self.vendor = Vendor.objects.create(company=self.company, name="Vendor")
        today = timezone.localdate()
        tz = timezone.get_current_timezone()
        eligible = timezone.make_aware(
            datetime.combine(today + timedelta(days=10) - timedelta(days=30), time.min),
            tz,
        )
        sent = eligible + timedelta(days=2)
        deal_at = sent + timedelta(days=4)

        self.pending_sub = ClientSubscription.objects.create(
            company=self.company,
            client=self.client_obj,
            product=self.product,
            vendor=self.vendor,
            start_date=today - timedelta(days=355),
            end_date=today + timedelta(days=10),
            quantity=4,
            billing_amount=Decimal("4000.00"),
        )
        self.silent_sub = ClientSubscription.objects.create(
            company=self.company,
            client=self.client_obj,
            product=self.product,
            vendor=self.vendor,
            start_date=today - timedelta(days=400),
            end_date=today - timedelta(days=12),
            quantity=2,
            billing_amount=Decimal("800.00"),
        )
        self.win_sub = ClientSubscription.objects.create(
            company=self.company,
            client=self.client_obj,
            product=self.product,
            vendor=self.vendor,
            start_date=today - timedelta(days=365),
            end_date=today + timedelta(days=10),
            quantity=3,
            billing_amount=Decimal("1500.00"),
            deal_status=ClientSubscription.DealStatus.WIN,
            deal_status_set_at=deal_at,
        )
        self.lost_sub = ClientSubscription.objects.create(
            company=self.company,
            client=self.client_obj,
            product=self.product,
            vendor=self.vendor,
            start_date=today - timedelta(days=365),
            end_date=today - timedelta(days=3),
            quantity=1,
            billing_amount=Decimal("250.00"),
            deal_status=ClientSubscription.DealStatus.LOST,
            deal_status_set_at=timezone.now(),
        )
        pending_invite = SubscriptionRenewalInvite.objects.create(
            company=self.company,
            client_email="acme@example.com",
            email_sent_at=sent,
            read_at=sent + timedelta(hours=6),
            submitted_at=sent + timedelta(days=1),
        )
        SubscriptionRenewalInviteLine.objects.create(
            invite=pending_invite,
            subscription=self.pending_sub,
            company_name="Acme Steel",
            product_name=self.product.name,
            current_quantity=4,
            renewal_count=6,
        )
        SubscriptionRenewalInviteNewProduct.objects.create(
            invite=pending_invite,
            product=self.add_on,
            company_name="Acme Steel",
            product_name="Backup Cloud",
            quantity=5,
        )
        win_invite = SubscriptionRenewalInvite.objects.create(
            company=self.company,
            client_email="acme@example.com",
            email_sent_at=sent,
            submitted_at=sent + timedelta(days=1),
        )
        SubscriptionRenewalInviteLine.objects.create(
            invite=win_invite,
            subscription=self.win_sub,
            company_name="Acme Steel",
            product_name=self.product.name,
            current_quantity=3,
            renewal_count=3,
        )
        self.client.force_login(self.user)

    def test_report_counts_pending_silent_lost_and_new_products(self):
        report = renewal_report_charts(self.company, range_key="90")
        self.assertEqual(report["pending_renewals"], 1)
        self.assertEqual(report["silent_expired"], 1)
        self.assertEqual(report["lost_count"], 1)
        self.assertEqual(report["win_count"], 1)
        self.assertEqual(report["resub_rate"], 50)
        self.assertEqual(report["did_not_renew"], 2)
        self.assertEqual(report["new_product_clients"], 1)
        self.assertEqual(report["new_product_qty"], 5)
        self.assertEqual(report["qty_up"], 1)
        self.assertEqual(report["qty_same"], 1)
        self.assertEqual(report["avg_send_delay"], "2d")
        self.assertEqual(report["avg_deal_delay"], "4d")
        self.assertEqual(report["charts"]["funnel_values"][0], 2)
        remaining_clients = [row["client"] for row in report["remaining_rows"]]
        self.assertIn("Acme Steel", remaining_clients)
        self.assertTrue(report["new_product_rows"])
        reasons = {row["reason"] for row in report["did_not_renew_rows"]}
        self.assertIn("Lost", reasons)
        self.assertTrue(any("Silent" in reason for reason in reasons))

    def test_report_downloads_pdf(self):
        response = self.client.get(reverse("client_renewal_report"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertIn("attachment", response["Content-Disposition"])
        self.assertIn("client_renewal_report_", response["Content-Disposition"])
        self.assertTrue(response["Content-Disposition"].endswith('.pdf"'))
        self.assertTrue(response.content.startswith(b"%PDF-"))
        self.assertGreater(len(response.content), 2000)

    def test_tracker_page_has_report_download(self):
        response = self.client.get(reverse("client_renewal_list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse("client_renewal_report"))
        self.assertContains(response, "Download report")
        self.assertNotContains(response, "cr-tabs")
        self.assertNotContains(response, "Tracker</a>")


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

    def test_import_sets_optional_invoice_number(self):
        csv_data = (
            "Subscription Id,Product Name,Customer Name,Start Date,"
            "End Date,Quantity,Price,Invoice Number\n"
            "SUB-NEW,Microsoft 365,Acme,2026-03-01,2027-03-01,2,1800,INV-42\n"
        ).encode()
        created, updated, errors = import_subscriptions_from_file(
            self.company,
            csv_data,
            "subscriptions.csv",
            import_vendor=self.vendor,
        )
        self.assertEqual((created, updated, errors), (1, 0, []))
        created_sub = ClientSubscription.objects.get(
            company=self.company, subscription_code="SUB-NEW"
        )
        self.assertEqual(created_sub.invoice_number, "INV-42")
        self.assertFalse(created_sub.invoice_pdf)

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


class OemTicketFeatureTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(
            email="oem-owner@example.com", password="pass12345"
        )
        self.company = CompanyProfile.objects.create(
            user=self.user,
            company_name="Acme OEM",
            oem_email="oem-from@example.com",
            oem_email_app_password="oem-secret",
        )
        self.client.login(email="oem-owner@example.com", password="pass12345")
        inbox_patch = patch(
            "business.helpdesk_inbox.maybe_fetch_on_dashboard",
            return_value={"created": 0, "logs": []},
        )
        inbox_patch.start()
        self.addCleanup(inbox_patch.stop)

    def test_company_setup_shows_oem_email(self):
        response = self.client.get(reverse("company_setup"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "OEM email")
        self.assertContains(response, "oem_email")

    def test_internal_and_oem_tabs(self):
        internal = self.client.get(reverse("support_ticket_list"))
        self.assertEqual(internal.status_code, 200)
        self.assertContains(internal, "External (OEM)")
        self.assertContains(internal, "Transfer to OEM")
        oem = self.client.get(reverse("support_ticket_list") + "?source=oem")
        self.assertEqual(oem.status_code, 200)
        self.assertContains(oem, "Email Diary")
        self.assertContains(oem, "Subscription ID")
        self.assertContains(oem, "+ OEM ticket")
        self.assertNotContains(oem, "Serial Number")

    def test_diary_add_company_and_extra_email(self):
        response = self.client.post(
            reverse("support_oem_diary"),
            {"action": "add_company", "name": "HP", "email": "support@hp.example"},
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["rows"][0]["name"], "HP")
        extra = self.client.post(
            reverse("support_oem_diary"),
            {"action": "add_email", "name": "HP", "email": "care@hp.example"},
        )
        emails = {item["email"] for item in extra.json()["rows"][0]["emails"]}
        self.assertEqual(emails, {"support@hp.example", "care@hp.example"})

    def test_quick_oem_contacts_list_products_with_company_fields(self):
        Product.objects.create(
            company=self.company,
            name="Laptop",
            company_name="HP",
            company_email="support@hp.example",
        )
        Product.objects.create(company=self.company, name="Printer")
        self.client.post(
            reverse("support_oem_diary"),
            {"action": "add_company", "name": "abc", "email": "keniep442006@gmail.com"},
        )
        page = self.client.get(reverse("support_oem_ticket_add") + "?mode=quick")
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Search / select product...")
        self.assertContains(page, "Product (optional)")
        response = self.client.get(reverse("support_oem_contacts"))
        self.assertEqual(response.status_code, 200)
        contacts = response.json()["contacts"]
        by_product = {row["product_name"]: row for row in contacts}
        self.assertIn("Laptop", by_product)
        self.assertIn("Printer", by_product)
        self.assertEqual(by_product["Laptop"]["name"], "HP")
        self.assertEqual(by_product["Laptop"]["email"], "support@hp.example")
        self.assertEqual(by_product["Printer"]["name"], "")
        self.assertEqual(by_product["Printer"]["email"], "")
        self.assertNotIn("keniep442006@gmail.com", {row["email"] for row in contacts})

    @patch("business.oem_tickets.send_outbound", return_value=(True, None))
    def test_quick_oem_creates_ticket(self, _send):
        response = self.client.post(
            reverse("support_oem_ticket_add"),
            {
                "mode": "quick",
                "to_email": "vendor@example.com",
                "oem_company": "HP",
                "subject": "Laptop issue",
                "description": "Screen flicker",
                "cc": "",
                "subscription_id": "",
            },
        )
        self.assertEqual(response.status_code, 302)
        from business.models import SupportTicketMeta

        meta = SupportTicketMeta.objects.get(is_oem=True)
        self.assertEqual(meta.oem_company_name, "HP")
        self.assertEqual(meta.oem_to_email, "vendor@example.com")
        self.assertEqual(meta.subscription_id, "")
        html_body = _send.call_args.kwargs["html_body"]
        plain_body = _send.call_args.args[4]
        self.assertIn("OEM SUPPORT", html_body)
        self.assertIn("Laptop issue", html_body)
        self.assertIn("Screen flicker", html_body)
        self.assertIn("Best Regards", html_body)
        self.assertIn("Acme OEM", html_body)
        self.assertNotIn("HP", html_body)
        self.assertIn("Hello,", plain_body)
        self.assertIn("Screen flicker", plain_body)
        self.assertIn("Best Regards,", plain_body)
        self.assertIn("Acme OEM", plain_body)

    def test_oem_email_html_uses_subject_heading_and_attachment_chips(self):
        from types import SimpleNamespace

        from business.oem_tickets import build_oem_email_html, build_oem_email_text

        company = SimpleNamespace(company_name="Acme OEM")
        html_body = build_oem_email_html(
            company,
            subject="Laptop issue",
            body="Screen flicker",
            attachment_names=["photo.png"],
            logo_cid="company-logo",
        )
        self.assertIn("OEM SUPPORT", html_body)
        self.assertIn("<h1", html_body)
        self.assertIn("Laptop issue", html_body)
        self.assertIn("Screen flicker", html_body)
        self.assertNotIn("Attachments", html_body)
        self.assertNotIn("ATTACHMENTS", html_body)
        self.assertIn("photo.png", html_body)
        self.assertIn("cid:company-logo", html_body)
        self.assertIn("Best Regards", html_body)
        self.assertNotIn("ticket id", html_body.lower())
        text_body = build_oem_email_text(
            company,
            subject="Laptop issue",
            body="Screen flicker",
            attachment_names=["photo.png"],
        )
        self.assertIn("- photo.png", text_body)

    @patch("business.oem_tickets.send_outbound", return_value=(True, None))
    def test_transfer_marks_internal_ticket(self, _send):
        from business.helpdesk_bridge import create_internal_ticket

        source = create_internal_ticket(
            company=self.company,
            user=self.user,
            subject="Internal printer issue",
            description="Won't print",
        )
        response = self.client.post(
            reverse("support_ticket_transfer_oem", kwargs={"pk": source.pk}),
            {
                "to_email": "oem@example.com",
                "oem_company": "HP",
                "subject": "Internal printer issue",
                "description": "Won't print",
            },
        )
        self.assertEqual(response.status_code, 302)
        source.crm_meta.refresh_from_db()
        self.assertTrue(source.crm_meta.transferred_to_id)
        detail = self.client.get(
            reverse("support_ticket_detail", kwargs={"pk": source.pk})
        )
        self.assertContains(detail, "Transferred to")

    def test_subscription_lookup_fills_oem_details(self):
        client_obj = Client.objects.create(company=self.company, name="Retail Co")
        product = Product.objects.create(
            company=self.company,
            name="Laptop",
            company_name="HP",
            company_email="support@hp.example",
        )
        ClientSubscription.objects.create(
            company=self.company,
            client=client_obj,
            product=product,
            subscription_code="SUB-88",
            start_date=date(2026, 1, 1),
            serial_number="HP-123",
        )
        response = self.client.get(
            reverse("support_oem_subscription_lookup"), {"q": "SUB-88"}
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()["subscription"]
        self.assertEqual(data["oem_company"], "HP")
        self.assertEqual(data["oem_email"], "support@hp.example")
        self.assertEqual(data["client_name"], "Retail Co")

    def test_subscription_lookup_lists_options_for_dropdown(self):
        client_obj = Client.objects.create(company=self.company, name="Retail Co")
        product = Product.objects.create(
            company=self.company,
            name="Laptop",
            company_name="HP",
            company_email="support@hp.example",
        )
        ClientSubscription.objects.create(
            company=self.company,
            client=client_obj,
            product=product,
            subscription_code="SUB-88",
            start_date=date(2026, 1, 1),
            serial_number="HP-123",
        )
        response = self.client.get(reverse("support_oem_subscription_lookup"))
        self.assertEqual(response.status_code, 200)
        rows = response.json()["subscriptions"]
        self.assertEqual(rows[0]["subscription_id"], "SUB-88")
        self.assertEqual(rows[0]["client_name"], "Retail Co")
        page = self.client.get(reverse("support_oem_ticket_add") + "?mode=standard")
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Search / select subscription ID")


class SubscriptionListExportTests(TestCase):
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
        self.hp_product = Product.objects.create(
            company=self.company,
            name="Laptop",
            category="Hardware",
            company_name="HP",
        )
        self.ms_product = Product.objects.create(
            company=self.company,
            name="Microsoft 365",
            category="Software",
            company_name="Microsoft",
        )
        self.blank_product = Product.objects.create(
            company=self.company,
            name="Unknown Box",
            category="Hardware",
            company_name="",
        )
        self.vendor = Vendor.objects.create(company=self.company, name="Redington")
        self.hp_sub = ClientSubscription.objects.create(
            company=self.company,
            client=self.client_obj,
            product=self.hp_product,
            vendor=self.vendor,
            start_date=date(2026, 1, 1),
            end_date=date(2027, 1, 1),
            quantity=2,
            billing_amount=Decimal("1500.00"),
            serial_number="HP-123",
            subscription_code="SUB-HP",
            invoice_number="INV-1",
            notes="HP laptop note",
        )
        self.ms_sub = ClientSubscription.objects.create(
            company=self.company,
            client=self.client_obj,
            product=self.ms_product,
            vendor=self.vendor,
            start_date=date(2026, 2, 1),
            end_date=date(2027, 2, 1),
            quantity=5,
            billing_amount=Decimal("900.00"),
            subscription_code="SUB-MS",
        )
        self.blank_sub = ClientSubscription.objects.create(
            company=self.company,
            client=self.client_obj,
            product=self.blank_product,
            start_date=date(2026, 3, 1),
            quantity=1,
            billing_amount=Decimal("10.00"),
        )
        self.client.force_login(self.user)

    def test_list_shows_company_column_and_filter(self):
        response = self.client.get(reverse("subscription_list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "<th>Company</th>", html=True)
        self.assertContains(response, "HP")
        self.assertContains(response, "Microsoft")
        self.assertContains(response, 'name="company_name"')
        self.assertContains(response, "Export")
        self.assertContains(response, "Columns to include")
        self.assertContains(response, "Companies to include")

    def test_filter_by_company_name(self):
        response = self.client.get(
            reverse("subscription_list"), {"company_name": "HP"}
        )
        self.assertEqual(response.status_code, 200)
        names = [row.product.name for row in response.context["object_list"]]
        self.assertEqual(names, ["Laptop"])

    def test_filter_by_blank_company(self):
        response = self.client.get(
            reverse("subscription_list"), {"company_name": NO_COMPANY_VALUE}
        )
        self.assertEqual(response.status_code, 200)
        names = [row.product.name for row in response.context["object_list"]]
        self.assertEqual(names, ["Unknown Box"])

    def test_export_selected_company_and_columns(self):
        response = self.client.post(
            reverse("subscription_export"),
            {
                "columns": ["client", "product", "company", "quantity"],
                "companies": ["HP"],
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response["Content-Type"],
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        wb = load_workbook(BytesIO(response.content))
        rows = list(wb.active.iter_rows(values_only=True))
        self.assertEqual(rows[0], ("Client", "Product", "Company", "Quantity"))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1], ("OMSGuru", "Laptop", "HP", 2))

    def test_export_requires_columns_and_companies(self):
        missing_cols = self.client.post(
            reverse("subscription_export"),
            {"companies": ["HP"]},
        )
        self.assertEqual(missing_cols.status_code, 302)
        missing_companies = self.client.post(
            reverse("subscription_export"),
            {"columns": ["client"]},
        )
        self.assertEqual(missing_companies.status_code, 302)

    def test_export_keeps_page_client_filter(self):
        other_client = Client.objects.create(company=self.company, name="Other Co")
        ClientSubscription.objects.create(
            company=self.company,
            client=other_client,
            product=self.hp_product,
            start_date=date(2026, 4, 1),
            quantity=9,
        )
        response = self.client.post(
            reverse("subscription_export"),
            {
                "client": str(self.client_obj.pk),
                "columns": ["client", "product"],
                "companies": ["HP"],
            },
        )
        self.assertEqual(response.status_code, 200)
        rows = list(
            load_workbook(BytesIO(response.content)).active.iter_rows(values_only=True)
        )
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1][0], "OMSGuru")

    def test_export_builder_uses_requested_column_order(self):
        self.assertEqual(
            normalize_column_keys(["bogus", "product", "product", "client"]),
            ["product", "client"],
        )
        content = build_subscription_export_bytes(
            [self.ms_sub], ["product", "company"]
        )
        rows = list(load_workbook(BytesIO(content)).active.iter_rows(values_only=True))
        self.assertEqual(rows[0], ("Product", "Company"))
        self.assertEqual(rows[1], ("Microsoft 365", "Microsoft"))


class ProductPriceFieldsTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            "owner@example.com", "pass"
        )
        self.company = CompanyProfile.objects.create(
            user=self.user, company_name="Co"
        )
        self.client.login(email="owner@example.com", password="pass")

    def test_add_form_shows_price_gst_and_discounted_price(self):
        response = self.client.get(reverse("product_add"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Price")
        self.assertContains(response, "GST (%)")
        self.assertContains(response, "Discounted price")
        self.assertContains(response, 'name="unit_price"')
        self.assertContains(response, 'name="tax"')
        self.assertContains(response, 'name="discounted_price"')

    def test_create_product_saves_prices_and_list_shows_unit_price_only(self):
        response = self.client.post(
            reverse("product_add"),
            {
                "name": "Laptop",
                "category": "Hardware",
                "asset_type": "Laptop",
                "unit_price": "1200.50",
                "tax": "18",
                "discounted_price": "999.00",
                "components_count": "0",
            },
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        product = Product.objects.get(company=self.company, name="Laptop")
        self.assertEqual(product.unit_price, Decimal("1200.50"))
        self.assertEqual(product.tax, "18%")
        self.assertEqual(product.discounted_price, Decimal("999.00"))
        self.assertContains(response, "Price")
        self.assertContains(response, "1200.50")
        self.assertNotContains(response, "Discounted price")
        self.assertNotContains(response, "999.00")

