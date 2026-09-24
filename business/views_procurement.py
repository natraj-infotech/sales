"""Company and public views for the procurement module."""

from __future__ import annotations

import json
import logging
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from accounts.permissions import sales_module_required
from business.models import (
    DealRound,
    VendorPriceSubmission,
)
from business.procurement import (
    CREATE_EMAIL_WARNING,
    CREATE_SUCCESS,
    add_manual_vendor_prices,
    add_round,
    chart_series,
    compare_rows_for_round,
    create_deal,
    deal_rounds,
    delete_deals,
    first_line,
    format_money,
    grouped_deal_rows,
    invited_vendors,
    lowest_prices_for_previous_round,
    mark_round_final,
    matrix_rows,
    preview_deal_no,
    product_company_label,
    product_lines,
    remove_manual_vendor,
    resend_round,
    round_counts,
    save_finalised_price,
    send_purchase_department_email,
    submit_vendor_prices,
    timeline_rows,
)
from business.procurement_pdf import build_compare_pdf
from business.views import _company

logger = logging.getLogger(__name__)


def _deal_or_404(company, deal_no):
    line = first_line(company, deal_no)
    if line is None:
        from django.http import Http404

        raise Http404("No deal found for the provided deal number.")
    return line


def _catalog_payload(company):
    products = list(company.products.all().order_by("name", "pk"))
    categories = sorted(
        {p.category for p in products if (p.category or "").strip()},
        key=lambda value: value.lower(),
    )
    vendors = [
        {
            "id": vendor.pk,
            "name": vendor.name,
            "company_names": [
                (name or "").strip()
                for name in (vendor.catalog_companies or [])
                if (name or "").strip()
            ],
        }
        for vendor in company.vendors.all().order_by("name")
    ]
    product_rows = []
    for product in products:
        company_name = product_company_label(product)
        product_rows.append(
            {
                "id": product.pk,
                "name": product.name,
                "category": product.category or "",
                "company_name": company_name,
            }
        )
    return categories, product_rows, vendors


def _parse_decimal(raw):
    text = str(raw or "").strip().replace(",", "")
    if not text:
        return None
    try:
        value = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    if value < 0:
        return None
    return value


@sales_module_required
def procurement_list(request):
    company = _company(request)
    if request.method == "POST":
        deal_nos = request.POST.getlist("deal_nos")
        if not deal_nos:
            messages.error(request, "Please select at least one deal to delete.")
            return redirect("procurement_list")
        delete_deals(company, deal_nos)
        messages.success(request, "Selected deals were deleted.")
        return redirect("procurement_list")
    return render(
        request,
        "business/procurement_list.html",
        {
            "object_list": grouped_deal_rows(company),
        },
    )


@sales_module_required
def procurement_create(request):
    company = _company(request)
    categories, products, vendors = _catalog_payload(company)
    if request.method == "POST":
        error = None
        try:
            selected_products = json.loads(request.POST.get("products_json") or "[]")
            vendor_ids = [int(pk) for pk in request.POST.getlist("vendor_ids") if pk]
        except (ValueError, json.JSONDecodeError):
            selected_products = []
            vendor_ids = []
            error = "Please add at least one product and one vendor."
        expiration_days = request.POST.get("expiration_days") or "1"
        try:
            expiration_days = int(expiration_days)
        except (TypeError, ValueError):
            expiration_days = 0
        if expiration_days < 1:
            error = "Link expiration must be at least 1 day."
        built = []
        seen_products = set()
        if error is None:
            if not selected_products or not vendor_ids:
                error = "Please add at least one product and one vendor."
            for item in selected_products:
                try:
                    product_id = int(item.get("product_id"))
                    quantity = int(item.get("quantity"))
                except (TypeError, ValueError):
                    error = "Category, product, and quantity are required."
                    break
                if quantity < 1:
                    error = "Quantity must be at least 1."
                    break
                if product_id in seen_products:
                    error = "Duplicate products cannot be added to the same deal."
                    break
                seen_products.add(product_id)
                product = (
                    company.products.select_related("vendor")
                    .filter(pk=product_id)
                    .first()
                )
                if product is None:
                    error = "One of the selected products was not found."
                    break
                built.append({"product": product, "quantity": quantity})
            vendors_ok = company.vendors.filter(pk__in=vendor_ids).count()
            if error is None and vendors_ok != len(set(vendor_ids)):
                error = "One of the selected vendors was not found."
        if error:
            messages.error(request, error)
        else:
            result = create_deal(
                company, built, vendor_ids, expiration_days=expiration_days
            )
            if result["failed"]:
                messages.warning(request, CREATE_EMAIL_WARNING)
            else:
                messages.success(request, CREATE_SUCCESS)
            return redirect("procurement_list")
    return render(
        request,
        "business/procurement_create.html",
        {
            "deal_no": preview_deal_no(company),
            "categories": categories,
            "products_data": products,
            "vendors_data": vendors,
        },
    )


def _detail_context(company, deal_no):
    lines = product_lines(company, deal_no)
    if not lines:
        return None
    vendors = invited_vendors(company, deal_no)
    rounds = []
    for round_obj in deal_rounds(company, deal_no):
        submitted, links = round_counts(round_obj)
        rounds.append(
            {
                "object": round_obj,
                "submitted": submitted,
                "links": links,
            }
        )
    matrix = matrix_rows(company, deal_no)
    has_matrix = any(
        any(cell is not None for cell in round_row["cells"])
        for vendor_row in matrix
        for round_row in vendor_row["rounds"]
    )
    return {
        "deal_no": deal_no,
        "header_line": lines[0],
        "lines": lines,
        "vendors": vendors,
        "rounds": rounds,
        "matrix": matrix,
        "has_matrix": has_matrix,
        "timeline": timeline_rows(company, deal_no) if not has_matrix else [],
        "products": [line.product for line in lines],
    }


@sales_module_required
@require_http_methods(["GET", "POST"])
def procurement_detail(request, deal_no):
    company = _company(request)
    _deal_or_404(company, deal_no)
    if request.method == "POST":
        action = request.POST.get("action")
        round_id = request.POST.get("round_id")
        round_obj = None
        if round_id:
            round_obj = get_object_or_404(
                DealRound, pk=round_id, company=company, deal_no=deal_no
            )
        if action == "resend_round" and round_obj:
            sent, failed = resend_round(company, deal_no, round_obj)
            if failed:
                messages.warning(
                    request,
                    f"{round_obj.display_name} links were created, but some emails were not sent.",
                )
            else:
                messages.success(
                    request, f"{round_obj.display_name} links sent to vendors."
                )
        elif action == "mark_final" and round_obj:
            mark_round_final(company, deal_no, round_obj)
            messages.success(request, f"{round_obj.display_name} marked as Final.")
        elif action == "add_round":
            new_round, sent, failed = add_round(company, deal_no)
            if failed:
                messages.warning(
                    request,
                    f"{new_round.display_name} was created, but some emails were not sent.",
                )
            else:
                messages.success(
                    request, f"{new_round.display_name} created and links sent to vendors."
                )
        return redirect("procurement_detail", deal_no=deal_no)
    return render(
        request,
        "business/procurement_detail.html",
        _detail_context(company, deal_no),
    )


@sales_module_required
@require_http_methods(["GET", "POST"])
def procurement_compare_picker(request):
    company = _company(request)
    deals = grouped_deal_rows(company)
    selected = (request.GET.get("deal_no") or request.POST.get("deal_no") or "").strip()
    deal_error = ""
    context = {
        "deals": deals,
        "selected_deal_no": selected,
        "lines": [],
        "vendors_table": [],
        "all_vendors": company.vendors.all().order_by("name"),
        "deal_found": False,
        "format_money": format_money,
    }
    if request.method == "POST":
        action = request.POST.get("action")
        selected = (request.POST.get("deal_no") or "").strip()
        if not first_line(company, selected):
            messages.error(request, "No deal found for the provided deal number.")
            context["deal_error"] = "No deal found for the provided deal number."
            context["selected_deal_no"] = selected
            return render(request, "business/procurement_compare_picker.html", context)
        if action == "add_vendor":
            vendor_id = request.POST.get("vendor_id")
            vendor = company.vendors.filter(pk=vendor_id).first()
            note = request.POST.get("note") or ""
            if vendor is None:
                messages.error(request, "Please select a vendor.")
            else:
                lines = product_lines(company, selected)
                product_prices = {}
                valid = True
                for line in lines:
                    amount = _parse_decimal(
                        request.POST.get(f"price_{line.product_id}")
                    )
                    if amount is None or amount <= 0:
                        valid = False
                        break
                    product_prices[line.product_id] = amount
                if not valid:
                    messages.error(
                        request, "Please enter valid prices for all products."
                    )
                else:
                    add_manual_vendor_prices(
                        company,
                        selected,
                        vendor,
                        note=note,
                        mode="product",
                        product_prices=product_prices,
                    )
                    messages.success(request, "Vendor and prices added.")
            return redirect(f"{reverse('procurement_compare')}?deal_no={selected}")
        if action == "delete_vendor":
            vendor = company.vendors.filter(pk=request.POST.get("vendor_id")).first()
            if vendor:
                remove_manual_vendor(company, selected, vendor)
                messages.success(request, "Vendor removed from this comparison.")
            return redirect(f"{reverse('procurement_compare')}?deal_no={selected}")
        if action == "fetch" or action == "compare":
            if action == "compare":
                return redirect("procurement_compare_result", deal_no=selected)
            return redirect(f"{reverse('procurement_compare')}?deal_no={selected}")
    if selected:
        line = first_line(company, selected)
        if line is None:
            deal_error = "No deal found for the provided deal number."
        else:
            rounds = deal_rounds(company, selected)
            round_obj = rounds[-1] if rounds else None
            context.update(
                {
                    "deal_found": True,
                    "header_line": line,
                    "lines": product_lines(company, selected),
                    "vendors_table": compare_rows_for_round(
                        company, selected, round_obj
                    )
                    if round_obj
                    else [],
                }
            )
    context["deal_error"] = deal_error
    context["selected_deal_no"] = selected
    return render(request, "business/procurement_compare_picker.html", context)


def _compare_result_context(company, deal_no, round_id=None, product_id="all"):
    lines = product_lines(company, deal_no)
    rounds = deal_rounds(company, deal_no)
    if not lines or not rounds:
        return None
    selected_round = rounds[-1]
    if round_id:
        match = next((row for row in rounds if str(row.pk) == str(round_id)), None)
        if match:
            selected_round = match
    round_payload = []
    for round_obj in rounds:
        submitted, links = round_counts(round_obj)
        round_payload.append(
            {
                "object": round_obj,
                "submitted": submitted,
                "links": links,
            }
        )
    ranked = compare_rows_for_round(company, deal_no, selected_round)
    chart = chart_series(company, deal_no, product_id=product_id)
    finalised_price = None
    finalised_vendor_id = ""
    for round_obj in rounds:
        if round_obj.finalised_price is not None:
            finalised_price = round_obj.finalised_price
            finalised_vendor_id = round_obj.finalised_vendor_id or ""
    return {
        "deal_no": deal_no,
        "header_line": lines[0],
        "lines": lines,
        "rounds": round_payload,
        "selected_round": selected_round,
        "ranked": ranked,
        "chart": chart,
        "chart_json": json.dumps(chart),
        "product_id": str(product_id or "all"),
        "vendors": [row.vendor for row in invited_vendors(company, deal_no)],
        "finalised_price": finalised_price,
        "finalised_vendor_id": str(finalised_vendor_id or ""),
        "format_money": format_money,
    }


@sales_module_required
@require_http_methods(["GET", "POST"])
def procurement_compare_result(request, deal_no):
    company = _company(request)
    _deal_or_404(company, deal_no)
    round_id = request.GET.get("round") or request.POST.get("round")
    product_id = request.GET.get("product") or "all"
    if request.method == "POST":
        action = request.POST.get("action")
        round_id = request.POST.get("round_id") or round_id
        round_obj = None
        if round_id:
            round_obj = get_object_or_404(
                DealRound, pk=round_id, company=company, deal_no=deal_no
            )
        if action == "save_finalised":
            vendor = None
            vendor_id = request.POST.get("finalised_vendor")
            if vendor_id:
                vendor = company.vendors.filter(pk=vendor_id).first()
            raw = (request.POST.get("finalised_price") or "").strip()
            price = _parse_decimal(raw) if raw else None
            save_finalised_price(company, deal_no, price, vendor)
            messages.success(request, "Finalised price saved.")
        elif action == "resend_round" and round_obj:
            sent, failed = resend_round(company, deal_no, round_obj)
            if failed:
                messages.warning(
                    request,
                    f"{round_obj.display_name} links were created, but some emails were not sent.",
                )
            else:
                messages.success(
                    request, f"{round_obj.display_name} links sent to vendors."
                )
        elif action == "mark_final" and round_obj:
            mark_round_final(company, deal_no, round_obj)
            messages.success(request, f"{round_obj.display_name} marked as Final.")
        elif action == "add_round":
            new_round, sent, failed = add_round(company, deal_no)
            round_id = new_round.pk
            if failed:
                messages.warning(
                    request,
                    f"{new_round.display_name} was created, but some emails were not sent.",
                )
            else:
                messages.success(
                    request, f"{new_round.display_name} created and links sent to vendors."
                )
        elif action == "send_purchase":
            context = _compare_result_context(company, deal_no, round_id, product_id)
            pdf_bytes = build_compare_pdf(
                deal_no,
                context["lines"],
                context["ranked"],
                context["selected_round"].display_name,
            )
            ok, message = send_purchase_department_email(company, deal_no, pdf_bytes)
            if ok:
                messages.success(request, message)
            else:
                messages.error(request, message)
        url = reverse("procurement_compare_result", kwargs={"deal_no": deal_no})
        if round_id:
            url = f"{url}?round={round_id}"
        return redirect(url)
    context = _compare_result_context(company, deal_no, round_id, product_id)
    return render(request, "business/procurement_compare_result.html", context)


@sales_module_required
def procurement_compare_pdf(request, deal_no):
    company = _company(request)
    _deal_or_404(company, deal_no)
    round_id = request.GET.get("round")
    context = _compare_result_context(company, deal_no, round_id)
    pdf_bytes = build_compare_pdf(
        deal_no,
        context["lines"],
        context["ranked"],
        context["selected_round"].display_name,
    )
    response = HttpResponse(pdf_bytes, content_type="application/pdf")
    response["Content-Disposition"] = (
        f'attachment; filename="Compare_Deals_{deal_no}.pdf"'
    )
    return response


def _public_context(submission=None, **extra):
    ctx = {"public_page": True, "submission": submission}
    ctx.update(extra)
    return ctx


@require_http_methods(["GET"])
def vendor_price_submit(request, token):
    submission = (
        VendorPriceSubmission.objects.select_related(
            "vendor", "deal_round", "company"
        )
        .filter(submission_token=token)
        .first()
    )
    if submission is None:
        return render(
            request,
            "business/vendor_price_submit.html",
            _public_context(state="invalid", error_message="This link is not valid."),
            status=404,
        )
    company = submission.company
    deal_no = submission.deal_no
    lines = product_lines(company, deal_no)
    previous = []
    if submission.deal_round.round_number > 1:
        previous = [
            item
            for item in lowest_prices_for_previous_round(
                company, deal_no, submission.deal_round
            )
            if item.get("lowest") is not None
        ]
    existing = {
        row.product_id: row
        for row in submission.product_prices.select_related("product")
    }
    form_lines = [
        {
            "line": line,
            "price_value": existing[line.product_id].price if line.product_id in existing else "",
            "note_value": existing[line.product_id].note if line.product_id in existing else "",
        }
        for line in lines
    ]
    state = "form"
    if submission.is_submitted:
        state = "submitted"
    elif submission.is_expired():
        state = "expired"
    return render(
        request,
        "business/vendor_price_submit.html",
        _public_context(
            submission,
            state=state,
            deal_no=deal_no,
            lines=lines,
            form_lines=form_lines,
            previous_lowest=previous,
            company=company,
        ),
    )


@require_http_methods(["POST"])
def vendor_price_submit_post(request, token):
    submission = (
        VendorPriceSubmission.objects.select_related(
            "vendor", "deal_round", "company"
        )
        .filter(submission_token=token)
        .first()
    )
    if submission is None:
        return render(
            request,
            "business/vendor_price_submit.html",
            _public_context(state="invalid", error_message="This link is not valid."),
            status=404,
        )
    if submission.is_submitted:
        return redirect("vendor_price_submit", token=token)
    if submission.is_expired():
        return redirect("vendor_price_submit", token=token)
    company = submission.company
    lines = product_lines(company, submission.deal_no)
    items = []
    error = ""
    for line in lines:
        amount = _parse_decimal(request.POST.get(f"price_{line.product_id}"))
        if amount is None or amount <= 0:
            error = "Please enter valid prices for all products."
            break
        items.append(
            {
                "product": line.product,
                "price": amount,
                "note": request.POST.get(f"note_{line.product_id}") or "",
            }
        )
    if error:
        previous = []
        if submission.deal_round.round_number > 1:
            previous = [
                item
                for item in lowest_prices_for_previous_round(
                    company, submission.deal_no, submission.deal_round
                )
                if item.get("lowest") is not None
            ]
        form_lines = [
            {
                "line": line,
                "price_value": request.POST.get(f"price_{line.product_id}", ""),
                "note_value": request.POST.get(f"note_{line.product_id}", ""),
            }
            for line in lines
        ]
        return render(
            request,
            "business/vendor_price_submit.html",
            _public_context(
                submission,
                state="form",
                deal_no=submission.deal_no,
                lines=lines,
                form_lines=form_lines,
                previous_lowest=previous,
                error_message=error,
                company=company,
            ),
        )
    submit_vendor_prices(submission, items)
    return render(
        request,
        "business/vendor_price_submit.html",
        _public_context(
            submission,
            state="submitted",
            deal_no=submission.deal_no,
            lines=lines,
            form_lines=[{"line": line, "price_value": "", "note_value": ""} for line in lines],
            success_message="Prices submitted successfully. You can now close this window.",
            company=company,
            just_submitted=True,
        ),
    )
