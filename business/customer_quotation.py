"""Build customer quotations from WhatsApp procure lines and send PDFs."""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

from accounts.models import CompanyProfile
from business.lead_ingest import create_lead_from_whatsapp
from business.models import Lead  # noqa: F401 — used via create_lead_from_whatsapp side effect

from .catalog import CatalogProduct, CustomerQuotation, CustomerQuotationLine
from .customer_quotation_pdf import build_customer_quotation_pdf

logger = logging.getLogger(__name__)

PENDING_PRICE_MSG = (
    "Some items need custom pricing. Our team will contact you shortly "
    "with the best prices."
)
FULL_QUOTE_MSG = "Please find your quotation summary below."
SUBMITTED_MSG = "Your requirement has been submitted."


def _line_from_draft(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "catalog_product_id": entry.get("catalog_product_id"),
        "display_name": entry.get("display_name") or "",
        "master_category": entry.get("master_category") or "",
        "category_name": entry.get("category_name") or "",
        "subcategory_name": entry.get("subcategory_name") or "",
        "quantity": int(entry.get("quantity") or 1),
        "unit_price": entry.get("unit_price"),
    }


def create_quotation_from_procure(
    *,
    company: CompanyProfile | None,
    lead: Lead,
    contact_name: str,
    company_name: str,
    phone: str,
    draft_lines: list[dict[str, Any]],
    whatsapp_wa_id: str,
) -> tuple[Lead, CustomerQuotation | None]:
    requirement_parts = []
    for entry in draft_lines:
        requirement_parts.append(
            f"- {entry.get('display_name')} x {entry.get('quantity')} "
            f"({entry.get('master_category')} / {entry.get('category_name')} / "
            f"{entry.get('subcategory_name')})"
        )
    requirement = "\n".join(requirement_parts)

    lead, _ = create_lead_from_whatsapp(
        company=company,
        contact_name=contact_name or lead.contact_name,
        company_name=company_name,
        phone=phone,
        requirement=requirement,
        product_ids=[],
        whatsapp_wa_id=whatsapp_wa_id,
        raw_payload={"whatsapp_flow": "procure", "lines": draft_lines},
    )

    if company is None:
        return lead, None

    quotation = CustomerQuotation.objects.create(
        company=company,
        lead=lead,
        customer_company_name=company_name,
        whatsapp_wa_id=whatsapp_wa_id,
    )
    subtotal = Decimal("0")
    has_unpriced = False

    for entry in draft_lines:
        parsed = _line_from_draft(entry)
        qty = parsed["quantity"]
        unit = parsed["unit_price"]
        unit_dec: Decimal | None = None
        line_total: Decimal | None = None
        if unit is not None and str(unit).strip() != "":
            try:
                unit_dec = Decimal(str(unit))
            except Exception:  # noqa: BLE001
                unit_dec = None
        if unit_dec is not None and unit_dec > 0:
            line_total = unit_dec * qty
            subtotal += line_total
        else:
            has_unpriced = True
            unit_dec = None
            line_total = None

        cat_id = parsed["catalog_product_id"]
        catalog_product = None
        if cat_id:
            catalog_product = CatalogProduct.objects.filter(pk=cat_id).first()

        CustomerQuotationLine.objects.create(
            quotation=quotation,
            catalog_product=catalog_product,
            master_category=parsed["master_category"],
            category_name=parsed["category_name"],
            subcategory_name=parsed["subcategory_name"],
            display_name=parsed["display_name"],
            quantity=qty,
            unit_price=unit_dec,
            line_total=line_total,
        )

    quotation.subtotal = subtotal
    quotation.has_unpriced_lines = has_unpriced
    if has_unpriced:
        quotation.status = CustomerQuotation.Status.PENDING_PRICING
    else:
        quotation.status = CustomerQuotation.Status.SENT

    pdf_bytes = build_customer_quotation_pdf(quotation)
    if pdf_bytes:
        from django.core.files.base import ContentFile

        quotation.pdf.save(
            f"quote_{quotation.pk}.pdf",
            ContentFile(pdf_bytes),
            save=False,
        )

    quotation.save()
    return lead, quotation


def quotation_whatsapp_messages(quotation: CustomerQuotation | None) -> list[str]:
    if quotation is None:
        return [SUBMITTED_MSG + " Our team will contact you soon."]
    if quotation.has_unpriced_lines:
        if quotation.subtotal > 0:
            return [
                f"{FULL_QUOTE_MSG} Subtotal for priced items: ₹{quotation.subtotal:,.2f}.",
                PENDING_PRICE_MSG,
            ]
        return [SUBMITTED_MSG, PENDING_PRICE_MSG]
    return [
        f"{FULL_QUOTE_MSG} Total: ₹{quotation.subtotal:,.2f}.",
        "Thank you — we will follow up if you have any questions.",
    ]
