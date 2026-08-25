from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from business.expiry_notifications import (
    build_renewal_quote_items,
    send_renewal_quotation_email,
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
