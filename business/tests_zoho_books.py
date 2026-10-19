from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from accounts.models import CompanyProfile
from business.forms import ZohoSubscriptionLineForm
from business.models import (
    Client,
    ClientSubscription,
    Product,
    Vendor,
    ZohoBooksDocument,
    ZohoBooksDocumentLine,
)
from business.zoho_books import (
    SESSION_INVOICE_IDS,
    build_review_rows,
    invoice_item_rows,
    invoice_period_range,
    list_invoices_for_items,
    mark_imported_invoices,
)


def _invoice():
    return {
        "invoice_id": "100",
        "invoice_number": "INV-102",
        "date": "2026-01-15",
        "customer_name": "Zoho Customer",
        "gst_no": "27 ABCDE1234F1Z5",
        "line_items": [
            {
                "line_item_id": "1",
                "name": "Microsoft 365",
                "sku": "m365",
                "quantity": 2,
                "item_total": "1000.50",
            },
            {
                "line_item_id": "2",
                "name": "Backup",
                "sku": "",
                "quantity": 1,
                "item_total": 500,
            },
            {
                "line_item_id": "3",
                "name": "Unknown tool",
                "sku": "",
                "quantity": 4,
                "item_total": 80,
                "serial_numbers": ["SN-9"],
            },
        ],
    }


class ZohoBooksDraftTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("zoho-owner@example.com", "pass")
        self.company = CompanyProfile.objects.create(
            user=self.user,
            company_name="Zoho Co",
            zoho_refresh_token="refresh-token",
            zoho_books_organization_id="999",
        )
        self.client_obj = Client.objects.create(
            company=self.company,
            name="Stored Client",
            gst_number="27ABCDE1234F1Z5",
        )
        self.microsoft = Product.objects.create(
            company=self.company,
            name="Microsoft 365",
            sku="M365",
            category="Software",
            company_name="Microsoft",
        )
        self.backup = Product.objects.create(company=self.company, name="Backup")
        self.vendor = Vendor.objects.create(company=self.company, name="Redington")
        self.microsoft.vendor = self.vendor
        self.microsoft.save(update_fields=["vendor"])

    def test_one_invoice_with_three_products_builds_three_rows(self):
        rows = build_review_rows(self.company, [_invoice()])

        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["client_id"], self.client_obj.pk)
        self.assertEqual(rows[0]["product_id"], self.microsoft.pk)
        self.assertEqual(rows[0]["vendor_id"], self.vendor.pk)
        self.assertEqual(rows[0]["quantity"], 2)
        self.assertEqual(rows[0]["billing_amount"], Decimal("1000.50"))
        self.assertEqual(rows[0]["start_date"], date(2026, 1, 15))
        self.assertEqual(rows[0]["end_date"], date(2027, 1, 15))
        self.assertEqual(rows[0]["invoice_number"], "INV-102")
        self.assertEqual(rows[0]["category"], "Software")
        self.assertEqual(rows[0]["company_name"], "Microsoft")
        self.assertIn("GST", rows[0]["client_note"])
        self.assertIn("SKU", rows[0]["product_note"])
        self.assertEqual(rows[1]["product_id"], self.backup.pk)
        self.assertIsNone(rows[2]["product_id"])
        self.assertEqual(rows[2]["serial_number"], "SN-9")
        self.assertFalse(any(row["already_imported"] for row in rows))

    def test_duration_in_description_sets_start_and_end(self):
        invoice = _invoice()
        invoice["date"] = "2026-10-08"
        invoice["line_items"][0]["description"] = (
            "Pan of Company Is AABCR0347P\nDuration: 09-10-2026 TO 08-10-2027"
        )
        invoice["line_items"][1]["description"] = "No period on this line."

        rows = build_review_rows(self.company, [invoice])

        self.assertEqual(rows[0]["start_date"], date(2026, 10, 9))
        self.assertEqual(rows[0]["end_date"], date(2027, 10, 8))
        self.assertEqual(rows[1]["start_date"], date(2026, 10, 8))
        self.assertEqual(rows[1]["end_date"], date(2027, 10, 8))

    def test_serial_in_description_fills_serial_number(self):
        invoice = _invoice()
        invoice["line_items"][0]["description"] = (
            "HP WORKSTATION Z2 TWR\n"
            "I9 14900K/32 GB\n"
            "S/N : HPWS 22G9\n"
            "SKU: HPWS 22G9"
        )
        invoice["line_items"][1]["description"] = (
            "NVIDIA RTX 2000 ADA 16GB GRAPHIC CARD\n"
            "S/N:- 1421826001569\n"
            "SKU: RTX 2000 ADA"
        )
        invoice["line_items"][1]["serial_numbers"] = ["API-SN"]
        invoice["line_items"][2]["description"] = "No serial written here."

        rows = build_review_rows(self.company, [invoice])

        self.assertEqual(rows[0]["serial_number"], "HPWS 22G9")
        self.assertEqual(rows[1]["serial_number"], "1421826001569")
        self.assertEqual(rows[2]["serial_number"], "SN-9")

    def test_imported_line_is_marked(self):
        ClientSubscription.objects.create(
            company=self.company,
            client=self.client_obj,
            product=self.microsoft,
            start_date=date(2026, 1, 15),
            zoho_invoice_id="100",
            zoho_line_item_id="1",
        )

        rows = build_review_rows(self.company, [_invoice()])

        self.assertTrue(rows[0]["already_imported"])
        self.assertFalse(rows[1]["already_imported"])

    def test_invoice_list_marks_invoices_that_already_created_subscriptions(self):
        ClientSubscription.objects.create(
            company=self.company,
            client=self.client_obj,
            product=self.microsoft,
            start_date=date(2026, 1, 15),
            zoho_invoice_id="100",
            zoho_line_item_id="1",
        )
        invoices = [
            {"invoice_id": "100", "invoice_number": "INV-102"},
            {"invoice_id": 200, "invoice_number": "INV-200"},
        ]

        mark_imported_invoices(self.company, invoices)

        self.assertTrue(invoices[0]["subscription_added"])
        self.assertFalse(invoices[1]["subscription_added"])

    def test_closest_client_name_ignores_case_and_pvt_ltd(self):
        client = Client.objects.create(
            company=self.company,
            name="Inovic CRM India Pvt Ltd",
        )
        invoice = _invoice()
        invoice["gst_no"] = ""
        invoice["customer_name"] = "INOVIC CRM INDIA PRIVATE LIMITED"

        rows = build_review_rows(self.company, [invoice])

        self.assertEqual(rows[0]["client_id"], client.pk)
        self.assertIn("name", rows[0]["client_note"].lower())

    def test_unrelated_customer_name_leaves_client_blank(self):
        invoice = _invoice()
        invoice["gst_no"] = ""
        invoice["customer_name"] = "Totally Different Exports"

        rows = build_review_rows(self.company, [invoice])

        self.assertIsNone(rows[0]["client_id"])

    def test_equally_close_client_names_leave_client_blank(self):
        Client.objects.create(company=self.company, name="Foo Pvt Ltd")
        Client.objects.create(company=self.company, name="Foo Private Limited")
        invoice = _invoice()
        invoice["gst_no"] = ""
        invoice["customer_name"] = "FOO PRIVATE LIMITED"

        rows = build_review_rows(self.company, [invoice])

        self.assertIsNone(rows[0]["client_id"])

    def test_category_dropdown_includes_hardware(self):
        form = ZohoSubscriptionLineForm(company=self.company)
        labels = [label for _value, label in form.fields["category_filter"].choices]

        self.assertIn("Hardware", labels)
        self.assertIn("Software", labels)


class ZohoBooksImportViewTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("zoho-view@example.com", "pass")
        self.company = CompanyProfile.objects.create(
            user=self.user,
            company_name="Zoho View Co",
            zoho_refresh_token="refresh-token",
            zoho_books_organization_id="999",
        )
        self.client_obj = Client.objects.create(
            company=self.company,
            name="Stored Client",
            gst_number="27ABCDE1234F1Z5",
        )
        self.microsoft = Product.objects.create(
            company=self.company, name="Microsoft 365", sku="M365"
        )
        self.backup = Product.objects.create(company=self.company, name="Backup")
        self.other = Product.objects.create(company=self.company, name="Other plan")
        self.http = self.client
        self.http.force_login(self.user)

    def test_subscription_list_links_to_zoho_books(self):
        response = self.http.get(reverse("subscription_list"))
        self.assertContains(response, reverse("subscription_zoho_import"))

    def test_import_page_asks_to_connect_when_zoho_is_missing(self):
        self.company.zoho_refresh_token = ""
        self.company.save(update_fields=["zoho_refresh_token"])
        response = self.http.get(reverse("subscription_zoho_import"))
        self.assertContains(response, "Zoho is not connected")

    @patch("business.views_zoho_books.get_invoice_pdf", return_value=b"%PDF-1.4 test")
    @patch("business.views_zoho_books.get_invoice")
    def test_review_save_creates_one_subscription_per_product(self, get_invoice, _pdf):
        get_invoice.return_value = _invoice()
        session = self.http.session
        session[SESSION_INVOICE_IDS] = ["100"]
        session.save()

        review = self.http.get(reverse("subscription_zoho_review"))
        self.assertContains(review, "Unknown tool")
        self.assertContains(review, "INV-102")
        self.assertContains(review, "Basic Subscription Details")
        self.assertContains(review, "Category")
        self.assertContains(review, "View invoice")

        data = {
            "form-TOTAL_FORMS": "3",
            "form-INITIAL_FORMS": "3",
            "form-MIN_NUM_FORMS": "0",
            "form-MAX_NUM_FORMS": "1000",
        }
        lines = [
            ("1", self.microsoft.pk, 2, "1000.50", ""),
            ("2", self.backup.pk, 1, "500.00", ""),
            ("3", self.other.pk, 4, "80.00", "SN-9"),
        ]
        for index, (line_id, product_id, qty, price, serial) in enumerate(lines):
            data.update(
                {
                    f"form-{index}-include": "on",
                    f"form-{index}-zoho_invoice_id": "100",
                    f"form-{index}-zoho_line_item_id": line_id,
                    f"form-{index}-client": str(self.client_obj.pk),
                    f"form-{index}-product": str(product_id),
                    f"form-{index}-quantity": str(qty),
                    f"form-{index}-start_date": "2026-01-15",
                    f"form-{index}-end_date": "2027-01-15",
                    f"form-{index}-vendor": "",
                    f"form-{index}-billing_amount": price,
                    f"form-{index}-invoice_number": "INV-102",
                    f"form-{index}-serial_number": serial,
                    f"form-{index}-subscription_code": "",
                }
            )

        response = self.http.post(reverse("subscription_zoho_review"), data)
        self.assertRedirects(response, reverse("subscription_list"))
        created = list(
            ClientSubscription.objects.filter(company=self.company).order_by(
                "zoho_line_item_id"
            )
        )
        self.assertEqual(len(created), 3)
        self.assertEqual(
            [item.product_id for item in created],
            [self.microsoft.pk, self.backup.pk, self.other.pk],
        )
        self.assertEqual({item.invoice_number for item in created}, {"INV-102"})
        self.assertEqual({item.client_id for item in created}, {self.client_obj.pk})
        self.assertEqual(created[0].quantity, 2)
        self.assertEqual(created[0].billing_amount, Decimal("1000.50"))
        self.assertEqual(created[0].end_date, date(2027, 1, 15))
        self.assertTrue(created[0].invoice_pdf.name.endswith(".pdf"))
        self.assertEqual(created[2].serial_number, "SN-9")

        session = self.http.session
        session[SESSION_INVOICE_IDS] = ["100"]
        session.save()
        again = self.http.post(reverse("subscription_zoho_review"), data)
        self.assertRedirects(again, reverse("subscription_list"))
        self.assertEqual(
            ClientSubscription.objects.filter(company=self.company).count(),
            3,
        )


class ZohoBooksInvoiceFilterTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("zoho-filter@example.com", "pass")
        self.company = CompanyProfile.objects.create(
            user=self.user,
            company_name="Zoho Filter Co",
            zoho_refresh_token="refresh-token",
            zoho_books_organization_id="999",
        )
        self.http = self.client
        self.http.force_login(self.user)

    def test_invoice_item_rows_use_the_product_name(self):
        rows = invoice_item_rows(_invoice())

        self.assertEqual(
            [(row["name"], row["quantity"]) for row in rows],
            [("Microsoft 365", 2), ("Backup", 1), ("Unknown tool", 4)],
        )
        self.assertEqual(rows[0]["amount"], "1000.50")

    @patch("business.zoho_books.get_access_token", return_value="token")
    @patch("business.zoho_books.list_invoices")
    def test_selected_products_return_each_matching_invoice_once(self, list_invoices, _token):
        def side_effect(*_args, **kwargs):
            item_id = kwargs.get("item_id")
            if kwargs.get("page") != 1:
                return [], {"has_more_page": False}
            if item_id == "11":
                return [
                    {"invoice_id": "1", "date": "2026-02-01", "invoice_number": "A"},
                    {"invoice_id": "2", "date": "2026-01-01", "invoice_number": "B"},
                ], {"has_more_page": False}
            return [
                {"invoice_id": "2", "date": "2026-01-01", "invoice_number": "B"},
                {"invoice_id": "3", "date": "2026-03-01", "invoice_number": "C"},
            ], {"has_more_page": False}

        list_invoices.side_effect = side_effect

        invoices, page_context = list_invoices_for_items(
            self.company,
            "999",
            ["11", "22"],
            page=1,
        )

        self.assertEqual([item["invoice_id"] for item in invoices], ["3", "1", "2"])
        self.assertFalse(page_context["has_more_page"])

    @patch("business.views_zoho_books.resolve_organization")
    def test_import_page_shows_a_loading_state(self, resolve_organization):
        resolve_organization.return_value = {
            "organization_id": "999",
            "name": "Natraj",
        }

        page = self.http.get(reverse("subscription_zoho_import"))

        self.assertContains(page, "Loading saved invoices")
        self.assertContains(page, "Sync from Zoho")
        self.assertContains(page, "zoho-spinner")
        self.assertContains(page, "Opening ")
        self.assertNotContains(page, "Review selected invoice")
        self.assertNotContains(page, 'type="radio" name="invoice_ids"')

    def _store_invoice(self, zoho_id, number, day, item_id="11"):
        document = ZohoBooksDocument.objects.create(
            company=self.company,
            organization_id="999",
            kind="invoice",
            zoho_id=zoho_id,
            number=number,
            document_date=day,
            party_name="Avi",
            status="draft",
            currency_code="INR",
            total=Decimal("10.00"),
            last_modified_raw="2026-02-01T00:00:00+0000",
        )
        ZohoBooksDocumentLine.objects.create(
            document=document,
            line_id=f"line-{zoho_id}",
            item_id=item_id,
            name="Microsoft 365",
            sku="M365",
            quantity=1,
            amount=Decimal("10.00"),
        )
        return document

    @patch("business.zoho_books.books_get")
    def test_saved_invoices_load_without_calling_zoho(self, books_get):
        self._store_invoice("1", "GST/1", date(2026, 1, 1))

        first = self.http.get(reverse("subscription_zoho_invoice_data"))
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()["products"][0]["name"], "Microsoft 365")
        self.assertEqual(first.json()["documents"][0]["items"][0]["name"], "Microsoft 365")

        second = self.http.get(reverse("subscription_zoho_invoice_data"))
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()["count"], 1)
        books_get.assert_not_called()

    def test_year_range_is_the_whole_year_and_month_is_that_month(self):
        self.assertEqual(invoice_period_range("2026"), ("2026-01-01", "2026-12-31"))
        self.assertEqual(invoice_period_range("2026", "10"), ("2026-10-01", "2026-10-31"))
        self.assertIsNone(invoice_period_range("", "10"))

    @patch("business.zoho_books.books_get")
    def test_year_filter_reads_saved_invoices(self, books_get):
        self._store_invoice("1", "GST/1", date(2026, 2, 1))
        self._store_invoice("2", "GST/2", date(2025, 2, 1))

        response = self.http.get(reverse("subscription_zoho_invoice_data"), {"year": "2026"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["count"], 1)
        self.assertEqual(response.json()["documents"][0]["number"], "GST/1")
        books_get.assert_not_called()

    @patch("business.zoho_books.books_get")
    def test_month_filter_and_product_search_read_saved_rows(self, books_get):
        self._store_invoice("1", "GST/1", date(2026, 10, 2), item_id="11")
        self._store_invoice("2", "GST/2", date(2026, 11, 2), item_id="22")

        month = self.http.get(
            reverse("subscription_zoho_invoice_data"),
            {"year": "2026", "month": "10"},
        )
        self.assertEqual(month.json()["count"], 1)
        self.assertEqual(month.json()["documents"][0]["number"], "GST/1")

        product = self.http.get(
            reverse("subscription_zoho_invoice_data"),
            {"item_id": "22"},
        )
        self.assertEqual(product.json()["count"], 1)
        self.assertEqual(product.json()["documents"][0]["number"], "GST/2")

        number = self.http.get(reverse("subscription_zoho_invoice_data"), {"q": "GST/1"})
        self.assertEqual([row["number"] for row in number.json()["documents"]], ["GST/1"])
        books_get.assert_not_called()

    @patch("business.zoho_books.books_get")
    def test_filters_combine_and_an_empty_month_is_not_unsynced(self, books_get):
        self._store_invoice("1", "GST/2026/1", date(2026, 1, 5), item_id="11")
        self._store_invoice("2", "GST/2025/1", date(2025, 1, 8), item_id="22")
        self._store_invoice("3", "GST/2026/7", date(2026, 7, 2), item_id="11")

        january = self.http.get(reverse("subscription_zoho_invoice_data"), {"month": "1"})
        self.assertEqual(
            sorted(row["number"] for row in january.json()["documents"]),
            ["GST/2025/1", "GST/2026/1"],
        )
        self.assertTrue(january.json()["ever_synced"])

        together = self.http.get(
            reverse("subscription_zoho_invoice_data"),
            {"year": "2026", "month": "1", "item_id": "11", "q": "GST/2026"},
        )
        self.assertEqual([row["number"] for row in together.json()["documents"]], ["GST/2026/1"])

        empty = self.http.get(
            reverse("subscription_zoho_invoice_data"),
            {"year": "2027", "month": "1"},
        )
        self.assertEqual(empty.json()["count"], 0)
        self.assertTrue(empty.json()["ever_synced"])
        books_get.assert_not_called()

    @patch("business.zoho_books.get_document")
    @patch("business.zoho_books.list_documents_page")
    def test_sync_saves_products_and_later_sync_skips_unchanged(self, list_page, get_document):
        summary = {
            "invoice_id": "1",
            "invoice_number": "GST/1",
            "date": "2026-02-01",
            "customer_name": "Avi",
            "status": "sent",
            "total": "10",
            "currency_code": "INR",
            "last_modified_time": "2026-02-01T10:00:00+0000",
        }
        list_page.return_value = ([summary], {"has_more_page": False})
        get_document.return_value = {
            **summary,
            "gst_no": "27ABCDE1234F1Z5",
            "line_items": [
                {
                    "line_item_id": "9",
                    "item_id": "11",
                    "name": "Microsoft 365",
                    "sku": "M365",
                    "quantity": 2,
                    "item_total": "10.50",
                    "description": "Duration: 09-10-2026 TO 08-10-2027",
                }
            ],
        }

        first = self.http.post(
            reverse("subscription_zoho_sync"),
            {"kind": "invoice", "start": "1"},
        )
        self.assertEqual(first.status_code, 200)
        self.assertFalse(first.json()["has_more"])
        self.assertEqual(first.json()["stored"], 1)
        self.assertEqual(get_document.call_count, 1)
        self.assertIsNone(list_page.call_args.kwargs["modified_after"])

        saved = self.http.get(reverse("subscription_zoho_invoice_data"))
        self.assertEqual(saved.json()["documents"][0]["items"][0]["name"], "Microsoft 365")
        self.assertEqual(get_document.call_count, 1)

        second = self.http.post(
            reverse("subscription_zoho_sync"),
            {"kind": "invoice", "start": "1"},
        )
        self.assertEqual(second.status_code, 200)
        self.assertIsNotNone(list_page.call_args.kwargs["modified_after"])
        self.assertEqual(get_document.call_count, 1)

    @patch("business.zoho_books.get_document")
    @patch("business.zoho_books.list_documents_page")
    def test_bill_sync_saves_the_vendor_bill(self, list_page, get_document):
        summary = {
            "bill_id": "50",
            "bill_number": "BILL-9",
            "date": "2026-03-01",
            "due_date": "2026-03-15",
            "vendor_name": "Redington",
            "status": "open",
            "total": "80",
            "balance": "80",
            "currency_code": "INR",
            "last_modified_time": "2026-03-01T10:00:00+0000",
        }
        list_page.return_value = ([summary], {"has_more_page": False})
        get_document.return_value = {
            **summary,
            "line_items": [
                {
                    "line_item_id": "4",
                    "item_id": "77",
                    "name": "Laptop",
                    "quantity": 1,
                    "item_total": "80",
                }
            ],
        }

        page = self.http.get(reverse("subscription_zoho_bills"))
        self.assertContains(page, "Purchase bills")
        self.assertContains(page, "Sync from Zoho")

        synced = self.http.post(
            reverse("subscription_zoho_sync"),
            {"kind": "bill", "start": "1"},
        )
        self.assertEqual(synced.status_code, 200)
        self.assertEqual(synced.json()["stored"], 1)

        data = self.http.get(reverse("subscription_zoho_bill_data"))
        row = data.json()["documents"][0]
        self.assertEqual(row["number"], "BILL-9")
        self.assertEqual(row["party_name"], "Redington")
        self.assertEqual(row["items"][0]["name"], "Laptop")
        self.assertEqual(get_document.call_count, 1)

    @patch("business.views_zoho_books.get_invoice")
    def test_plus_loads_the_products_on_one_invoice(self, get_invoice):
        get_invoice.return_value = _invoice()

        response = self.http.get(
            reverse("subscription_zoho_invoice_items", args=["100"])
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["items"][0]["name"], "Microsoft 365")
        self.assertEqual(len(payload["items"]), 3)
