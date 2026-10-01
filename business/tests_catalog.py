"""Catalog and customer quotation tests."""

from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from accounts.models import CompanyMember, CompanyProfile
from business.catalog import (
    CatalogCategory,
    CatalogMasterCategory,
    CatalogProduct,
    CatalogSubcategory,
    CustomerQuotation,
)
from business.customer_quotation import create_quotation_from_procure, quotation_whatsapp_messages
from business.models import Lead

User = get_user_model()


class CatalogQuotationTests(TestCase):
    def setUp(self):
        user = User.objects.create_user(email="cat@example.com", password="x")
        self.company = CompanyProfile.objects.create(user=user, company_name="Cat Co")
        cat = CatalogCategory.objects.create(
            company=self.company,
            master_category=CatalogMasterCategory.HARDWARE,
            name="Laptop",
        )
        sub = CatalogSubcategory.objects.create(category=cat, name="Intel i5")
        self.priced = CatalogProduct.objects.create(
            subcategory=sub,
            brand="Dell",
            product_name="Latitude",
            model_number="5540",
            unit_price=Decimal("1000.00"),
        )
        self.unpriced = CatalogProduct.objects.create(
            subcategory=sub,
            brand="HP",
            product_name="EliteBook",
            model_number="840",
            unit_price=None,
        )
        self.lead = Lead.objects.create(
            company=self.company,
            contact_name="Buyer",
            source=Lead.Source.WHATSAPP,
            whatsapp_wa_id="919900000001",
        )

    def test_full_priced_quotation(self):
        lines = [
            {
                "catalog_product_id": self.priced.pk,
                "display_name": self.priced.display_label(),
                "quantity": 2,
                "master_category": "hardware",
                "category_name": "Laptop",
                "subcategory_name": "Intel i5",
                "unit_price": "1000.00",
            }
        ]
        _, q = create_quotation_from_procure(
            company=self.company,
            lead=self.lead,
            contact_name="Buyer",
            company_name="Acme",
            phone="919900000001",
            draft_lines=lines,
            whatsapp_wa_id="919900000001",
        )
        self.assertIsNotNone(q)
        assert q is not None
        self.assertEqual(q.subtotal, Decimal("2000.00"))
        self.assertFalse(q.has_unpriced_lines)
        self.assertTrue(q.pdf)

    def test_mixed_pricing_pending_message(self):
        lines = [
            {
                "catalog_product_id": self.priced.pk,
                "display_name": self.priced.display_label(),
                "quantity": 1,
                "master_category": "hardware",
                "category_name": "Laptop",
                "subcategory_name": "Intel i5",
                "unit_price": "1000.00",
            },
            {
                "catalog_product_id": self.unpriced.pk,
                "display_name": self.unpriced.display_label(),
                "quantity": 1,
                "master_category": "hardware",
                "category_name": "Laptop",
                "subcategory_name": "Intel i5",
                "unit_price": None,
            },
        ]
        _, q = create_quotation_from_procure(
            company=self.company,
            lead=self.lead,
            contact_name="Buyer",
            company_name="Acme",
            phone="919900000001",
            draft_lines=lines,
            whatsapp_wa_id="919900000001",
        )
        assert q is not None
        self.assertTrue(q.has_unpriced_lines)
        msgs = quotation_whatsapp_messages(q)
        self.assertTrue(any("team will contact" in m.lower() for m in msgs))


class CatalogHubAccessTests(TestCase):
    def setUp(self):
        user = User.objects.create_user(email="hub@example.com", password="pass12345")
        self.company = CompanyProfile.objects.create(user=user, company_name="Hub Co")
        CompanyMember.objects.update_or_create(
            company=self.company,
            user=user,
            defaults={"sales_access": True, "role": CompanyMember.Role.OWNER},
        )
        self.client.login(email="hub@example.com", password="pass12345")

    def test_catalog_hub_loads(self):
        response = self.client.get(reverse("catalog_hub"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Hardware")
