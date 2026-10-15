"""Zoho Books invoice picker and subscription review."""

from __future__ import annotations

import logging

from django.contrib import messages
from django.forms import formset_factory
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods

from accounts.permissions import sales_module_required
from business.forms import ZohoSubscriptionLineForm
from business.views import _company, _subscription_product_options
from business.models import ZohoBooksSync
from business.zoho_books import (
    SESSION_INVOICE_IDS,
    ZohoBooksError,
    build_review_rows,
    create_subscriptions,
    get_bill_pdf,
    get_invoice,
    get_invoice_pdf,
    invoice_item_rows,
    list_organizations,
    list_stored_documents,
    local_organization,
    pdf_filename,
    resolve_organization,
    save_organization,
    stored_invoice_dict,
    sync_zoho_documents,
)
from business.zoho_crm import zoho_configured

logger = logging.getLogger(__name__)

ZohoLineFormSet = formset_factory(ZohoSubscriptionLineForm, extra=0)


def _selected_invoice_ids(request) -> list[str]:
    raw = request.session.get(SESSION_INVOICE_IDS) or []
    ids = [str(item) for item in raw if str(item).isdigit()]
    return ids[:1]


def _load_selected_invoices(company, organization_id: str, invoice_ids: list[str]) -> list[dict]:
    invoices = []
    for invoice_id in invoice_ids:
        stored = stored_invoice_dict(company, organization_id, invoice_id)
        if stored is not None:
            invoices.append(stored)
        else:
            invoices.append(get_invoice(company, organization_id, invoice_id))
    return invoices


def _line_initial(row: dict) -> dict:
    return {
        "include": True,
        "zoho_invoice_id": row["zoho_invoice_id"],
        "zoho_line_item_id": row["zoho_line_item_id"],
        "client": row["client_id"],
        "category_filter": row.get("category") or "",
        "company_name_filter": row.get("company_name") or "",
        "product": row["product_id"],
        "quantity": row["quantity"],
        "start_date": row["start_date"],
        "end_date": row["end_date"],
        "vendor": row["vendor_id"] or "",
        "billing_amount": row["billing_amount"],
        "invoice_number": row["invoice_number"],
        "serial_number": row["serial_number"],
        "subscription_code": "",
    }


def _attach_drafts(formset, drafts: list[dict]) -> None:
    by_key = {
        (row["zoho_invoice_id"], row["zoho_line_item_id"]): row for row in drafts
    }
    for form in formset.forms:
        key = (
            str(form["zoho_invoice_id"].value() or ""),
            str(form["zoho_line_item_id"].value() or ""),
        )
        form.draft = by_key.get(key)


def _duplicate_subscription_codes(company, formset) -> bool:
    existing = {
        (code or "").strip().casefold()
        for code in company.subscriptions.values_list("subscription_code", flat=True)
        if (code or "").strip()
    }
    seen: set[str] = set()
    blocked = False
    for form in formset.forms:
        if not form.cleaned_data.get("include"):
            continue
        code = (form.cleaned_data.get("subscription_code") or "").strip()
        if not code:
            continue
        key = code.casefold()
        if key in seen or key in existing:
            form.add_error(
                "subscription_code",
                "This subscription id is already used.",
            )
            blocked = True
        seen.add(key)
    return blocked


def _render_import(request, company, **extra):
    return render(
        request,
        "business/subscription_zoho_import.html",
        {
            "company": company,
            "zoho_configured": zoho_configured(),
            "organizations": extra.pop("organizations", []),
            "organization": extra.pop("organization", None),
            "invoices": extra.pop("invoices", []),
            "page_context": extra.pop("page_context", {}),
            "query": extra.pop("query", ""),
            "page": extra.pop("page", 1),
            "needs_reconnect": extra.pop("needs_reconnect", False),
            "products": extra.pop("products", []),
            "selected_item_ids": extra.pop("selected_item_ids", []),
            "filter_query": extra.pop("filter_query", ""),
            "document_kind": extra.pop("document_kind", ZohoBooksSync.Kind.INVOICE),
            **extra,
        },
    )


def _page_for(kind: str) -> str:
    if kind == ZohoBooksSync.Kind.BILL:
        return "subscription_zoho_bills"
    return "subscription_zoho_import"


def _zoho_books_page(request, kind: str):
    company = _company(request)
    if not company.zoho_connected:
        return _render_import(
            request,
            company,
            needs_reconnect=not zoho_configured(),
            document_kind=kind,
        )
    if not zoho_configured():
        messages.error(
            request,
            "Zoho app credentials are not configured on this server.",
        )
        return redirect("subscription_list")

    try:
        if request.method == "POST" and request.POST.get("action") == "save_org":
            organization = save_organization(company, request.POST.get("organization_id"))
            messages.success(
                request,
                f"Using Zoho Books organization {organization.get('name') or organization.get('organization_id')}.",
            )
            return redirect(_page_for(kind))

        organization = local_organization(company)
        if organization is None:
            organization = resolve_organization(company)
        if organization is None:
            return _render_import(
                request,
                company,
                organizations=list_organizations(company),
                document_kind=kind,
            )

        if (
            kind == ZohoBooksSync.Kind.INVOICE
            and request.method == "POST"
            and request.POST.get("action") == "review"
        ):
            invoice_ids = [
                invoice_id
                for invoice_id in request.POST.getlist("invoice_ids")
                if str(invoice_id).isdigit()
            ]
            if len(invoice_ids) != 1:
                messages.error(request, "Select one Zoho Books invoice.")
            else:
                request.session[SESSION_INVOICE_IDS] = invoice_ids
                request.session.modified = True
                return redirect("subscription_zoho_review")

        return _render_import(
            request,
            company,
            organization=organization,
            document_kind=kind,
        )
    except ZohoBooksError as exc:
        logger.warning("Zoho Books import list failed company_id=%s", company.pk)
        return _render_import(
            request,
            company,
            error=str(exc),
            needs_reconnect=exc.needs_reconnect,
            document_kind=kind,
        )


@sales_module_required
@require_http_methods(["GET", "POST"])
def subscription_zoho_import(request):
    return _zoho_books_page(request, ZohoBooksSync.Kind.INVOICE)


@sales_module_required
@require_http_methods(["GET", "POST"])
def subscription_zoho_bills(request):
    return _zoho_books_page(request, ZohoBooksSync.Kind.BILL)


@sales_module_required
@require_http_methods(["GET", "POST"])
def subscription_zoho_review(request):
    company = _company(request)
    invoice_ids = _selected_invoice_ids(request)
    if not company.zoho_connected or not invoice_ids:
        messages.error(request, "Select Zoho Books invoices before reviewing subscriptions.")
        return redirect("subscription_zoho_import")

    organization_id = (company.zoho_books_organization_id or "").strip()
    if not organization_id:
        return redirect("subscription_zoho_import")

    try:
        invoices = _load_selected_invoices(company, organization_id, invoice_ids)
    except ZohoBooksError as exc:
        messages.error(request, str(exc))
        if exc.needs_reconnect:
            return redirect("company_setup")
        return redirect("subscription_zoho_import")

    drafts = build_review_rows(company, invoices)
    pending = [row for row in drafts if not row["already_imported"]]
    imported = [row for row in drafts if row["already_imported"]]
    if not drafts:
        messages.error(request, "The selected invoices have no product lines.")
        return redirect("subscription_zoho_import")

    if request.method == "POST":
        formset = ZohoLineFormSet(request.POST, form_kwargs={"company": company})
        if formset.is_valid() and not _duplicate_subscription_codes(company, formset):
            selected = [
                form.cleaned_data
                for form in formset.forms
                if form.cleaned_data.get("include")
            ]
            allowed = {
                (row["zoho_invoice_id"], row["zoho_line_item_id"]) for row in pending
            }
            imported_keys = {
                (row["zoho_invoice_id"], row["zoho_line_item_id"]) for row in imported
            }
            rows = []
            already = 0
            for cleaned in selected:
                key = (cleaned["zoho_invoice_id"], cleaned["zoho_line_item_id"])
                if key in imported_keys:
                    already += 1
                    continue
                if key not in allowed:
                    messages.error(
                        request,
                        "One of the product lines is no longer on the Zoho invoice. Start the import again.",
                    )
                    return redirect("subscription_zoho_import")
                rows.append(
                    {
                        "zoho_invoice_id": cleaned["zoho_invoice_id"],
                        "zoho_line_item_id": cleaned["zoho_line_item_id"],
                        "client": cleaned["client"],
                        "product": cleaned["product"],
                        "vendor": cleaned.get("vendor"),
                        "quantity": cleaned["quantity"],
                        "start_date": cleaned["start_date"],
                        "end_date": cleaned.get("end_date"),
                        "billing_amount": cleaned["billing_amount"],
                        "invoice_number": (cleaned.get("invoice_number") or "").strip(),
                        "serial_number": (cleaned.get("serial_number") or "").strip(),
                        "subscription_code": (cleaned.get("subscription_code") or "").strip(),
                    }
                )
            if not rows:
                if already:
                    messages.warning(
                        request,
                        "Those product lines were already imported.",
                    )
                    request.session.pop(SESSION_INVOICE_IDS, None)
                    return redirect("subscription_list")
                messages.error(request, "Select at least one product line to import.")
            else:
                pdfs = {}
                missing_pdfs = []
                for invoice in invoices:
                    invoice_id = str(invoice.get("invoice_id") or "")
                    if invoice_id not in {row["zoho_invoice_id"] for row in rows}:
                        continue
                    try:
                        pdfs[invoice_id] = {
                            "name": pdf_filename(
                                invoice.get("invoice_number") or "",
                                invoice_id,
                            ),
                            "bytes": get_invoice_pdf(company, organization_id, invoice_id),
                        }
                    except ZohoBooksError:
                        logger.warning(
                            "Zoho invoice PDF download failed company_id=%s invoice_id=%s",
                            company.pk,
                            invoice_id,
                        )
                        missing_pdfs.append(invoice.get("invoice_number") or invoice_id)
                try:
                    created, skipped = create_subscriptions(company, rows, pdfs)
                except ZohoBooksError as exc:
                    messages.error(request, str(exc))
                else:
                    if created:
                        messages.success(
                            request,
                            f"Created {created} subscription(s) from Zoho Books.",
                        )
                    if skipped:
                        messages.warning(
                            request,
                            f"Skipped {skipped} product line(s) that were already imported.",
                        )
                    for number in missing_pdfs:
                        messages.warning(
                            request,
                            f"Invoice {number} was saved without a PDF.",
                        )
                    if created or skipped:
                        request.session.pop(SESSION_INVOICE_IDS, None)
                        return redirect("subscription_list")
                    messages.error(request, "No subscriptions were created.")
        _attach_drafts(formset, pending)
    else:
        formset = ZohoLineFormSet(
            initial=[_line_initial(row) for row in pending],
            form_kwargs={"company": company},
        )
        _attach_drafts(formset, pending)

    return render(
        request,
        "business/subscription_zoho_review.html",
        {
            "company": company,
            "formset": formset,
            "imported_rows": imported,
            "invoice_count": len(invoices),
            "invoice": invoices[0] if invoices else {},
            "product_options": _subscription_product_options(company),
        },
    )


@sales_module_required
@require_http_methods(["GET"])
def subscription_zoho_pdf(request, invoice_id: str):
    company = _company(request)
    if not str(invoice_id).isdigit() or not company.zoho_connected:
        raise Http404
    organization_id = (company.zoho_books_organization_id or "").strip()
    if not organization_id:
        raise Http404
    stored = stored_invoice_dict(company, organization_id, invoice_id)
    number = (stored or {}).get("invoice_number") or ""
    try:
        content = get_invoice_pdf(company, organization_id, invoice_id)
        if not number:
            invoice = get_invoice(company, organization_id, invoice_id)
            number = invoice.get("invoice_number") or ""
    except ZohoBooksError as exc:
        messages.error(request, str(exc))
        return redirect("subscription_zoho_import")
    filename = pdf_filename(number, invoice_id)
    response = HttpResponse(content, content_type="application/pdf")
    response["Content-Disposition"] = f'inline; filename="{filename}"'
    response["X-Frame-Options"] = "SAMEORIGIN"
    return response


def _invoice_page_number(request) -> int:
    try:
        return max(int(request.GET.get("page") or 1), 1)
    except ValueError:
        return 1


def _invoice_period(request) -> tuple[str, str]:
    year = (request.GET.get("year") or "").strip()
    month = (request.GET.get("month") or "").strip()
    if year.isdigit() and 2000 <= int(year) <= 2100:
        year = str(int(year))
    else:
        year = ""
    if month.isdigit() and 1 <= int(month) <= 12:
        month = str(int(month))
    else:
        month = ""
    return year, month


def _stored_document_data(request, kind: str):
    company = _company(request)
    if not company.zoho_connected or not zoho_configured():
        return JsonResponse({"error": "Zoho Books is not connected."}, status=400)
    organization_id = (company.zoho_books_organization_id or "").strip()
    if not organization_id:
        return JsonResponse({"error": "Choose a Zoho Books organization."}, status=400)
    year, month = _invoice_period(request)
    payload = list_stored_documents(
        company,
        organization_id,
        kind,
        page=_invoice_page_number(request),
        year=year,
        month=month,
        query=request.GET.get("q") or "",
        item_ids=request.GET.getlist("item_id"),
    )
    return JsonResponse(payload)


@sales_module_required
@require_http_methods(["GET"])
def subscription_zoho_invoice_data(request):
    """Saved sales invoices. This does not call Zoho."""
    return _stored_document_data(request, ZohoBooksSync.Kind.INVOICE)


@sales_module_required
@require_http_methods(["GET"])
def subscription_zoho_bill_data(request):
    """Saved purchase bills. This does not call Zoho."""
    return _stored_document_data(request, ZohoBooksSync.Kind.BILL)


@sales_module_required
@require_http_methods(["POST"])
def subscription_zoho_sync(request):
    """Save one Zoho page. Repeat until has_more is false."""
    company = _company(request)
    if not company.zoho_connected or not zoho_configured():
        return JsonResponse({"error": "Zoho Books is not connected."}, status=400)
    organization_id = (company.zoho_books_organization_id or "").strip()
    if not organization_id:
        return JsonResponse({"error": "Choose a Zoho Books organization."}, status=400)
    kind = (request.POST.get("kind") or ZohoBooksSync.Kind.INVOICE).strip()
    if kind not in {ZohoBooksSync.Kind.INVOICE, ZohoBooksSync.Kind.BILL}:
        return JsonResponse({"error": "Choose sales invoices or purchase bills."}, status=400)
    try:
        payload = sync_zoho_documents(
            company,
            organization_id,
            kind,
            start=request.POST.get("start") == "1",
        )
    except ZohoBooksError as exc:
        return JsonResponse(
            {"error": str(exc), "needs_reconnect": exc.needs_reconnect},
            status=400,
        )
    return JsonResponse(payload)


@sales_module_required
@require_http_methods(["GET"])
def subscription_zoho_invoice_items(request, invoice_id: str):
    company = _company(request)
    if not str(invoice_id).isdigit() or not company.zoho_connected:
        return JsonResponse({"error": "Invoice not found."}, status=404)
    organization_id = (company.zoho_books_organization_id or "").strip()
    if not organization_id:
        return JsonResponse({"error": "Zoho Books organization is not selected."}, status=404)
    stored = stored_invoice_dict(company, organization_id, invoice_id)
    if stored is not None:
        return JsonResponse(
            {
                "currency": stored.get("currency_code") or "",
                "items": invoice_item_rows(stored),
            }
        )
    try:
        invoice = get_invoice(company, organization_id, invoice_id)
    except ZohoBooksError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    return JsonResponse(
        {
            "currency": invoice.get("currency_code") or "",
            "items": invoice_item_rows(invoice),
        }
    )


@sales_module_required
@require_http_methods(["GET"])
def subscription_zoho_bill_pdf(request, bill_id: str):
    company = _company(request)
    if not str(bill_id).isdigit() or not company.zoho_connected:
        raise Http404
    organization_id = (company.zoho_books_organization_id or "").strip()
    if not organization_id:
        raise Http404
    document = company.zoho_books_documents.filter(
        organization_id=organization_id,
        kind=ZohoBooksSync.Kind.BILL,
        zoho_id=bill_id,
    ).first()
    number = document.number if document else ""
    try:
        content = get_bill_pdf(company, organization_id, bill_id)
    except ZohoBooksError as exc:
        messages.error(request, str(exc))
        return redirect("subscription_zoho_bills")
    filename = pdf_filename(number or bill_id, bill_id)
    response = HttpResponse(content, content_type="application/pdf")
    response["Content-Disposition"] = f'inline; filename="{filename}"'
    response["X-Frame-Options"] = "SAMEORIGIN"
    return response
