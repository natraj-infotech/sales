"""Procurement deals: tokens, ranking, quotation emails, purchase-dept send."""

from __future__ import annotations

import logging
from datetime import timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from html import escape
from typing import Iterable

from django.conf import settings
from django.db import transaction
from django.db.models import Min
from django.urls import reverse
from django.utils import timezone

from accounts.models import CompanyProfile
from business.expiry_notifications import (
    _company_can_send_mail,
    _company_logo_inline,
    parse_extra_emails,
)
from business.models import (
    DealRound,
    DealRoundVendorPrice,
    DealVendor,
    DealVendorPrice,
    ProcurementDeal,
    Vendor,
    VendorPriceSubmission,
    VendorProductPrice,
)
from business.outbound_mail import send_outbound

logger = logging.getLogger(__name__)

CURRENCY = "₹"
ZERO = Decimal("0.00")

_FONT = "'Segoe UI', Arial, Helvetica, sans-serif"
_TEXT = "#1a1d21"
_MUTED = "#5c6370"
_BLUE = "#2b6fd6"
_HEADER_BG = "#2b6fd6"
_BORDER = "#e2e6eb"
_PAGE_BG = "#e8ecf1"
_CARD_BG = "#FFFFFF"
_TABLE_HEAD = "#eff6ff"

CREATE_SUCCESS = (
    "Deal created successfully and price submission links sent to vendors!"
)
CREATE_EMAIL_WARNING = (
    "Deal created, but price submission links were not sent to some or all vendors."
)
PURCHASE_MISSING = (
    "Your company does not have a purchase department email set. Please add one."
)


def product_company_label(product) -> str:
    return (getattr(product, "company_name", None) or "").strip()


def _as_money(value) -> Decimal:
    if value is None:
        return ZERO
    try:
        amount = Decimal(value)
    except (InvalidOperation, TypeError, ValueError):
        return ZERO
    return amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def format_money(value) -> str:
    return f"{CURRENCY}{_as_money(value):,.2f}"


def _public_base_url() -> str:
    base = (getattr(settings, "PUBLIC_BASE_URL", "") or "").strip().rstrip("/")
    if base:
        return base
    return "http://127.0.0.1:8000"


def submission_absolute_url(token) -> str:
    path = reverse("vendor_price_submit", kwargs={"token": str(token)})
    return f"{_public_base_url()}{path}"


def expiry_label(days: int) -> str:
    days = max(int(days or 1), 1)
    return "1 day" if days == 1 else f"{days} days"


def product_lines(company, deal_no):
    return list(
        ProcurementDeal.objects.filter(company=company, deal_no=deal_no)
        .select_related("product")
        .order_by("id")
    )


def first_line(company, deal_no):
    return (
        ProcurementDeal.objects.filter(company=company, deal_no=deal_no)
        .select_related("product")
        .order_by("id")
        .first()
    )


def invited_vendors(company, deal_no):
    return list(
        DealVendor.objects.filter(company=company, deal_no=deal_no)
        .select_related("vendor")
        .order_by("id")
    )


def vendors_for_products(company, product_ids):
    ids = [int(pk) for pk in product_ids]
    if not ids:
        return company.vendors.none()
    qs = company.vendors.all()
    for pk in ids:
        qs = qs.filter(catalog_products__pk=pk)
    return qs.distinct().order_by("name")


def deal_rounds(company, deal_no):
    return list(
        DealRound.objects.filter(company=company, deal_no=deal_no)
        .select_related("finalised_vendor")
        .order_by("round_number")
    )


def next_deal_no(company, when=None) -> str:
    now = when or timezone.localtime()
    prefix = now.strftime("%d%m%Y")
    existing = (
        ProcurementDeal.objects.filter(company=company, deal_no__startswith=prefix)
        .values_list("deal_no", flat=True)
        .distinct()
    )
    max_seq = 0
    for deal_no in existing:
        suffix = str(deal_no)[len(prefix) :]
        if suffix.isdigit():
            max_seq = max(max_seq, int(suffix))
    return f"{prefix}{max_seq + 1:04d}"


def preview_deal_no(company) -> str:
    return next_deal_no(company)


def grouped_deal_rows(company):
    lines = (
        ProcurementDeal.objects.filter(company=company)
        .select_related("product")
        .order_by("-created_at", "-id")
    )
    grouped = []
    seen = set()
    for line in lines:
        key = line.deal_no
        if key in seen:
            continue
        seen.add(key)
        siblings = [row for row in lines if row.deal_no == key]
        names = [row.product.name for row in siblings]
        extra = len(names) - 2
        if extra > 0:
            products_label = ", ".join(names[:2]) + f" +{extra} more"
        else:
            products_label = ", ".join(names) if names else "—"
        grouped.append(
            {
                "id": siblings[0].pk,
                "deal_no": key,
                "created_at": siblings[0].created_at,
                "products_label": products_label,
                "total_quantity": len(siblings),
            }
        )
    return grouped


def create_deal(company, products: list[dict], vendor_ids: list[int], expiration_days: int):
    days = max(int(expiration_days or 1), 1)
    with transaction.atomic():
        deal_no = next_deal_no(company)
        lines = []
        for item in products:
            product = item["product"]
            quantity = int(item["quantity"])
            line = ProcurementDeal.objects.create(
                company=company,
                deal_no=deal_no,
                category=product.category or "",
                product=product,
                product_company=product_company_label(product),
                quantity=quantity,
                price=ZERO,
            )
            lines.append(line)
        first = lines[0]
        vendors = list(
            Vendor.objects.filter(company=company, pk__in=vendor_ids).order_by("name")
        )
        for vendor in vendors:
            DealVendor.objects.create(
                company=company,
                deal_no=deal_no,
                deal=first,
                vendor=vendor,
                price=ZERO,
            )
        round_obj = DealRound.objects.create(
            company=company,
            deal_no=deal_no,
            round_number=1,
            name="Round 1",
        )
        submissions = _create_round_submissions(
            company, deal_no, round_obj, expiration_days=days
        )
    sent, failed = _email_round_submissions(
        company,
        deal_no,
        round_obj,
        submissions,
        expiration_days=days,
        kind="create",
    )
    return {
        "deal_no": deal_no,
        "lines": lines,
        "round": round_obj,
        "sent": sent,
        "failed": failed,
    }


def _expires_at(days: int):
    return timezone.now() + timedelta(days=max(int(days or 1), 1))


def expire_unsubmitted(round_obj: DealRound):
    now = timezone.now()
    VendorPriceSubmission.objects.filter(
        deal_round=round_obj, is_submitted=False
    ).update(expires_at=now - timedelta(seconds=1))


def _create_round_submissions(company, deal_no, round_obj, *, expiration_days: int):
    first = first_line(company, deal_no)
    vendors = [row.vendor for row in invited_vendors(company, deal_no)]
    expire_unsubmitted(round_obj)
    submissions = []
    for vendor in vendors:
        if vendor_has_submitted(round_obj, vendor):
            continue
        submissions.append(
            VendorPriceSubmission.objects.create(
                company=company,
                deal_no=deal_no,
                deal=first,
                vendor=vendor,
                deal_round=round_obj,
                expires_at=_expires_at(expiration_days),
                is_final_quotation=bool(round_obj.is_final),
            )
        )
    return submissions


def _email_round_submissions(
    company,
    deal_no,
    round_obj,
    submissions,
    *,
    expiration_days: int,
    kind: str,
):
    previous_lowest = []
    if kind == "followup" or round_obj.round_number > 1:
        previous_lowest = lowest_prices_for_previous_round(company, deal_no, round_obj)
    sent = 0
    failed = 0
    for submission in submissions:
        ok = send_vendor_quotation_email(
            company,
            deal_no,
            submission.vendor,
            submission,
            round_obj,
            expiration_days=expiration_days,
            kind=kind,
            previous_lowest=previous_lowest,
        )
        if ok:
            sent += 1
        else:
            failed += 1
    return sent, failed


def issue_round_links(
    company,
    deal_no: str,
    round_obj: DealRound,
    *,
    expiration_days: int,
    kind: str,
):
    """Create new tokens for invited vendors and email email1. Returns (sent, failed)."""
    with transaction.atomic():
        submissions = _create_round_submissions(
            company, deal_no, round_obj, expiration_days=expiration_days
        )
    return _email_round_submissions(
        company,
        deal_no,
        round_obj,
        submissions,
        expiration_days=expiration_days,
        kind=kind,
    )


def resend_round(company, deal_no: str, round_obj: DealRound, expiration_days: int = 7):
    kind = "round1" if round_obj.round_number == 1 else "followup"
    if round_obj.round_number == 1:
        previous = lowest_prices_for_previous_round(company, deal_no, round_obj)
        if previous:
            kind = "followup"
    return issue_round_links(
        company,
        deal_no,
        round_obj,
        expiration_days=expiration_days,
        kind=kind,
    )


def add_round(company, deal_no: str, expiration_days: int = 7):
    with transaction.atomic():
        rounds = deal_rounds(company, deal_no)
        if not rounds:
            raise ValueError("Deal has no rounds.")
        previous = rounds[-1]
        next_number = previous.round_number + 1
        new_round = DealRound.objects.create(
            company=company,
            deal_no=deal_no,
            round_number=next_number,
            name=f"Round {next_number}",
            finalised_price=previous.finalised_price,
            finalised_vendor=previous.finalised_vendor,
        )
        existing = DealRoundVendorPrice.objects.filter(deal_round=previous)
        DealRoundVendorPrice.objects.bulk_create(
            [
                DealRoundVendorPrice(
                    deal_round=new_round,
                    vendor=row.vendor,
                    product=row.product,
                    price=row.price,
                    note=row.note,
                )
                for row in existing
            ]
        )
        submissions = _create_round_submissions(
            company, deal_no, new_round, expiration_days=expiration_days
        )
    sent, failed = _email_round_submissions(
        company,
        deal_no,
        new_round,
        submissions,
        expiration_days=expiration_days,
        kind="followup",
    )
    return new_round, sent, failed


@transaction.atomic
def mark_round_final(company, deal_no: str, round_obj: DealRound):
    DealRound.objects.filter(company=company, deal_no=deal_no, is_final=True).update(
        is_final=False
    )
    round_obj.is_final = True
    round_obj.save(update_fields=["is_final"])
    return round_obj


def save_finalised_price(company, deal_no: str, price, vendor):
    amount = None if price in (None, "") else _as_money(price)
    DealRound.objects.filter(company=company, deal_no=deal_no).update(
        finalised_price=amount,
        finalised_vendor=vendor,
    )


def lowest_prices_for_previous_round(company, deal_no: str, round_obj: DealRound):
    if round_obj.round_number <= 1:
        return []
    previous = DealRound.objects.filter(
        company=company, deal_no=deal_no, round_number=round_obj.round_number - 1
    ).first()
    if previous is None:
        return []
    return lowest_unit_prices(previous)


def lowest_unit_prices(round_obj: DealRound) -> list[dict]:
    lines = product_lines(round_obj.company, round_obj.deal_no)
    rows = []
    for line in lines:
        agg = DealRoundVendorPrice.objects.filter(
            deal_round=round_obj,
            product=line.product,
            price__gt=0,
        ).aggregate(lowest=Min("price"))
        rows.append(
            {
                "product": line.product,
                "product_name": line.product.name,
                "lowest": agg["lowest"],
            }
        )
    return rows


@transaction.atomic
def submit_vendor_prices(submission: VendorPriceSubmission, items: list[dict]):
    if submission.is_submitted:
        raise ValueError("already_submitted")
    if submission.is_expired():
        raise ValueError("expired")
    now = timezone.now()
    submission.is_submitted = True
    submission.submitted_at = now
    submission.is_final_quotation = bool(submission.deal_round.is_final)
    submission.save(
        update_fields=["is_submitted", "submitted_at", "is_final_quotation"]
    )
    VendorProductPrice.objects.filter(submission=submission).delete()
    for item in items:
        VendorProductPrice.objects.create(
            submission=submission,
            product=item["product"],
            price=_as_money(item["price"]),
            note=(item.get("note") or "").strip(),
        )
        DealRoundVendorPrice.objects.update_or_create(
            deal_round=submission.deal_round,
            vendor=submission.vendor,
            product=item["product"],
            defaults={
                "price": _as_money(item["price"]),
                "note": (item.get("note") or "").strip(),
            },
        )
    return submission


def delete_deals(company, deal_nos: Iterable[str]) -> int:
    deal_nos = [str(n) for n in deal_nos if n]
    if not deal_nos:
        return 0
    VendorPriceSubmission.objects.filter(company=company, deal_no__in=deal_nos).delete()
    DealRound.objects.filter(company=company, deal_no__in=deal_nos).delete()
    DealVendor.objects.filter(company=company, deal_no__in=deal_nos).delete()
    DealVendorPrice.objects.filter(company=company, deal_no__in=deal_nos).delete()
    deleted, _ = ProcurementDeal.objects.filter(
        company=company, deal_no__in=deal_nos
    ).delete()
    return deleted


def round_counts(round_obj: DealRound) -> tuple[int, int]:
    submitted = (
        VendorPriceSubmission.objects.filter(deal_round=round_obj, is_submitted=True)
        .values("vendor_id")
        .distinct()
        .count()
    )
    links = (
        VendorPriceSubmission.objects.filter(deal_round=round_obj)
        .values("vendor_id")
        .distinct()
        .count()
    )
    if links == 0:
        links = DealVendor.objects.filter(
            company=round_obj.company, deal_no=round_obj.deal_no
        ).count()
    return submitted, links


def vendor_has_submitted(round_obj: DealRound, vendor: Vendor) -> bool:
    return VendorPriceSubmission.objects.filter(
        deal_round=round_obj, vendor=vendor, is_submitted=True
    ).exists()


def price_map_for_round(round_obj: DealRound) -> dict[tuple[int, int], DealRoundVendorPrice]:
    rows = DealRoundVendorPrice.objects.filter(deal_round=round_obj).select_related(
        "vendor", "product"
    )
    return {(row.vendor_id, row.product_id): row for row in rows}


def rank_vendor_rows(rows: list[dict]) -> list[dict]:
    ranked = []
    pending = []
    for row in rows:
        if row.get("complete") and row.get("total") and row["total"] > 0:
            ranked.append(row)
        else:
            row = dict(row)
            row["rank"] = None
            pending.append(row)
    ranked.sort(key=lambda item: item["total"])
    for index, row in enumerate(ranked, start=1):
        row["rank"] = index
    return ranked + pending


def compare_rows_for_round(company, deal_no: str, round_obj: DealRound) -> list[dict]:
    lines = product_lines(company, deal_no)
    vendors = [row.vendor for row in invited_vendors(company, deal_no)]
    prices = price_map_for_round(round_obj)
    rows = []
    for vendor in vendors:
        product_prices = []
        notes = []
        complete = True
        total = ZERO
        for line in lines:
            cell = prices.get((vendor.id, line.product_id))
            unit = _as_money(cell.price) if cell else None
            if cell and (cell.note or "").strip():
                notes.append(cell.note.strip())
            if unit is None or unit <= 0:
                complete = False
            if unit is not None:
                total += unit * Decimal(line.quantity)
            product_prices.append(
                {
                    "product": line.product,
                    "unit": unit,
                    "display": format_money(unit) if unit is not None and unit > 0 else "—",
                }
            )
        submitted = vendor_has_submitted(round_obj, vendor)
        if not submitted and complete:
            submitted = True
        rows.append(
            {
                "vendor": vendor,
                "status": "Submitted" if submitted else "Awaiting link",
                "submitted": submitted,
                "product_prices": product_prices,
                "note": " / ".join(dict.fromkeys(notes)),
                "total": total,
                "complete": complete and submitted,
            }
        )
    return rank_vendor_rows(rows)


def chart_series(company, deal_no: str, product_id: str | int | None = None):
    rounds = deal_rounds(company, deal_no)
    vendors = [row.vendor for row in invited_vendors(company, deal_no)]
    labels = [round_obj.display_name for round_obj in rounds]
    datasets = []
    for vendor in vendors:
        data = []
        for round_obj in rounds:
            prices = price_map_for_round(round_obj)
            if product_id and str(product_id) != "all":
                cell = prices.get((vendor.id, int(product_id)))
                data.append(float(cell.price) if cell and cell.price else None)
            else:
                total = ZERO
                for line in product_lines(company, deal_no):
                    cell = prices.get((vendor.id, line.product_id))
                    if cell and cell.price:
                        total += _as_money(cell.price) * Decimal(line.quantity)
                data.append(float(total) if total > 0 else None)
        datasets.append({"label": vendor.name, "data": data})
    finalised = None
    vendor_name = ""
    for round_obj in rounds:
        if round_obj.finalised_price is not None:
            finalised = float(round_obj.finalised_price)
            if round_obj.finalised_vendor_id:
                vendor_name = round_obj.finalised_vendor.name if round_obj.finalised_vendor else ""
    if finalised is None:
        final_round = next((row for row in rounds if row.is_final), None)
        if final_round is not None:
            ranked = compare_rows_for_round(company, deal_no, final_round)
            if ranked and ranked[0].get("rank") == 1:
                finalised = float(ranked[0]["total"])
                vendor_name = ranked[0]["vendor"].name
    return {
        "labels": labels,
        "datasets": datasets,
        "finalised_price": finalised,
        "finalised_vendor": vendor_name,
    }


def timeline_rows(company, deal_no: str) -> list[dict]:
    rows = []
    submissions = (
        VendorPriceSubmission.objects.filter(company=company, deal_no=deal_no)
        .select_related("vendor", "deal_round")
        .prefetch_related("product_prices__product")
        .order_by("submitted_at", "id")
    )
    for submission in submissions:
        if not submission.is_submitted:
            continue
        source = "Final quotation" if submission.is_final_quotation else "Quotation submitted"
        if submission.deal_round.is_final:
            source = f"{submission.deal_round.display_name} (Final)"
        for price in submission.product_prices.all():
            rows.append(
                {
                    "time": submission.submitted_at,
                    "vendor": submission.vendor.name,
                    "product": price.product.name,
                    "price": price.price,
                    "note": price.note,
                    "source": source,
                }
            )
    manuals = (
        DealVendorPrice.objects.filter(company=company, deal_no=deal_no)
        .select_related("vendor", "product")
        .order_by("created_at", "id")
    )
    for row in manuals:
        rows.append(
            {
                "time": row.created_at,
                "vendor": row.vendor.name,
                "product": row.product.name,
                "price": row.price,
                "note": row.note,
                "source": "Manually added",
            }
        )
    rows.sort(key=lambda item: item["time"] or timezone.now())
    return rows


def matrix_rows(company, deal_no: str) -> list[dict]:
    rounds = deal_rounds(company, deal_no)
    lines = product_lines(company, deal_no)
    vendors = [row.vendor for row in invited_vendors(company, deal_no)]
    result = []
    for vendor in vendors:
        vendor_rounds = []
        for round_obj in rounds:
            prices = price_map_for_round(round_obj)
            submitted = (
                VendorPriceSubmission.objects.filter(
                    deal_round=round_obj, vendor=vendor, is_submitted=True
                )
                .order_by("-submitted_at")
                .first()
            )
            cells = []
            has_any = False
            for line in lines:
                cell = prices.get((vendor.id, line.product_id))
                if cell and cell.price is not None:
                    has_any = True
                    cells.append(format_money(cell.price) if cell.price > 0 else "—")
                else:
                    cells.append(None)
            vendor_rounds.append(
                {
                    "round": round_obj,
                    "cells": cells,
                    "timestamp": submitted.submitted_at if submitted else None,
                    "has_prices": has_any,
                }
            )
        result.append({"vendor": vendor, "rounds": vendor_rounds})
    return result


def add_manual_vendor_prices(
    company,
    deal_no: str,
    vendor: Vendor,
    *,
    note: str,
    mode: str,
    product_prices: dict[int, Decimal] | None = None,
    deal_price: Decimal | None = None,
):
    lines = product_lines(company, deal_no)
    first = lines[0]
    DealVendor.objects.get_or_create(
        company=company,
        deal_no=deal_no,
        vendor=vendor,
        defaults={"deal": first, "price": ZERO, "note": note},
    )
    round_obj = deal_rounds(company, deal_no)[-1]
    note = (note or "").strip()
    if mode == "deal":
        total_qty = sum(line.quantity for line in lines) or 1
        unit = _as_money(deal_price) / Decimal(total_qty)
        encoded = f"DEAL_PRICE:Rs{_as_money(deal_price)}"
        if note:
            encoded = f"{encoded}:{note}"
        per_line_note = encoded
        prices = {line.product_id: unit for line in lines}
    else:
        per_line_note = note
        prices = product_prices or {}
    for line in lines:
        amount = _as_money(prices.get(line.product_id, ZERO))
        DealVendorPrice.objects.create(
            company=company,
            deal_no=deal_no,
            deal=first,
            vendor=vendor,
            product=line.product,
            price=amount,
            note=per_line_note,
        )
        DealRoundVendorPrice.objects.update_or_create(
            deal_round=round_obj,
            vendor=vendor,
            product=line.product,
            defaults={"price": amount, "note": per_line_note},
        )


def remove_manual_vendor(company, deal_no: str, vendor: Vendor):
    DealVendorPrice.objects.filter(
        company=company, deal_no=deal_no, vendor=vendor
    ).delete()
    submissions = list(
        VendorPriceSubmission.objects.filter(
            company=company, deal_no=deal_no, vendor=vendor, is_submitted=True
        ).prefetch_related("product_prices")
    )
    if not submissions:
        DealRoundVendorPrice.objects.filter(
            deal_round__company=company,
            deal_round__deal_no=deal_no,
            vendor=vendor,
        ).delete()
        DealVendor.objects.filter(
            company=company, deal_no=deal_no, vendor=vendor
        ).delete()
        return
    for submission in submissions:
        for price in submission.product_prices.all():
            DealRoundVendorPrice.objects.update_or_create(
                deal_round=submission.deal_round,
                vendor=vendor,
                product=price.product,
                defaults={"price": price.price, "note": price.note},
            )


def purchase_recipients(company: CompanyProfile) -> list[str]:
    return parse_extra_emails(company.purchase_department_emails)


def send_vendor_quotation_email(
    company,
    deal_no: str,
    vendor: Vendor,
    submission: VendorPriceSubmission,
    round_obj: DealRound,
    *,
    expiration_days: int,
    kind: str,
    previous_lowest: list[dict] | None = None,
) -> bool:
    to_email = (vendor.email or "").strip()
    if not to_email:
        logger.warning(
            "Skipped procurement email for vendor %s on deal %s: missing email 1.",
            vendor.pk,
            deal_no,
        )
        return False
    if not _company_can_send_mail(company):
        logger.warning(
            "Skipped procurement email for deal %s: outgoing mail not configured.",
            deal_no,
        )
        return False
    url = submission_absolute_url(submission.submission_token)
    lines = product_lines(company, deal_no)
    subject = f"Quotation Request for Deal No. {deal_no}"
    if kind == "create":
        eyebrow = "PROCUREMENT QUOTATION"
        heading = "Quotation request"
        intro = "Please provide your competitive pricing for the following products."
        cta = "Submit Prices Now"
        include_lowest = False
    elif kind == "followup":
        eyebrow = "PROCUREMENT QUOTATION"
        heading = "Quotation request"
        intro = (
            "Please submit your prices for this round using the link below. "
            "Vendor names for other quotes are not disclosed."
        )
        cta = "Submit Price"
        include_lowest = True
    else:
        eyebrow = "PROCUREMENT QUOTATION"
        heading = "Quotation request"
        intro = "Please provide your competitive pricing for the following products."
        cta = "Submit Prices Now"
        include_lowest = False

    fields = [
        ("DEAL NUMBER", deal_no),
        ("LINK EXPIRY", expiry_label(expiration_days)),
    ]
    html_body = _quotation_html(
        company,
        vendor,
        eyebrow=eyebrow,
        heading=heading,
        intro=intro,
        fields=fields,
        products=lines,
        url=url,
        cta=cta,
        expiration_days=expiration_days,
        previous_lowest=previous_lowest if include_lowest else None,
        include_lowest=include_lowest,
    )
    text_body = _quotation_text(
        company,
        vendor,
        intro=intro,
        fields=fields,
        products=lines,
        url=url,
        expiration_days=expiration_days,
        previous_lowest=previous_lowest if include_lowest else None,
        include_lowest=include_lowest,
        cta=cta,
    )
    logo_inline = _company_logo_inline(company)
    ok, err = send_outbound(
        company.outgoing_email,
        company.outgoing_email_app_password,
        [to_email],
        subject,
        text_body,
        html_body=html_body,
        inline_images=[logo_inline] if logo_inline else None,
    )
    if not ok:
        logger.warning(
            "Procurement quotation email failed for deal %s vendor %s: %s",
            deal_no,
            vendor.pk,
            err or "unknown error",
        )
    return ok


def send_purchase_department_email(company, deal_no: str, pdf_bytes: bytes) -> tuple[bool, str]:
    recipients = purchase_recipients(company)
    if not recipients:
        return False, PURCHASE_MISSING
    if not _company_can_send_mail(company):
        return False, "Failed to send. Please try again."
    filename = f"Deal_Comparison_{deal_no}.pdf"
    subject = f"Deal Comparison - Deal Number: {deal_no}"
    fields = [
        ("DEAL NUMBER", deal_no),
        ("ATTACHMENT", filename),
        ("STATUS", "Ready for review"),
    ]
    html_body = _branded_html(
        company,
        eyebrow="DEAL COMPARISON",
        heading="Deal comparison attached",
        intro_html=f'<p style="margin:0 0 12px 0;font-size:15px;line-height:1.55;color:{_TEXT};font-family:{_FONT};">Please find the deal comparison report attached.</p>',
        fields=fields,
        extra_html="",
    )
    text_body = "\n".join(
        [
            "Hello,",
            "",
            "Please find the deal comparison report attached.",
            "",
            f"DEAL NUMBER: {deal_no}",
            f"ATTACHMENT: {filename}",
            "STATUS: Ready for review",
            "",
            "Best Regards,",
            company.company_name,
            "",
            "This is an automated quotation request. Please do not reply to this email.",
        ]
    )
    logo_inline = _company_logo_inline(company)
    ok, err = send_outbound(
        company.outgoing_email,
        company.outgoing_email_app_password,
        recipients,
        subject,
        text_body,
        html_body=html_body,
        attachments=[(filename, pdf_bytes, "application/pdf")],
        inline_images=[logo_inline] if logo_inline else None,
    )
    if not ok:
        logger.warning("Purchase department email failed for deal %s: %s", deal_no, err)
        return False, "Failed to send. Please try again."
    return True, "Sent to Purchase Department successfully!"


def _logo_html(company: CompanyProfile, logo_cid: str = "") -> str:
    company_name = escape(company.company_name or "")
    if logo_cid:
        return (
            f'<img src="cid:{escape(logo_cid)}" alt="{company_name}" '
            'style="display:block;max-height:42px;max-width:180px;border:0;">'
        )
    return (
        f'<span style="color:#FFFFFF;font-family:{_FONT};font-size:18px;font-weight:700;">'
        f"{company_name}</span>"
    )


def _fields_html(fields: list[tuple[str, str]]) -> str:
    parts = []
    for label, value in fields:
        parts.append(
            f"""
            <tr>
              <td style="padding:12px;border:1px solid {_BORDER};background-color:{_TABLE_HEAD};color:{_MUTED};font-family:{_FONT};font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:0.3px;width:34%;">{escape(label)}</td>
              <td style="padding:12px;border:1px solid {_BORDER};color:{_TEXT};font-family:{_FONT};font-size:14px;">{escape(str(value))}</td>
            </tr>
            """.strip()
        )
    return "".join(parts)


def _products_html(lines) -> str:
    rows = []
    for line in lines:
        rows.append(
            f"""
            <tr>
              <td style="padding:10px 12px;border:1px solid {_BORDER};font-family:{_FONT};font-size:14px;color:{_TEXT};">{escape(line.category or "—")}</td>
              <td style="padding:10px 12px;border:1px solid {_BORDER};font-family:{_FONT};font-size:14px;color:{_TEXT};">{escape(line.product.name)}</td>
              <td style="padding:10px 12px;border:1px solid {_BORDER};font-family:{_FONT};font-size:14px;color:{_TEXT};">{escape(line.product_company or "—")}</td>
              <td style="padding:10px 12px;border:1px solid {_BORDER};font-family:{_FONT};font-size:14px;color:{_TEXT};text-align:right;">{line.quantity}</td>
            </tr>
            """.strip()
        )
    return f"""
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="border-collapse:collapse;width:100%;margin:12px 0;">
      <thead>
        <tr>
          <th align="left" style="padding:10px 12px;background-color:{_HEADER_BG};border:1px solid {_BORDER};color:#fff;font-family:{_FONT};font-size:12px;">Category</th>
          <th align="left" style="padding:10px 12px;background-color:{_HEADER_BG};border:1px solid {_BORDER};color:#fff;font-family:{_FONT};font-size:12px;">Product Name</th>
          <th align="left" style="padding:10px 12px;background-color:{_HEADER_BG};border:1px solid {_BORDER};color:#fff;font-family:{_FONT};font-size:12px;">Company Name</th>
          <th align="right" style="padding:10px 12px;background-color:{_HEADER_BG};border:1px solid {_BORDER};color:#fff;font-family:{_FONT};font-size:12px;">Quantity</th>
        </tr>
      </thead>
      <tbody>
        {''.join(rows)}
      </tbody>
    </table>
    """


def _lowest_html(previous_lowest: list[dict] | None) -> str:
    if not previous_lowest:
        return (
            f'<p style="margin:8px 0 0 0;font-size:14px;color:{_MUTED};font-family:{_FONT};">'
            "No vendor prices are available yet for this deal.</p>"
        )
    rows = []
    any_price = False
    for item in previous_lowest:
        lowest = item.get("lowest")
        if lowest is None:
            display = "—"
        else:
            any_price = True
            display = format_money(lowest)
        rows.append(
            f'<tr><td style="padding:10px 12px;border:1px solid {_BORDER};font-family:{_FONT};font-size:14px;">{escape(item["product_name"])}</td>'
            f'<td style="padding:10px 12px;border:1px solid {_BORDER};font-family:{_FONT};font-size:14px;text-align:right;">{escape(display)}</td></tr>'
        )
    if not any_price:
        return (
            f'<p style="margin:8px 0 0 0;font-size:14px;color:{_MUTED};font-family:{_FONT};">'
            "No vendor prices are available yet for this deal.</p>"
        )
    return f"""
    <h3 style="margin:18px 0 4px 0;font-size:16px;color:{_TEXT};font-family:{_FONT};">Lowest quoted prices</h3>
    <p style="margin:0 0 8px 0;font-size:13px;color:{_MUTED};font-family:{_FONT};">Vendor names are not disclosed.</p>
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="border-collapse:collapse;width:100%;">
      <thead>
        <tr>
          <th align="left" style="padding:10px 12px;background-color:{_HEADER_BG};border:1px solid {_BORDER};color:#fff;font-family:{_FONT};font-size:12px;">Product</th>
          <th align="right" style="padding:10px 12px;background-color:{_HEADER_BG};border:1px solid {_BORDER};color:#fff;font-family:{_FONT};font-size:12px;">Lowest Price ({CURRENCY})</th>
        </tr>
      </thead>
      <tbody>{''.join(rows)}</tbody>
    </table>
    """


def _cta_html(url: str, label: str) -> str:
    return f"""
    <p style="margin:20px 0 8px 0;">
      <a href="{escape(url)}" style="display:inline-block;background-color:{_HEADER_BG};color:#ffffff;font-family:{_FONT};font-size:14px;font-weight:700;text-decoration:none;padding:12px 22px;border-radius:8px;">
        {escape(label)}
      </a>
    </p>
    """


def _branded_html(company, *, eyebrow, heading, intro_html, fields, extra_html) -> str:
    logo_inline = _company_logo_inline(company)
    logo_cid = logo_inline[0] if logo_inline else ""
    company_name = escape(company.company_name or "")
    return f"""
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin:0;padding:0;background-color:{_PAGE_BG};">
  <tr>
    <td align="center" style="padding:24px 12px;">
      <table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0" style="width:600px;max-width:600px;background-color:{_CARD_BG};border:1px solid {_BORDER};border-radius:8px;">
        <tr>
          <td bgcolor="{_HEADER_BG}" style="background-color:{_HEADER_BG};padding:14px 18px;border-radius:8px 8px 0 0;">
            {_logo_html(company, logo_cid)}
          </td>
        </tr>
        <tr>
          <td style="padding:28px 28px 8px 28px;font-family:{_FONT};">
            <p style="margin:0 0 6px 0;font-size:12px;letter-spacing:0.4px;text-transform:uppercase;color:{_BLUE};font-weight:600;">{escape(eyebrow)}</p>
            <h1 style="margin:0 0 16px 0;font-size:22px;line-height:1.3;color:{_TEXT};font-weight:700;">{escape(heading)}</h1>
            {intro_html}
          </td>
        </tr>
        <tr>
          <td style="padding:0 28px 8px 28px;">
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="border-collapse:collapse;width:100%;">
              <thead>
                <tr>
                  <th align="left" style="padding:12px;background-color:{_HEADER_BG};border:1px solid {_BORDER};color:#FFFFFF;font-family:{_FONT};font-size:12px;">Field</th>
                  <th align="left" style="padding:12px;background-color:{_HEADER_BG};border:1px solid {_BORDER};color:#FFFFFF;font-family:{_FONT};font-size:12px;">Value</th>
                </tr>
              </thead>
              <tbody>{_fields_html(fields)}</tbody>
            </table>
          </td>
        </tr>
        <tr>
          <td style="padding:8px 28px 28px 28px;font-family:{_FONT};">
            {extra_html}
            <p style="margin:16px 0 0 0;font-size:15px;line-height:1.55;color:{_TEXT};">
              Best Regards,<br>
              <strong>{company_name}</strong>
            </p>
            <p style="margin:20px 0 0 0;padding-top:16px;border-top:1px solid {_BORDER};font-size:12px;line-height:1.5;color:{_MUTED};">
              This is an automated quotation request. Please do not reply to this email.
            </p>
          </td>
        </tr>
      </table>
    </td>
  </tr>
</table>
""".strip()


def _quotation_html(
    company,
    vendor,
    *,
    eyebrow,
    heading,
    intro,
    fields,
    products,
    url,
    cta,
    expiration_days,
    previous_lowest,
    include_lowest,
) -> str:
    intro_html = (
        f'<p style="margin:0 0 8px 0;font-size:15px;line-height:1.55;color:{_TEXT};font-family:{_FONT};">Hello,</p>'
        f'<p style="margin:0 0 12px 0;font-size:15px;line-height:1.55;color:{_TEXT};font-family:{_FONT};">{escape(intro)}</p>'
    )
    extra = _products_html(products)
    extra += (
        f'<p style="margin:12px 0 0 0;font-size:15px;color:{_TEXT};font-family:{_FONT};">'
        "Please submit your prices online.</p>"
    )
    if include_lowest:
        extra += _lowest_html(previous_lowest)
    extra += _cta_html(url, cta)
    extra += (
        f'<p style="margin:8px 0 0 0;font-size:13px;color:{_MUTED};font-family:{_FONT};">'
        f"This link will expire in {int(expiration_days)} day(s).</p>"
    )
    return _branded_html(
        company,
        eyebrow=eyebrow,
        heading=heading,
        intro_html=intro_html,
        fields=fields,
        extra_html=extra,
    )


def _quotation_text(
    company,
    vendor,
    *,
    intro,
    fields,
    products,
    url,
    expiration_days,
    previous_lowest,
    include_lowest,
    cta,
) -> str:
    lines = ["Hello,", "", intro, ""]
    for label, value in fields:
        lines.append(f"{label}: {value}")
    lines.append("")
    lines.append("Products:")
    for line in products:
        lines.append(
            f"- {line.category or '—'} | {line.product.name} | "
            f"{line.product_company or '—'} | Qty {line.quantity}"
        )
    lines.extend(
        [
            "",
            "Please submit your prices online.",
            "",
            f"{cta}: {url}",
            f"This link will expire in {int(expiration_days)} day(s).",
        ]
    )
    if include_lowest:
        lines.extend(["", "Lowest quoted prices", "Vendor names are not disclosed."])
        if previous_lowest and any(item.get("lowest") is not None for item in previous_lowest):
            for item in previous_lowest:
                if item.get("lowest") is None:
                    continue
                lines.append(f"- {item['product_name']}: {format_money(item['lowest'])}")
        else:
            lines.append("No vendor prices are available yet for this deal.")
    lines.extend(
        [
            "",
            "Best Regards,",
            company.company_name,
            "",
            "This is an automated quotation request. Please do not reply to this email.",
        ]
    )
    return "\n".join(lines)
