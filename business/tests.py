from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import CompanyProfile
from business.models import (
    Client,
    ClientSubscription,
    Product,
    SubscriptionRenewalInvite,
    SubscriptionRenewalInviteLine,
    SubscriptionRenewalInviteNewProduct,
    SubscriptionRenewalInviteRecipient,
)

from business.expiry_notifications import (
    build_renewal_quote_items,
    send_renewal_confirmation_email,
    send_renewal_quotation_email,
)
from business.renewal_tokens import (
    sign_renewal_confirm_token,
    unsign_renewal_confirm_token,
)
from business.views import (
    _existing_expiry_rows,
    _new_subscription_rows,
    _renewal_invite_queryset,
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

    def test_new_tab_only_includes_submitted_add_products(self):
        rows = _new_subscription_rows(self._invites())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["client_email"], "acme@example.com")
        self.assertEqual(rows[0]["product_name"], "VPN")
        self.assertEqual(rows[0]["quantity"], 3)

    def test_existing_tab_shows_previous_and_new_quantity(self):
        rows = _existing_expiry_rows(self._invites())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["product_name"], "Antivirus")
        self.assertEqual(rows[0]["previous_quantity"], 5)
        self.assertEqual(rows[0]["new_quantity"], 8)

    def test_list_page_splits_tabs(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("client_renewal_list"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["new_subscription_rows"]), 1)
        self.assertEqual(len(response.context["existing_expiry_rows"]), 1)
        self.assertEqual(len(response.context["object_list"]), 2)
        self.assertContains(response, "New subscriptions")
        self.assertContains(response, "Renewal requests")
        self.assertContains(response, "All emails")
        self.assertContains(response, "VPN")
        self.assertContains(response, "Previous quantity")
        self.assertContains(response, "pending@example.com")

    def test_live_json_includes_tab_rows(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("client_renewal_live"))
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(len(data["new_subscription_rows"]), 1)
        self.assertEqual(data["new_subscription_rows"][0]["product_name"], "VPN")
        self.assertEqual(data["new_subscription_rows"][0]["quantity"], 3)
        self.assertEqual(len(data["existing_expiry_rows"]), 1)
        self.assertEqual(data["existing_expiry_rows"][0]["previous_quantity"], 5)
        self.assertEqual(data["existing_expiry_rows"][0]["new_quantity"], 8)
        self.assertEqual(len(data["rows"]), 2)
