from decimal import Decimal

from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from accounts.models import CompanyMember, CompanyProfile, User
from accounts.views import _format_price_input, _parse_unit_price
from business.models import Product


class ProductPriceParseTests(SimpleTestCase):
    def test_blank_becomes_zero(self):
        value, err = _parse_unit_price("  ")
        self.assertIsNone(err)
        self.assertEqual(str(value), "0.00")

    def test_accepts_currency_and_commas(self):
        value, err = _parse_unit_price("₹1,200.5")
        self.assertIsNone(err)
        self.assertEqual(str(value), "1200.50")

    def test_rejects_negative(self):
        value, err = _parse_unit_price("-10")
        self.assertIsNone(value)
        self.assertEqual(err, "Price cannot be negative.")

    def test_rejects_invalid(self):
        value, err = _parse_unit_price("abc")
        self.assertIsNone(value)
        self.assertEqual(err, "Enter a valid price.")

    def test_format_price_input(self):
        self.assertEqual(_format_price_input("1200"), "1200.00")


class ProductPricesViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="owner@example.com", password="pass12345"
        )
        self.company = CompanyProfile.objects.create(
            user=self.user, company_name="Acme"
        )
        CompanyMember.objects.update_or_create(
            company=self.company,
            user=self.user,
            defaults={
                "role": CompanyMember.Role.OWNER,
                "sales_access": True,
                "can_grant_sales": True,
            },
        )
        self.product = Product.objects.create(
            company=self.company, name="Antivirus", unit_price=Decimal("0")
        )
        self.client.login(email="owner@example.com", password="pass12345")

    def test_page_lists_products(self):
        response = self.client.get(reverse("company_product_prices"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Antivirus")
        self.assertContains(response, "Save prices")

    def test_saves_list_price(self):
        url = reverse("company_product_prices")
        response = self.client.post(
            url, {f"price_{self.product.pk}": "1,200.50"}, follow=True
        )
        self.assertEqual(response.status_code, 200)
        self.product.refresh_from_db()
        self.assertEqual(self.product.unit_price, Decimal("1200.50"))

    def test_company_setup_has_product_prices_button(self):
        response = self.client.get(reverse("company_setup"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse("company_product_prices"))
        self.assertContains(response, "Product prices")

