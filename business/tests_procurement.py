from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import CompanyMember, CompanyProfile
from business.models import (
    DealRound,
    DealRoundVendorPrice,
    DealVendor,
    ProcurementDeal,
    Product,
    Vendor,
    VendorPriceSubmission,
)
from business.procurement import (
    compare_rows_for_round,
    create_deal,
    mark_round_final,
    next_deal_no,
    submit_vendor_prices,
    vendors_for_products,
)


class ProcurementFlowTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_user("owner@example.com", "pass")
        self.company = CompanyProfile.objects.create(
            user=self.owner,
            company_name="Acme",
            outgoing_email="sales@example.com",
            outgoing_email_app_password="secret",
            purchase_department_emails="purchase@example.com",
        )
        self.client.force_login(self.owner)
        self.p1 = Product.objects.create(
            company=self.company,
            name="Laptop",
            category="Hardware",
            company_name="Dell",
        )
        self.p2 = Product.objects.create(
            company=self.company,
            name="Switch",
            category="Hardware",
            company_name="Cisco",
        )
        self.v1 = Vendor.objects.create(
            company=self.company, name="Vendor A", email="a@example.com"
        )
        self.v2 = Vendor.objects.create(
            company=self.company, name="Vendor B", email="b@example.com"
        )
        self.v3 = Vendor.objects.create(
            company=self.company, name="Vendor C", email="c@example.com"
        )

    def _create_deal(self, send_ok=True):
        with patch(
            "business.procurement.send_outbound", return_value=(send_ok, None)
        ) as mocked:
            result = create_deal(
                self.company,
                [
                    {"product": self.p1, "quantity": 2},
                    {"product": self.p2, "quantity": 1},
                ],
                [self.v1.pk, self.v2.pk, self.v3.pk],
                expiration_days=1,
            )
        return result, mocked

    def test_create_deal_sends_unique_links_and_round_one(self):
        result, mocked = self._create_deal()
        deal_no = result["deal_no"]
        self.assertEqual(ProcurementDeal.objects.filter(deal_no=deal_no).count(), 2)
        self.assertEqual(DealVendor.objects.filter(deal_no=deal_no).count(), 3)
        round_obj = DealRound.objects.get(deal_no=deal_no, round_number=1)
        self.assertEqual(round_obj.name, "Round 1")
        tokens = list(
            VendorPriceSubmission.objects.filter(deal_no=deal_no).values_list(
                "submission_token", flat=True
            )
        )
        self.assertEqual(len(tokens), 3)
        self.assertEqual(len(set(tokens)), 3)
        self.assertEqual(mocked.call_count, 3)
        subjects = [call.args[3] for call in mocked.call_args_list]
        self.assertTrue(all(s == f"Quotation Request for Deal No. {deal_no}" for s in subjects))

    def test_vendor_submit_shows_on_detail_others_empty(self):
        result, _ = self._create_deal()
        deal_no = result["deal_no"]
        sub_a = VendorPriceSubmission.objects.get(deal_no=deal_no, vendor=self.v1)
        submit_vendor_prices(
            sub_a,
            [
                {"product": self.p1, "price": Decimal("100.00"), "note": ""},
                {"product": self.p2, "price": Decimal("50.00"), "note": ""},
            ],
        )
        response = self.client.get(reverse("procurement_detail", args=[deal_no]))
        self.assertContains(response, "Vendor A")
        self.assertContains(response, "₹100.00")
        self.assertContains(response, "Vendor B")
        body = response.content.decode()
        self.assertIn("Vendor B", body)
        prices_b = DealRoundVendorPrice.objects.filter(
            deal_round__deal_no=deal_no, vendor=self.v2
        )
        self.assertFalse(prices_b.exists())

    def test_resend_expires_unused_keeps_submitted(self):
        result, _ = self._create_deal()
        deal_no = result["deal_no"]
        sub_a = VendorPriceSubmission.objects.get(deal_no=deal_no, vendor=self.v1)
        submit_vendor_prices(
            sub_a,
            [
                {"product": self.p1, "price": Decimal("100.00"), "note": ""},
                {"product": self.p2, "price": Decimal("50.00"), "note": ""},
            ],
        )
        old_b = VendorPriceSubmission.objects.get(deal_no=deal_no, vendor=self.v2)
        old_token_b = old_b.submission_token
        with patch("business.procurement.send_outbound", return_value=(True, None)):
            self.client.post(
                reverse("procurement_detail", args=[deal_no]),
                {"action": "resend_round", "round_id": old_b.deal_round_id},
            )
        old_b.refresh_from_db()
        self.assertTrue(old_b.is_expired())
        self.assertEqual(old_b.submission_token, old_token_b)
        new_b = (
            VendorPriceSubmission.objects.filter(deal_no=deal_no, vendor=self.v2)
            .exclude(pk=old_b.pk)
            .get()
        )
        self.assertFalse(new_b.is_expired())
        sub_a.refresh_from_db()
        self.assertTrue(sub_a.is_submitted)
        self.assertFalse(
            VendorPriceSubmission.objects.filter(
                deal_no=deal_no, vendor=self.v1
            ).exclude(pk=sub_a.pk).exists()
        )

    def test_add_round_followup_hides_vendor_names(self):
        result, _ = self._create_deal()
        deal_no = result["deal_no"]
        sub_a = VendorPriceSubmission.objects.get(deal_no=deal_no, vendor=self.v1)
        submit_vendor_prices(
            sub_a,
            [
                {"product": self.p1, "price": Decimal("100.00"), "note": ""},
                {"product": self.p2, "price": Decimal("50.00"), "note": ""},
            ],
        )
        with patch(
            "business.procurement.send_outbound", return_value=(True, None)
        ) as mocked:
            self.client.post(
                reverse("procurement_detail", args=[deal_no]),
                {"action": "add_round"},
            )
        self.assertTrue(DealRound.objects.filter(deal_no=deal_no, round_number=2).exists())
        html = mocked.call_args_list[0].kwargs["html_body"]
        self.assertIn("Lowest quoted prices", html)
        self.assertIn("Vendor names are not disclosed", html)
        self.assertNotIn("Vendor A", html)

    def test_mark_round_two_final_clears_previous(self):
        result, _ = self._create_deal()
        deal_no = result["deal_no"]
        round1 = DealRound.objects.get(deal_no=deal_no, round_number=1)
        mark_round_final(self.company, deal_no, round1)
        with patch("business.procurement.send_outbound", return_value=(True, None)):
            self.client.post(
                reverse("procurement_detail", args=[deal_no]),
                {"action": "add_round"},
            )
        round2 = DealRound.objects.get(deal_no=deal_no, round_number=2)
        self.client.post(
            reverse("procurement_detail", args=[deal_no]),
            {"action": "mark_final", "round_id": round2.pk},
        )
        round1.refresh_from_db()
        round2.refresh_from_db()
        self.assertFalse(round1.is_final)
        self.assertTrue(round2.is_final)

    def test_compare_ranks_lowest_total_first(self):
        result, _ = self._create_deal()
        deal_no = result["deal_no"]
        round_obj = DealRound.objects.get(deal_no=deal_no)
        for vendor, p1, p2 in (
            (self.v1, "80", "20"),
            (self.v2, "10", "10"),
            (self.v3, "50", "50"),
        ):
            sub = VendorPriceSubmission.objects.get(deal_no=deal_no, vendor=vendor)
            submit_vendor_prices(
                sub,
                [
                    {"product": self.p1, "price": Decimal(p1), "note": ""},
                    {"product": self.p2, "price": Decimal(p2), "note": ""},
                ],
            )
        ranked = compare_rows_for_round(self.company, deal_no, round_obj)
        self.assertEqual(ranked[0]["vendor"], self.v2)
        self.assertEqual(ranked[0]["rank"], 1)
        self.assertEqual(ranked[1]["vendor"], self.v3)
        self.assertEqual(ranked[2]["vendor"], self.v1)

    def test_export_pdf_and_purchase_email(self):
        result, _ = self._create_deal()
        deal_no = result["deal_no"]
        response = self.client.get(
            reverse("procurement_compare_pdf", args=[deal_no])
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertIn(f"Compare_Deals_{deal_no}.pdf", response["Content-Disposition"])
        with patch(
            "business.procurement.send_outbound", return_value=(True, None)
        ) as mocked:
            post = self.client.post(
                reverse("procurement_compare_result", args=[deal_no]),
                {"action": "send_purchase"},
                follow=True,
            )
        self.assertContains(post, "Sent to Purchase Department successfully!")
        self.assertEqual(mocked.call_count, 1)
        self.assertEqual(mocked.call_args.args[2], ["purchase@example.com"])
        self.assertEqual(
            mocked.call_args.args[3],
            f"Deal Comparison - Deal Number: {deal_no}",
        )
        attachments = mocked.call_args.kwargs["attachments"]
        self.assertEqual(attachments[0][0], f"Deal_Comparison_{deal_no}.pdf")

    def test_purchase_missing_email(self):
        self.company.purchase_department_emails = ""
        self.company.save(update_fields=["purchase_department_emails"])
        result, _ = self._create_deal()
        deal_no = result["deal_no"]
        response = self.client.post(
            reverse("procurement_compare_result", args=[deal_no]),
            {"action": "send_purchase"},
            follow=True,
        )
        self.assertContains(
            response,
            "Your company does not have a purchase department email set. Please add one.",
        )

    def test_public_token_states(self):
        result, _ = self._create_deal()
        deal_no = result["deal_no"]
        sub = VendorPriceSubmission.objects.get(deal_no=deal_no, vendor=self.v1)
        url = reverse("vendor_price_submit", args=[sub.submission_token])
        response = self.client.get(url)
        self.assertContains(response, "Price Submission")
        self.assertContains(response, "Vendor A")
        self.client.logout()
        response = self.client.get(url)
        self.assertContains(response, "Enter your unit prices")
        submit_url = reverse("vendor_price_submit_post", args=[sub.submission_token])
        posted = self.client.post(
            submit_url,
            {
                f"price_{self.p1.pk}": "12.50",
                f"price_{self.p2.pk}": "9.00",
            },
        )
        self.assertContains(posted, "Prices submitted successfully")
        again = self.client.get(url)
        self.assertContains(again, "Prices already submitted")
        sub_b = VendorPriceSubmission.objects.get(deal_no=deal_no, vendor=self.v2)
        sub_b.expires_at = timezone.now() - timedelta(seconds=5)
        sub_b.save(update_fields=["expires_at"])
        expired = self.client.get(
            reverse("vendor_price_submit", args=[sub_b.submission_token])
        )
        self.assertContains(expired, "Link Expired")
        invalid = self.client.get(
            reverse(
                "vendor_price_submit",
                args=["11111111-1111-1111-1111-111111111111"],
            )
        )
        self.assertContains(invalid, "Error", status_code=404)

    def test_permissions(self):
        User = get_user_model()
        member = User.objects.create_user("staff@example.com", "pass")
        CompanyMember.objects.create(
            company=self.company, user=member, sales_access=False
        )
        self.client.force_login(member)
        list_resp = self.client.get(reverse("procurement_list"))
        self.assertEqual(list_resp.status_code, 302)
        create_resp = self.client.get(reverse("procurement_create"))
        self.assertEqual(create_resp.status_code, 302)
        vendor_resp = self.client.get(reverse("vendor_list"))
        self.assertEqual(vendor_resp.status_code, 302)

    def test_create_view_and_email_failure_keeps_deal(self):
        self.v1.catalog_companies = ["Dell", "Cisco"]
        self.v1.save(update_fields=["catalog_companies"])
        self.v2.catalog_companies = ["Dell", "Cisco"]
        self.v2.save(update_fields=["catalog_companies"])
        self.v3.catalog_companies = ["Dell", "Cisco"]
        self.v3.save(update_fields=["catalog_companies"])
        with patch("business.procurement.send_outbound", return_value=(False, "boom")):
            response = self.client.post(
                reverse("procurement_create"),
                {
                    "products_json": (
                        f'[{{"product_id": {self.p1.pk}, "quantity": 1}},'
                        f'{{"product_id": {self.p2.pk}, "quantity": 2}}]'
                    ),
                    "vendor_ids": [self.v1.pk, self.v2.pk, self.v3.pk],
                    "expiration_days": 1,
                },
                follow=True,
            )
        self.assertContains(
            response,
            "Deal created, but price submission links were not sent",
        )
        self.assertTrue(ProcurementDeal.objects.filter(company=self.company).exists())

    def test_delete_removes_sibling_rows(self):
        result, _ = self._create_deal()
        deal_no = result["deal_no"]
        self.client.post(reverse("procurement_list"), {"deal_nos": [deal_no]})
        self.assertFalse(ProcurementDeal.objects.filter(deal_no=deal_no).exists())
        self.assertFalse(DealVendor.objects.filter(deal_no=deal_no).exists())
        self.assertFalse(DealRound.objects.filter(deal_no=deal_no).exists())
        self.assertFalse(
            VendorPriceSubmission.objects.filter(deal_no=deal_no).exists()
        )

    def test_deal_number_daily_counter(self):
        first = next_deal_no(self.company)
        ProcurementDeal.objects.create(
            company=self.company,
            deal_no=first,
            product=self.p1,
            quantity=1,
        )
        second = next_deal_no(self.company)
        self.assertNotEqual(first, second)
        self.assertEqual(first[:8], second[:8])
        self.assertEqual(int(second[-4:]), int(first[-4:]) + 1)

    def test_sidebar_has_procurement_not_scrap(self):
        response = self.client.get(reverse("procurement_list"))
        self.assertContains(response, "Procurement")
        self.assertNotContains(response, "Create Scrap Deal")
        self.assertNotContains(response, "All Branches")

    def test_other_vendor_is_saved_and_gets_quotation(self):
        self.v1.catalog_companies = ["Dell", "Cisco"]
        self.v1.save(update_fields=["catalog_companies"])
        self.v2.catalog_companies = []
        self.v2.save(update_fields=["catalog_companies"])
        page = self.client.get(reverse("procurement_create"))
        self.assertContains(page, "Other vendors")
        self.assertContains(page, "other-list")
        with patch("business.procurement.send_outbound", return_value=(True, None)) as mocked:
            response = self.client.post(
                reverse("procurement_create"),
                {
                    "products_json": (
                        f'[{{"product_id": {self.p1.pk}, "quantity": 1}},'
                        f'{{"product_id": {self.p2.pk}, "quantity": 2}}]'
                    ),
                    "vendor_ids": [self.v1.pk, self.v2.pk],
                    "expiration_days": 1,
                },
                follow=True,
            )
        self.assertContains(response, "Deal created")
        self.assertTrue(DealVendor.objects.filter(company=self.company, vendor=self.v2).exists())
        self.assertTrue(
            VendorPriceSubmission.objects.filter(company=self.company, vendor=self.v2).exists()
        )
        self.assertEqual(mocked.call_count, 2)

    def test_vendors_for_products_requires_all_selected_companies(self):
        extra = Product.objects.create(
            company=self.company,
            name="Another Dell laptop",
            category="Hardware",
            company_name="Dell",
        )
        self.v1.catalog_companies = ["Dell", "Cisco"]
        self.v1.save(update_fields=["catalog_companies"])
        self.v2.catalog_companies = ["Dell"]
        self.v2.save(update_fields=["catalog_companies"])
        both = list(vendors_for_products(self.company, [self.p1.pk, self.p2.pk]))
        self.assertEqual([vendor.pk for vendor in both], [self.v1.pk])
        dell_only = list(
            vendors_for_products(self.company, [self.p1.pk, extra.pk])
        )
        self.assertEqual(
            [vendor.pk for vendor in dell_only],
            [self.v1.pk, self.v2.pk],
        )

    def test_vendor_add_and_edit_save_catalog_companies(self):
        created = self.client.post(
            reverse("vendor_add"),
            {"name": "Mapped Vendor", "catalog_companies": ["Dell", "Cisco"]},
        )
        self.assertEqual(created.status_code, 302)
        vendor = Vendor.objects.get(company=self.company, name="Mapped Vendor")
        self.assertEqual(set(vendor.catalog_companies), {"Dell", "Cisco"})
        add_page = self.client.get(reverse("vendor_add"))
        self.assertContains(add_page, "Product company")
        self.assertContains(add_page, "Dell")
        edit_page = self.client.get(reverse("vendor_edit", args=[vendor.pk]))
        self.assertContains(edit_page, "Product company")
        self.assertContains(edit_page, "checked")
        updated = self.client.post(
            reverse("vendor_edit", args=[vendor.pk]),
            {"name": "Mapped Vendor", "catalog_companies": ["Dell"]},
        )
        self.assertEqual(updated.status_code, 302)
        vendor.refresh_from_db()
        self.assertEqual(vendor.catalog_companies, ["Dell"])
