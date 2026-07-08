import csv
from datetime import timedelta
from io import StringIO

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.db.models import Count, Q, Sum
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods
from openpyxl import Workbook

from accounts.models import CompanyProfile
from accounts.permissions import (
    company_owner_required,
    company_users,
    get_company_for_user,
    sales_module_required,
)

from .forms import (
    CaseLockInboxForm,
    CaseLockPublicForm,
    ClientForm,
    ClientSubscriptionForm,
    SubscriptionRenewForm,
    ProductForm,
    SupportTicketCommentForm,
    SupportTicketForm,
    VendorForm,
)
from .models import (
    CaseLockInbox,
    CaseLockInvite,
    CaseLockSubmission,
    Client,
    ClientSubscription,
    Product,
    SupportTicket,
    Vendor,
)
from .caselock_inbox import send_caselock_decision_email
from .import_stash import product_import_stash, subscription_import_stash
from .subscription_import import (
    IMPORT_HEADERS,
    IMPORT_SAMPLE_ROW,
    PRESET_VENDOR_CHOICES,
    build_mapping_form_rows,
    column_mapping_from_post,
    ensure_import_vendor,
    import_subscriptions_from_file,
    resolve_import_vendor_name,
    suggest_column_mapping,
    validate_column_mapping,
    count_subscription_data_rows,
    detect_import_header,
    _cell_str,
    _read_header_row_raw,
)

User = get_user_model()


def _company(request):
    company = get_company_for_user(request.user)
    if company is None:
        return get_object_or_404(CompanyProfile, user=request.user)
    return company


def _delete_success_message(
    request, entity_count: int, sub_count: int, entity_label: str
) -> None:
    if entity_count == 0:
        messages.warning(request, f"No {entity_label} were deleted.")
        return
    msg = f"Deleted {entity_count} {entity_label}."
    if sub_count:
        msg += f" Removed {sub_count} related subscription(s)."
    messages.success(request, msg)


def _delete_clients(company, qs) -> tuple[int, int]:
    sub_count = company.subscriptions.filter(client__in=qs).count()
    entity_count = qs.count()
    if entity_count:
        qs.delete()
    return entity_count, sub_count


def _delete_products(company, qs) -> tuple[int, int]:
    sub_count = company.subscriptions.filter(product__in=qs).count()
    entity_count = qs.count()
    if entity_count:
        qs.delete()
    return entity_count, sub_count


def _delete_vendors(company, qs) -> tuple[int, int]:
    sub_count = company.subscriptions.filter(vendor__in=qs).count()
    entity_count = qs.count()
    if entity_count:
        qs.delete()
    return entity_count, sub_count


EXPIRING_SOON_DAYS = (90, 45, 30, 15, 7)


def _parse_expiring_days(raw: str, *, default: int = 30) -> int:
    try:
        days = int(raw)
    except (TypeError, ValueError):
        return default
    if days in EXPIRING_SOON_DAYS:
        return days
    return default


def _filter_expiring_soon(qs, days: int, today=None):
    today = today or timezone.now().date()
    until = today + timedelta(days=days)
    return qs.filter(
        end_date__isnull=False,
        end_date__gte=today,
        end_date__lte=until,
    )


def _expiring_subscription_choices():
    return [("", "All")] + [
        (str(days), f"Next {days} days") for days in EXPIRING_SOON_DAYS
    ]


@sales_module_required
def dashboard(request):
    company = _company(request)
    expiring_days = _parse_expiring_days(request.GET.get("expiring_days", ""))
    today = timezone.now().date()
    expiring_qs = _filter_expiring_soon(
        company.subscriptions.filter(status=ClientSubscription.Status.ACTIVE),
        expiring_days,
        today,
    )
    expiring_subs = expiring_qs.select_related("client", "product", "vendor").order_by(
        "end_date", "client__name"
    )[:50]
    ctx = {
        "company": company,
        "counts": {
            "vendors": company.vendors.count(),
            "clients": company.clients.count(),
            "products": company.products.count(),
            "subscriptions": company.subscriptions.count(),
        },
        "active_subs": company.subscriptions.filter(status=ClientSubscription.Status.ACTIVE).count(),
        "recent_subs": company.subscriptions.select_related("client", "product")[:8],
        "revenue_hint": company.subscriptions.filter(
            status=ClientSubscription.Status.ACTIVE
        ).aggregate(total=Sum("billing_amount"))["total"]
        or 0,
        "expiring_days": expiring_days,
        "expiring_filters": EXPIRING_SOON_DAYS,
        "expiring_subs": expiring_subs,
        "expiring_count": expiring_qs.count(),
        "expiring_list_url": (
            f"{reverse('subscription_list')}?expiring_soon={expiring_days}"
        ),
        "dashboard_renew_next": (
            f"{reverse('dashboard')}?expiring_days={expiring_days}"
        ),
    }
    return render(request, "business/dashboard.html", ctx)


def _crud_list(request, model, template_name, search_fields):
    company = _company(request)
    q = request.GET.get("q", "").strip()
    qs = model.objects.filter(company=company)
    if q:
        from django.db.models import Q

        cond = Q()
        for f in search_fields:
            cond |= Q(**{f"{f}__icontains": q})
        qs = qs.filter(cond)
    return render(
        request,
        template_name,
        {"company": company, "object_list": qs, "q": q},
    )


@sales_module_required
def vendor_list(request):
    company = _company(request)
    q = request.GET.get("q", "").strip()
    qs = company.vendors.all().order_by("name")
    if q:
        qs = qs.filter(
            Q(name__icontains=q)
            | Q(gst_number__icontains=q)
            | Q(email__icontains=q)
            | Q(email_2__icontains=q)
            | Q(email_3__icontains=q)
            | Q(phone__icontains=q)
            | Q(phone_2__icontains=q)
            | Q(phone_3__icontains=q)
        )
    return render(
        request,
        "business/vendor_list.html",
        {"company": company, "object_list": qs, "q": q},
    )


@sales_module_required
@require_http_methods(["POST"])
def vendor_bulk_delete(request):
    company = _company(request)
    ids = request.POST.getlist("vendor_ids")
    if not ids:
        messages.warning(request, "Select at least one vendor to delete.")
        return redirect("vendor_list")
    qs = company.vendors.filter(pk__in=ids)
    n, sub_count = _delete_vendors(company, qs)
    _delete_success_message(request, n, sub_count, "vendor(s)")
    return redirect("vendor_list")


@sales_module_required
@require_http_methods(["POST"])
def vendor_delete(request, pk):
    company = _company(request)
    obj = get_object_or_404(Vendor, pk=pk, company=company)
    n, sub_count = _delete_vendors(company, company.vendors.filter(pk=obj.pk))
    _delete_success_message(request, n, sub_count, "vendor")
    return redirect("vendor_list")


@sales_module_required
def vendor_export(request):
    from .vendor_import import build_vendor_export_bytes

    company = _company(request)
    content = build_vendor_export_bytes(company)
    response = HttpResponse(
        content,
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    response["Content-Disposition"] = 'attachment; filename="vendors_export.xlsx"'
    return response


def _vendor_import_messages(
    request, created: int, updated: int, errs: list[tuple[int, str]]
) -> None:
    if created:
        messages.success(request, f"Imported {created} new vendor(s).")
    if updated:
        messages.success(request, f"Updated {updated} existing vendor(s).")
    if not created and not updated and not errs:
        messages.info(request, "No changes were made.")
    for row_no, msg in errs[:25]:
        if row_no:
            messages.warning(request, f"Row {row_no}: {msg}")
        else:
            messages.error(request, msg)
    if len(errs) > 25:
        messages.warning(
            request,
            f"{len(errs) - 25} more row(s) had issues (not listed).",
        )


@sales_module_required
@require_http_methods(["GET", "POST"])
def vendor_import(request):
    company = _company(request)
    if request.method == "POST":
        upload = request.FILES.get("file")
        if not upload:
            messages.error(request, "Choose an Excel or CSV file to upload.")
            return redirect("vendor_import")
        name_lower = (upload.name or "").lower()
        if not (name_lower.endswith(".xlsx") or name_lower.endswith(".csv")):
            messages.error(request, "Please upload a .xlsx or .csv file.")
            return redirect("vendor_import")
        from .vendor_import import import_vendors_from_file

        created, updated, errs = import_vendors_from_file(
            company,
            upload.read(),
            upload.name or "",
        )
        _vendor_import_messages(request, created, updated, errs)
        return redirect("vendor_list")

    return render(request, "business/vendor_import.html", {"company": company})


@sales_module_required
def vendor_add(request):
    company = _company(request)
    if request.method == "POST":
        form = VendorForm(request.POST)
        if form.is_valid():
            v = form.save(commit=False)
            v.company = company
            v.save()
            return redirect("vendor_list")
    else:
        form = VendorForm()
    return render(
        request,
        "business/vendor_form.html",
        {"company": company, "form": form, "title": "Add vendor"},
    )


@sales_module_required
def vendor_edit(request, pk):
    company = _company(request)
    obj = get_object_or_404(Vendor, pk=pk, company=company)
    if request.method == "POST":
        form = VendorForm(request.POST, instance=obj)
        if form.is_valid():
            form.save()
            return redirect("vendor_list")
    else:
        form = VendorForm(instance=obj)
    return render(
        request,
        "business/vendor_form.html",
        {"company": company, "form": form, "title": "Edit vendor", "object": obj},
    )


@sales_module_required
def client_list(request):
    return _crud_list(
        request, Client, "business/client_list.html", ("name", "email", "phone", "gst_number")
    )


@sales_module_required
def client_export(request):
    from .client_import import build_client_export_bytes

    company = _company(request)
    content = build_client_export_bytes(company)
    response = HttpResponse(
        content,
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    response["Content-Disposition"] = 'attachment; filename="clients_export.xlsx"'
    return response


def _client_import_messages(
    request, created: int, updated: int, errs: list[tuple[int, str]]
) -> None:
    if created:
        messages.success(request, f"Imported {created} new client(s).")
    if updated:
        messages.success(request, f"Updated {updated} existing client(s).")
    if not created and not updated and not errs:
        messages.info(request, "No changes were made.")
    for row_no, msg in errs[:25]:
        if row_no:
            messages.warning(request, f"Row {row_no}: {msg}")
        else:
            messages.error(request, msg)
    if len(errs) > 25:
        messages.warning(
            request,
            f"{len(errs) - 25} more row(s) had issues (not listed).",
        )


@sales_module_required
@require_http_methods(["GET", "POST"])
def client_import(request):
    company = _company(request)
    if request.method == "POST":
        upload = request.FILES.get("file")
        if not upload:
            messages.error(request, "Choose an Excel or CSV file to upload.")
            return redirect("client_import")
        name_lower = (upload.name or "").lower()
        if not (name_lower.endswith(".xlsx") or name_lower.endswith(".csv")):
            messages.error(request, "Please upload a .xlsx or .csv file.")
            return redirect("client_import")
        from .client_import import import_clients_from_file

        created, updated, errs = import_clients_from_file(
            company,
            upload.read(),
            upload.name or "",
        )
        _client_import_messages(request, created, updated, errs)
        return redirect("client_list")

    return render(request, "business/client_import.html", {"company": company})


@sales_module_required
def client_add(request):
    company = _company(request)
    if request.method == "POST":
        form = ClientForm(request.POST)
        if form.is_valid():
            c = form.save(commit=False)
            c.company = company
            c.save()
            return redirect("client_list")
    else:
        form = ClientForm()
    return render(
        request,
        "business/client_form.html",
        {
            "company": company,
            "form": form,
            "title": "Add Client",
            "subtitle": "Create client for client subscription mapping",
        },
    )


@sales_module_required
def client_edit(request, pk):
    company = _company(request)
    obj = get_object_or_404(Client, pk=pk, company=company)
    if request.method == "POST":
        form = ClientForm(request.POST, instance=obj)
        if form.is_valid():
            form.save()
            return redirect("client_list")
    else:
        form = ClientForm(instance=obj)
    return render(
        request,
        "business/client_form.html",
        {
            "company": company,
            "form": form,
            "title": "Edit Client",
            "subtitle": "Update client for client subscription mapping",
            "object": obj,
        },
    )


@sales_module_required
@require_http_methods(["POST"])
def client_delete(request, pk):
    company = _company(request)
    obj = get_object_or_404(Client, pk=pk, company=company)
    n, sub_count = _delete_clients(company, company.clients.filter(pk=obj.pk))
    _delete_success_message(request, n, sub_count, "client")
    return redirect("client_list")


@sales_module_required
def product_list(request):
    company = _company(request)
    q = request.GET.get("q", "").strip()
    qs = company.products.all().order_by("name")
    if q:
        qs = qs.filter(
            Q(name__icontains=q)
            | Q(sku__icontains=q)
            | Q(category__icontains=q)
            | Q(asset_type__icontains=q)
            | Q(company_name__icontains=q)
            | Q(hsn_code__icontains=q)
            | Q(company_email__icontains=q)
            | Q(components__icontains=q)
            | Q(notes__icontains=q)
            | Q(description__icontains=q)
        )
    return render(
        request,
        "business/product_list.html",
        {"company": company, "object_list": qs, "q": q},
    )


@sales_module_required
@require_http_methods(["POST"])
def product_bulk_delete(request):
    company = _company(request)
    ids = request.POST.getlist("product_ids")
    if not ids:
        messages.warning(request, "Select at least one product to delete.")
        return redirect("product_list")
    qs = company.products.filter(pk__in=ids)
    n, sub_count = _delete_products(company, qs)
    _delete_success_message(request, n, sub_count, "product(s)")
    return redirect("product_list")


@sales_module_required
@require_http_methods(["POST"])
def product_delete(request, pk):
    company = _company(request)
    obj = get_object_or_404(Product, pk=pk, company=company)
    n, sub_count = _delete_products(company, company.products.filter(pk=obj.pk))
    _delete_success_message(request, n, sub_count, "product")
    return redirect("product_list")


def _stash_product_import(request, file_bytes: bytes, filename: str) -> str:
    from .product_import import (
        auto_column_mapping,
        count_product_data_rows,
        detect_import_header,
        read_import_headers as read_product_import_headers,
    )

    header_row_index, raw_headers = detect_import_header(file_bytes, filename)
    headers = read_product_import_headers(file_bytes, filename)
    return product_import_stash.stash(
        request,
        file_bytes,
        {
            "filename": filename,
            "headers": headers,
            "suggested": auto_column_mapping(raw_headers),
            "header_row_index": header_row_index,
            "data_row_count": count_product_data_rows(
                file_bytes, filename, header_row_index
            ),
        },
    )


def _product_import_messages(
    request, created: int, updated: int, errs: list
) -> None:
    if created or updated:
        parts = []
        if created:
            parts.append(f"{created} added")
        if updated:
            parts.append(f"{updated} updated")
        messages.success(request, f"Import complete: {', '.join(parts)}.")
    for row_no, msg in errs[:25]:
        if row_no:
            messages.warning(request, f"Row {row_no}: {msg}")
        else:
            messages.error(request, msg)
    if len(errs) > 25:
        messages.warning(
            request,
            f"{len(errs) - 25} more row(s) had issues (not listed).",
        )


@sales_module_required
def product_export(request):
    from .product_bulk_edit import build_product_export_bytes

    company = _company(request)
    content = build_product_export_bytes(company)
    response = HttpResponse(
        content,
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    response["Content-Disposition"] = 'attachment; filename="products_export.xlsx"'
    return response


def _product_bulk_import_messages(
    request, created: int, updated: int, errs: list[tuple[int, str]]
) -> None:
    if created:
        messages.success(request, f"Imported {created} new product(s).")
    if updated:
        messages.success(request, f"Updated {updated} existing product(s).")
    if not created and not updated and not errs:
        messages.info(request, "No changes were made.")
    for row_no, msg in errs[:25]:
        if row_no:
            messages.warning(request, f"Row {row_no}: {msg}")
        else:
            messages.error(request, msg)
    if len(errs) > 25:
        messages.warning(
            request,
            f"{len(errs) - 25} more row(s) had issues (not listed).",
        )


@sales_module_required
@require_http_methods(["GET", "POST"])
def product_import(request):
    company = _company(request)
    if request.method == "POST":
        upload = request.FILES.get("file")
        if not upload:
            messages.error(request, "Choose an Excel or CSV file to upload.")
            return redirect("product_import")
        name_lower = (upload.name or "").lower()
        if not (name_lower.endswith(".xlsx") or name_lower.endswith(".csv")):
            messages.error(request, "Please upload a .xlsx or .csv file.")
            return redirect("product_import")
        from .product_bulk_edit import import_products_bulk_from_file

        created, updated, errs = import_products_bulk_from_file(
            company,
            upload.read(),
            upload.name or "",
        )
        _product_bulk_import_messages(request, created, updated, errs)
        return redirect("product_list")

    return render(request, "business/product_import.html", {"company": company})


@sales_module_required
@require_http_methods(["GET", "POST"])
def product_import_advanced(request):
    company = _company(request)
    if request.method == "POST":
        upload = request.FILES.get("file")
        if not upload:
            messages.error(request, "Choose a .xlsx or .csv file to upload.")
            return redirect("product_import_advanced")
        name_lower = (upload.name or "").lower()
        if not (name_lower.endswith(".xlsx") or name_lower.endswith(".csv")):
            messages.error(request, "Please upload a .xlsx or .csv file.")
            return redirect("product_import_advanced")
        _stash_product_import(request, upload.read(), upload.name or "")
        messages.info(request, "Review the column mapping, then run import.")
        return redirect("product_import_map")

    return render(request, "business/product_import_advanced.html", {"company": company})


@sales_module_required
def product_import_template(request):
    from .product_import import IMPORT_HEADERS, IMPORT_SAMPLE_ROW

    _company(request)
    fmt = (request.GET.get("format") or "xlsx").lower()
    if fmt == "csv":
        buf = StringIO()
        writer = csv.writer(buf)
        writer.writerow(IMPORT_HEADERS)
        writer.writerow(IMPORT_SAMPLE_ROW)
        response = HttpResponse(buf.getvalue(), content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = (
            'attachment; filename="product_import_template.csv"'
        )
        return response

    wb = Workbook()
    ws = wb.active
    ws.title = "Products"
    ws.append(list(IMPORT_HEADERS))
    ws.append(list(IMPORT_SAMPLE_ROW))
    response = HttpResponse(
        content_type=(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
    )
    response["Content-Disposition"] = (
        'attachment; filename="product_import_template.xlsx"'
    )
    wb.save(response)
    return response


@sales_module_required
@require_http_methods(["GET", "POST"])
def product_import_map(request):
    from .product_import import (
        build_mapping_form_rows,
        column_mapping_from_post,
        import_products_from_file,
        validate_column_mapping,
    )

    company = _company(request)
    token, data = product_import_stash.get(request)
    if not data:
        messages.error(
            request,
            "No import file in progress. Upload your Excel or CSV file again.",
        )
        return redirect("product_import_advanced")

    file_headers = data["headers"]
    header_count = len(file_headers)

    if request.method == "POST":
        col_map = column_mapping_from_post(
            request.POST, suggested=data.get("suggested")
        )
        if any(idx >= header_count for idx in col_map.values()):
            messages.error(request, "Invalid column selection. Try again.")
        else:
            map_errors = validate_column_mapping(col_map)
            if map_errors:
                for err in map_errors:
                    messages.error(request, err)
            else:
                file_bytes = product_import_stash.read_bytes(data)
                if not file_bytes:
                    messages.error(
                        request,
                        "Uploaded file could not be read. Please upload again.",
                    )
                    return redirect("product_import_advanced")
                created, updated, errs = import_products_from_file(
                    company,
                    file_bytes,
                    data["filename"],
                    column_mapping=col_map,
                    header_row_index=data.get("header_row_index", 0),
                )
                product_import_stash.clear(request, token)
                _product_import_messages(request, created, updated, errs)
                return redirect("product_list")

    mapping_rows = build_mapping_form_rows(
        file_headers,
        data.get("suggested") or {},
    )
    return render(
        request,
        "business/product_import_map.html",
        {
            "company": company,
            "filename": data["filename"],
            "mapping_rows": mapping_rows,
            "file_headers": file_headers,
            "data_row_count": data.get("data_row_count"),
            "header_row_number": (data.get("header_row_index") or 0) + 1,
        },
    )


@sales_module_required
def product_add(request):
    company = _company(request)
    if request.method == "POST":
        form = ProductForm(request.POST, company=company)
        if form.is_valid():
            p = form.save(commit=False)
            p.company = company
            p.save()
            return redirect("product_list")
    else:
        form = ProductForm(company=company)
    return render(
        request,
        "business/product_form.html",
        {"company": company, "form": form, "title": "Add product"},
    )


@sales_module_required
def product_edit(request, pk):
    company = _company(request)
    obj = get_object_or_404(Product, pk=pk, company=company)
    if request.method == "POST":
        form = ProductForm(request.POST, instance=obj, company=company)
        if form.is_valid():
            form.save()
            return redirect("product_list")
    else:
        form = ProductForm(instance=obj, company=company)
    return render(
        request,
        "business/product_form.html",
        {"company": company, "form": form, "title": "Edit product", "object": obj},
    )


@sales_module_required
def subscription_list(request):
    company = _company(request)
    base_qs = company.subscriptions.select_related("client", "product", "vendor")
    total_count = base_qs.count()
    qs = base_qs.all()
    today = timezone.now().date()

    client_id = request.GET.get("client", "").strip()
    if client_id:
        qs = qs.filter(client_id=client_id)
    product_id = request.GET.get("product", "").strip()
    if product_id:
        qs = qs.filter(product_id=product_id)
    vendor_id = request.GET.get("vendor", "").strip()
    if vendor_id:
        qs = qs.filter(vendor_id=vendor_id)
    category = request.GET.get("category", "").strip()
    if category:
        qs = qs.filter(product__category__iexact=category)

    fd_start = request.GET.get("start_date", "").strip()
    fd_end = request.GET.get("end_date", "").strip()
    if fd_start:
        qs = qs.filter(start_date__gte=fd_start)
    if fd_end:
        qs = qs.filter(end_date__lte=fd_end)

    expiring = request.GET.get("expiring_soon", "").strip()
    if expiring:
        days = _parse_expiring_days(expiring, default=0)
        if days:
            qs = _filter_expiring_soon(qs, days, today)

    categories = (
        company.products.exclude(category="")
        .values_list("category", flat=True)
        .distinct()
        .order_by("category")
    )

    has_active_filters = any(
        (
            client_id,
            product_id,
            vendor_id,
            category,
            fd_start,
            fd_end,
            expiring,
        )
    )
    filtered_count = qs.count()

    return render(
        request,
        "business/subscription_list.html",
        {
            "company": company,
            "object_list": qs,
            "total_count": total_count,
            "filtered_count": filtered_count,
            "has_active_filters": has_active_filters,
            "clients": company.clients.all().order_by("name"),
            "products": company.products.all().order_by("name"),
            "vendors": company.vendors.all().order_by("name"),
            "categories": list(categories),
            "filters": {
                "client": client_id,
                "product": product_id,
                "vendor": vendor_id,
                "category": category,
                "start_date": fd_start,
                "end_date": fd_end,
                "expiring_soon": expiring,
            },
            "expiring_choices": _expiring_subscription_choices(),
        },
    )


def _stash_import_upload(
    request,
    file_bytes: bytes,
    filename: str,
    vendor_id: int,
    vendor_name: str,
) -> str:
    from .subscription_import import read_import_headers as read_sub_import_headers

    header_row_index, raw_headers = detect_import_header(file_bytes, filename)
    headers = read_sub_import_headers(file_bytes, filename)
    return subscription_import_stash.stash(
        request,
        file_bytes,
        {
            "filename": filename,
            "headers": headers,
            "suggested": suggest_column_mapping(raw_headers),
            "vendor_id": vendor_id,
            "vendor_name": vendor_name,
            "header_row_index": header_row_index,
            "data_row_count": count_subscription_data_rows(
                file_bytes, filename, header_row_index
            ),
        },
    )


def _subscription_import_messages(
    request,
    created: int,
    updated: int,
    errs: list,
    *,
    vendor_name: str = "",
) -> None:
    total = created + updated
    if total:
        parts = []
        if created:
            parts.append(f"{created} new")
        if updated:
            parts.append(f"{updated} updated")
        msg = f"Import complete: {', '.join(parts)} subscription(s)."
        if vendor_name:
            msg += f" Showing subscriptions for {vendor_name}."
        messages.success(request, msg)
    elif errs:
        messages.error(
            request,
            f"No subscriptions imported ({len(errs)} row error(s)). "
            "Check the mapping and error list below.",
        )
    for row_no, msg in errs[:25]:
        if row_no:
            messages.warning(request, f"Row {row_no}: {msg}")
        else:
            messages.error(request, msg)
    if len(errs) > 25:
        messages.warning(
            request,
            f"{len(errs) - 25} more row(s) had issues (not listed).",
        )


@sales_module_required
def subscription_import_template(request):
    _company(request)
    fmt = (request.GET.get("format") or "xlsx").lower()
    if fmt == "csv":
        buf = StringIO()
        writer = csv.writer(buf)
        writer.writerow(IMPORT_HEADERS)
        writer.writerow(IMPORT_SAMPLE_ROW)
        response = HttpResponse(buf.getvalue(), content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = (
            'attachment; filename="subscription_import_template.csv"'
        )
        return response

    wb = Workbook()
    ws = wb.active
    ws.title = "Subscriptions"
    ws.append(list(IMPORT_HEADERS))
    ws.append(list(IMPORT_SAMPLE_ROW))
    response = HttpResponse(
        content_type=(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
    )
    response["Content-Disposition"] = (
        'attachment; filename="subscription_import_template.xlsx"'
    )
    wb.save(response)
    return response


@sales_module_required
@require_http_methods(["GET", "POST"])
def subscription_import(request):
    company = _company(request)
    if request.method == "POST":
        vendor_name = resolve_import_vendor_name(
            request.POST.get("vendor_choice", ""),
            request.POST.get("vendor_other_name", ""),
        )
        if not vendor_name:
            messages.error(
                request,
                "Choose a vendor (Redington, Tata, or Other with a name).",
            )
            return redirect("subscription_import")
        upload = request.FILES.get("file")
        if not upload:
            messages.error(request, "Choose a .xlsx or .csv file to upload.")
            return redirect("subscription_import")
        name_lower = (upload.name or "").lower()
        if not (name_lower.endswith(".xlsx") or name_lower.endswith(".csv")):
            messages.error(request, "Please upload a .xlsx or .csv file.")
            return redirect("subscription_import")
        vendor = ensure_import_vendor(company, vendor_name)
        _stash_import_upload(
            request,
            upload.read(),
            upload.name or "",
            vendor.pk,
            vendor.name,
        )
        messages.info(request, "Map your file columns, then run import.")
        return redirect("subscription_import_map")

    return render(
        request,
        "business/subscription_import.html",
        {
            "company": company,
            "vendor_choices": PRESET_VENDOR_CHOICES,
        },
    )


def _render_subscription_import_map(
    request,
    *,
    company,
    data,
    import_vendor,
    file_headers,
    mapping_rows,
    import_errors,
):
    return render(
        request,
        "business/subscription_import_map.html",
        {
            "company": company,
            "filename": data["filename"],
            "vendor_name": data.get("vendor_name") or import_vendor.name,
            "mapping_rows": mapping_rows,
            "file_headers": file_headers,
            "import_errors": import_errors,
            "data_row_count": data.get("data_row_count"),
            "header_row_number": (data.get("header_row_index") or 0) + 1,
        },
    )


@sales_module_required
@require_http_methods(["GET", "POST"])
def subscription_import_map(request):
    company = _company(request)
    token, data = subscription_import_stash.get(request)
    if not data:
        messages.error(
            request,
            "No import file in progress. Upload your Excel or CSV file again.",
        )
        return redirect("subscription_import")

    file_headers = data["headers"]
    header_count = len(file_headers)

    import_vendor = get_object_or_404(
        Vendor, pk=data["vendor_id"], company=company
    )

    if request.method == "POST":
        col_map = column_mapping_from_post(request.POST)
        mapping_rows = build_mapping_form_rows(file_headers, col_map)
        invalid_idx = [idx for idx in col_map.values() if idx >= header_count]
        if invalid_idx:
            messages.error(request, "Invalid column selection. Try again.")
            return _render_subscription_import_map(
                request,
                company=company,
                data=data,
                import_vendor=import_vendor,
                file_headers=file_headers,
                mapping_rows=mapping_rows,
                import_errors=[],
            )

        map_errors = validate_column_mapping(col_map)
        if map_errors:
            for err in map_errors:
                messages.error(request, err)
            return _render_subscription_import_map(
                request,
                company=company,
                data=data,
                import_vendor=import_vendor,
                file_headers=file_headers,
                mapping_rows=mapping_rows,
                import_errors=[],
            )

        file_bytes = subscription_import_stash.read_bytes(data)
        if not file_bytes:
            messages.error(
                request,
                "Uploaded file could not be read. Please upload again.",
            )
            return redirect("subscription_import")

        created, updated, errs = import_subscriptions_from_file(
            company,
            file_bytes,
            data["filename"],
            column_mapping=col_map,
            import_vendor=import_vendor,
            header_row_index=data.get("header_row_index", 0),
        )
        _subscription_import_messages(
            request,
            created,
            updated,
            errs,
            vendor_name=import_vendor.name,
        )
        if created or updated:
            subscription_import_stash.clear(request, token)
            return redirect(
                f"{reverse('subscription_list')}?vendor={import_vendor.pk}"
            )
        return _render_subscription_import_map(
            request,
            company=company,
            data=data,
            import_vendor=import_vendor,
            file_headers=file_headers,
            mapping_rows=mapping_rows,
            import_errors=errs[:30],
        )

    mapping_rows = build_mapping_form_rows(
        file_headers,
        data.get("suggested") or {},
    )
    return _render_subscription_import_map(
        request,
        company=company,
        data=data,
        import_vendor=import_vendor,
        file_headers=file_headers,
        mapping_rows=mapping_rows,
        import_errors=[],
    )


@sales_module_required
def subscription_add(request):
    company = _company(request)
    asset_type = (
        request.POST.get("asset_type") or request.GET.get("asset_type", "")
    ).strip()
    if request.method == "POST":
        form = ClientSubscriptionForm(
            request.POST,
            company=company,
            asset_type_prefilter=asset_type or None,
        )
        if form.is_valid():
            s = form.save(commit=False)
            s.company = company
            s.save()
            return redirect("subscription_list")
    else:
        form = ClientSubscriptionForm(
            company=company,
            asset_type_prefilter=asset_type or None,
        )
    if asset_type:
        title = f"Add Client {asset_type.title()} Subscription"
    else:
        title = "Add subscription"
    subtitle = "Same workflow as subscriptions, using client instead of user"
    return render(
        request,
        "business/subscription_form.html",
        {
            "company": company,
            "form": form,
            "title": title,
            "subtitle": subtitle,
            "asset_type_locked": asset_type or "",
        },
    )


def _safe_next_url(raw: str, default: str) -> str:
    next_url = (raw or "").strip()
    if next_url.startswith("/") and not next_url.startswith("//"):
        return next_url
    return default


@sales_module_required
@require_http_methods(["GET", "POST"])
def subscription_renew(request, pk):
    company = _company(request)
    obj = get_object_or_404(
        ClientSubscription.objects.select_related("client", "product", "vendor"),
        pk=pk,
        company=company,
    )
    default_next = reverse("dashboard")
    next_url = _safe_next_url(
        request.POST.get("next") or request.GET.get("next"),
        default_next,
    )
    suggested_start, suggested_end = obj.renewal_dates()

    if request.method == "POST":
        form = SubscriptionRenewForm(request.POST)
        if form.is_valid():
            new_start = form.cleaned_data["start_date"]
            new_end = form.cleaned_data["end_date"]
            obj.start_date = new_start
            obj.end_date = new_end
            obj.status = ClientSubscription.Status.ACTIVE
            obj.save(update_fields=["start_date", "end_date", "status"])
            end_label = new_end.strftime("%d %b %Y") if new_end else "no end date"
            messages.success(
                request,
                f"Renewed {obj.client.name} — {obj.product.name}: "
                f"{new_start.strftime('%d %b %Y')} to {end_label}.",
            )
            return redirect(next_url)
    else:
        form = SubscriptionRenewForm(
            initial={
                "start_date": suggested_start,
                "end_date": suggested_end,
            }
        )

    return render(
        request,
        "business/subscription_renew.html",
        {
            "company": company,
            "subscription": obj,
            "form": form,
            "next_url": next_url,
            "suggested_start": suggested_start,
            "suggested_end": suggested_end,
        },
    )


@sales_module_required
def subscription_edit(request, pk):
    company = _company(request)
    obj = get_object_or_404(ClientSubscription, pk=pk, company=company)
    if request.method == "POST":
        form = ClientSubscriptionForm(
            request.POST, instance=obj, company=company
        )
        if form.is_valid():
            form.save()
            return redirect("subscription_list")
    else:
        form = ClientSubscriptionForm(instance=obj, company=company)
    at = (obj.product.asset_type or "").strip()
    title = (
        f"Edit Client {at.title()} Subscription" if at else "Edit subscription"
    )
    subtitle = "Same workflow as subscriptions, using client instead of user"
    return render(
        request,
        "business/subscription_form.html",
        {
            "company": company,
            "form": form,
            "title": title,
            "subtitle": subtitle,
            "object": obj,
            "asset_type_locked": "",
        },
    )


@company_owner_required
def support_ticket_list(request):
    company = _company(request)

    status_filter = request.GET.get("status", "").strip()
    source_filter = request.GET.get("source", "internal").strip()

    base_qs = company.support_tickets.select_related("created_by", "assignee")
    if source_filter == "external":
        base_qs = base_qs.exclude(source_sender="")
    elif source_filter == "all":
        pass
    else:
        source_filter = "internal"
        base_qs = base_qs.filter(source_sender="")

    qs = base_qs
    if status_filter in dict(SupportTicket.Status.choices):
        qs = qs.filter(status=status_filter)

    all_tickets = company.support_tickets
    counts = {
        "total": base_qs.count(),
        "closed": base_qs.filter(status=SupportTicket.Status.CLOSED).count(),
        "pending": base_qs.filter(
            status__in=[
                SupportTicket.Status.OPEN,
                SupportTicket.Status.IN_PROGRESS,
            ]
        ).count(),
        "under_observation": base_qs.filter(
            status=SupportTicket.Status.RESOLVED
        ).count(),
    }

    assignees = company_users(company)

    return render(
        request,
        "business/support_ticket_list.html",
        {
            "company": company,
            "object_list": qs,
            "status_filter": status_filter,
            "source_filter": source_filter,
            "status_choices": SupportTicket.Status.choices,
            "counts": counts,
            "internal_count": all_tickets.filter(source_sender="").count(),
            "external_count": all_tickets.exclude(source_sender="").count(),
            "assignees": assignees,
        },
    )


@company_owner_required
@require_http_methods(["POST"])
def support_ticket_update(request, pk):
    company = _company(request)
    ticket = get_object_or_404(SupportTicket, pk=pk, company=company)
    assignees = company_users(company)
    updates = []
    status = request.POST.get("status", "").strip()
    if status and status in dict(SupportTicket.Status.choices):
        ticket.status = status
        updates.append("status")

    assignee_raw = request.POST.get("assignee", "").strip()
    if assignee_raw:
        assignee = assignees.filter(pk=assignee_raw).first()
        ticket.assignee = assignee
    elif "assignee" in request.POST:
        ticket.assignee = None
    if "assignee" in request.POST:
        updates.append("assignee")

    if "remarks" in request.POST:
        ticket.remarks = request.POST.get("remarks", "").strip()
        updates.append("remarks")

    if "status" in updates:
        if ticket.status in (
            SupportTicket.Status.RESOLVED,
            SupportTicket.Status.CLOSED,
        ):
            ticket.completed_at = ticket.completed_at or timezone.now()
        elif ticket.status in (SupportTicket.Status.OPEN, SupportTicket.Status.IN_PROGRESS):
            ticket.completed_at = None
        updates.append("completed_at")

    if updates:
        ticket.save(update_fields=list(set(updates + ["updated_at"])))
    qs = request.POST.get("next_qs", "").strip()
    url = reverse("support_ticket_list")
    if qs:
        url = f"{url}?{qs}"
    return redirect(url)


@company_owner_required
def support_ticket_add(request):
    company = _company(request)
    if request.method == "POST":
        form = SupportTicketForm(request.POST)
        if form.is_valid():
            t = form.save(commit=False)
            t.company = company
            t.created_by = request.user
            t.save()
            messages.success(
                request,
                f"Support ticket {t.reference} was submitted.",
            )
            return redirect("support_ticket_detail", pk=t.pk)
    else:
        form = SupportTicketForm()
    return render(
        request,
        "business/support_ticket_form.html",
        {"company": company, "form": form},
    )


@company_owner_required
@require_http_methods(["GET", "POST"])
def support_ticket_detail(request, pk):
    company = _company(request)
    ticket = get_object_or_404(
        SupportTicket.objects.select_related("created_by", "assignee"),
        pk=pk,
        company=company,
    )
    comments_qs = ticket.comments.select_related("user")
    comment_form = SupportTicketCommentForm()
    if request.method == "POST":
        action = request.POST.get("action", "").strip()
        if action == "comment":
            comment_form = SupportTicketCommentForm(request.POST)
            if comment_form.is_valid():
                c = comment_form.save(commit=False)
                c.ticket = ticket
                c.user = request.user
                c.save()
                messages.success(request, "Comment added.")
                return redirect("support_ticket_detail", pk=ticket.pk)
        elif action == "close" and ticket.status != SupportTicket.Status.CLOSED:
            ticket.status = SupportTicket.Status.CLOSED
            ticket.completed_at = timezone.now()
            ticket.save(update_fields=["status", "completed_at", "updated_at"])
            messages.success(request, "Ticket marked as closed.")
        elif action == "reopen" and ticket.status == SupportTicket.Status.CLOSED:
            ticket.status = SupportTicket.Status.OPEN
            ticket.completed_at = None
            ticket.save(update_fields=["status", "completed_at", "updated_at"])
            messages.success(request, "Ticket reopened.")
        return redirect("support_ticket_detail", pk=ticket.pk)
    return render(
        request,
        "business/support_ticket_detail.html",
        {
            "company": company,
            "ticket": ticket,
            "comments": comments_qs,
            "comment_form": comment_form,
        },
    )


@require_http_methods(["GET", "POST"])
def caselock_public_form(request, invite_id):
    invite = get_object_or_404(
        CaseLockInvite.objects.select_related("inbox", "inbox__product", "inbox__company"),
        pk=invite_id,
    )
    if CaseLockSubmission.objects.filter(invite=invite).exists():
        return render(
            request,
            "business/caselock_public_done_guest.html",
            {"invite": invite},
        )
    if request.method == "POST":
        form = CaseLockPublicForm(request.POST)
        if form.is_valid():
            sub = form.save(commit=False)
            sub.invite = invite
            sub.save()
            return render(
                request,
                "business/caselock_public_thanks_guest.html",
                {"invite": invite},
            )
    else:
        form = CaseLockPublicForm(
            initial={
                "product_name": invite.inbox.product.name,
                "product_quantity": 1,
            }
        )
    return render(
        request,
        "business/caselock_public_form_guest.html",
        {"form": form, "invite": invite},
    )


@company_owner_required
def caselock_inbox_list(request):
    company = _company(request)
    qs = (
        company.caselock_inboxes.select_related("product")
        .annotate(submission_count=Count("invites__submission", distinct=True))
        .order_by("product__name")
    )
    return render(
        request,
        "business/caselock_inbox_list.html",
        {"company": company, "object_list": qs},
    )


@company_owner_required
def caselock_inbox_add(request):
    company = _company(request)
    if request.method == "POST":
        form = CaseLockInboxForm(request.POST, company=company)
        if form.is_valid():
            obj = form.save(commit=False)
            obj.company = company
            obj.save()
            messages.success(
                request,
                "Case lock mailbox saved. Ensure Celery beat is running to poll inboxes.",
            )
            return redirect("caselock_inbox_list")
    else:
        form = CaseLockInboxForm(company=company)
    return render(
        request,
        "business/caselock_inbox_form.html",
        {
            "company": company,
            "form": form,
            "title": "Add Case lock mailbox",
        },
    )


@company_owner_required
def caselock_inbox_edit(request, pk):
    company = _company(request)
    obj = get_object_or_404(CaseLockInbox, pk=pk, company=company)
    if request.method == "POST":
        form = CaseLockInboxForm(request.POST, instance=obj, company=company)
        if form.is_valid():
            form.save()
            messages.success(request, "Case lock mailbox updated.")
            return redirect("caselock_inbox_list")
    else:
        form = CaseLockInboxForm(instance=obj, company=company)
    return render(
        request,
        "business/caselock_inbox_form.html",
        {
            "company": company,
            "form": form,
            "title": "Edit Case lock mailbox",
            "object": obj,
        },
    )


@company_owner_required
def caselock_submissions(request):
    company = _company(request)
    qs = (
        CaseLockSubmission.objects.filter(invite__inbox__company=company)
        .select_related("invite", "invite__inbox", "invite__inbox__product")
        .order_by("-timestamp")
    )
    inbox_pk = request.GET.get("inbox", "").strip()
    selected_inbox = None
    if inbox_pk.isdigit():
        selected_inbox = company.caselock_inboxes.filter(pk=int(inbox_pk)).first()
        if selected_inbox:
            qs = qs.filter(invite__inbox=selected_inbox)
        else:
            messages.warning(request, "Unknown mailbox; showing all submissions.")
    return render(
        request,
        "business/caselock_submissions.html",
        {
            "company": company,
            "object_list": qs,
            "selected_inbox": selected_inbox,
        },
    )


@company_owner_required
@require_http_methods(["POST"])
def caselock_submission_status_update(request, pk):
    company = _company(request)
    sub = get_object_or_404(
        CaseLockSubmission.objects.select_related("invite__inbox"),
        pk=pk,
        invite__inbox__company=company,
    )
    action = (request.POST.get("action") or "").strip().lower()
    if action == "approve":
        sub.status = CaseLockSubmission.Status.APPROVED
        sub.save(update_fields=["status"])
        if send_caselock_decision_email(sub):
            messages.success(request, "Case lock approved and email sent.")
        else:
            messages.warning(request, "Case lock approved, but notification email failed.")
    elif action == "decline":
        sub.status = CaseLockSubmission.Status.DECLINED
        sub.save(update_fields=["status"])
        if send_caselock_decision_email(sub):
            messages.success(request, "Case lock declined and email sent.")
        else:
            messages.warning(request, "Case lock declined, but notification email failed.")
    else:
        messages.error(request, "Invalid action.")

    inbox_pk = (request.POST.get("inbox") or "").strip()
    url = reverse("caselock_submissions")
    if inbox_pk.isdigit():
        url = f"{url}?inbox={inbox_pk}"
    return redirect(url)
