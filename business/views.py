import csv
from datetime import timedelta
from io import StringIO

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.db.models import Count, Prefetch, Q, Sum
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.formats import date_format
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
    RenewalConfirmLineFormSet,
    RenewalNewProductFormSet,
    SubscriptionRenewForm,
    ProductForm,
    SupportSLASettingsForm,
    SupportTicketCommentForm,
    SupportTicketForm,
    TicketPriorityRuleFormSet,
    VendorForm,
)
from . import helpdesk_bridge
from .models import (
    CaseLockInbox,
    CaseLockInvite,
    CaseLockSubmission,
    Client,
    ClientSubscription,
    Product,
    SubscriptionRenewalInvite,
    SubscriptionRenewalInviteLine,
    SubscriptionRenewalInviteNewProduct,
    TicketPriorityRule,
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


def _subscription_product_options(company, asset_type_prefilter=""):
    products = company.products.all().order_by("name", "pk")
    asset_type_prefilter = (asset_type_prefilter or "").strip().lower()
    if asset_type_prefilter:
        products = [
            p
            for p in products
            if (p.asset_type or "").strip().lower() == asset_type_prefilter
            or (p.category or "").strip().lower() == asset_type_prefilter
        ]
    return [
        {
            "id": p.pk,
            "name": p.name,
            "category": (p.category or "").strip(),
            "asset_type": (p.asset_type or "").strip(),
            "company_name": (p.company_name or "").strip(),
        }
        for p in products
    ]


def _delete_success_message(
    request,
    entity_count: int,
    sub_count: int,
    entity_label: str,
    *,
    product_count: int = 0,
) -> None:
    if entity_count == 0:
        messages.warning(request, f"No {entity_label} were deleted.")
        return
    msg = f"Deleted {entity_count} {entity_label}."
    if product_count:
        msg += f" Removed {product_count} linked product(s)."
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


def _delete_vendors(company, qs) -> tuple[int, int, int]:
    product_qs = company.products.filter(vendor__in=qs)
    product_count = product_qs.count()
    sub_count = company.subscriptions.filter(
        Q(vendor__in=qs) | Q(product__in=product_qs)
    ).count()
    if product_count:
        product_qs.delete()
    entity_count = qs.count()
    if entity_count:
        qs.delete()
    return entity_count, sub_count, product_count


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


def _renewal_invite_line_prefetch():
    return Prefetch(
        "lines",
        queryset=SubscriptionRenewalInviteLine.objects.select_related(
            "subscription",
            "subscription__client",
            "subscription__product",
        ),
    )


def _renewal_invite_queryset(company):
    return (
        SubscriptionRenewalInvite.objects.filter(
            company=company,
            email_sent_at__isnull=False,
        )
        .annotate(line_count=Count("lines"))
        .prefetch_related(
            _renewal_invite_line_prefetch(),
            "recipients",
            "new_products",
        )
        .order_by("-email_sent_at", "-created_at")
    )


def _invite_deal_payload(invite):
    state = invite.deal_table_state()
    return {
        "deal_pending": state["pending"],
        "deal_badges": state["badges"],
        "deal_status_url": reverse(
            "client_renewal_set_deal_status", kwargs={"pk": invite.pk}
        ),
    }


def _format_dt(value):
    if not value:
        return ""
    return date_format(timezone.localtime(value), "d M Y, H:i:s")


def _invite_detail_url(invite):
    if not invite.submitted_at:
        return ""
    return reverse("client_renewal_detail", kwargs={"pk": invite.pk})


def _new_subscription_rows(invites):
    """Rows for clients who added catalog products via the renewal Add button."""
    rows = []
    for invite in invites:
        if not invite.submitted_at:
            continue
        emails = invite.sent_to_emails or [invite.client_email]
        detail_url = _invite_detail_url(invite)
        for item in invite.new_products.all():
            rows.append(
                {
                    "id": f"{invite.pk}-{item.pk}",
                    "client_email": invite.client_email,
                    "recipient_emails": emails,
                    "company_name": item.company_name or "",
                    "product_name": item.product_name,
                    "quantity": item.quantity,
                    "submitted_at": invite.submitted_at,
                    "submitted_by_email": invite.submitted_by_email or "",
                    "detail_url": detail_url,
                }
            )
    return rows


def _existing_expiry_rows(invites):
    """Rows for existing expiry products with previous vs submitted quantity."""
    rows = []
    for invite in invites:
        if not invite.submitted_at:
            continue
        emails = invite.sent_to_emails or [invite.client_email]
        detail_url = _invite_detail_url(invite)
        for line in invite.lines.all():
            rows.append(
                {
                    "id": f"{invite.pk}-{line.pk}",
                    "client_email": invite.client_email,
                    "recipient_emails": emails,
                    "company_name": line.company_name or "",
                    "product_name": line.product_name,
                    "previous_quantity": line.current_quantity,
                    "new_quantity": line.renewal_count,
                    "submitted_at": invite.submitted_at,
                    "submitted_by_email": invite.submitted_by_email or "",
                    "detail_url": detail_url,
                }
            )
    return rows


def _jsonify_tab_rows(rows):
    formatted = []
    for row in rows:
        item = dict(row)
        item["submitted_at"] = _format_dt(row.get("submitted_at"))
        formatted.append(item)
    return formatted


def _invite_status_row(invite):
    row = {
        "id": str(invite.pk),
        "client_email": invite.client_email,
        "recipient_emails": invite.sent_to_emails,
        "email_sent_at": _format_dt(invite.email_sent_at),
        "read_at": _format_dt(invite.read_at),
        "read_source": invite.read_source or "",
        "submitted_at": _format_dt(invite.submitted_at),
        "submitted_by_email": invite.submitted_by_email or "",
        "quotation_sent_at": _format_dt(invite.quotation_sent_at),
        "line_count": invite.line_count,
        "detail_url": _invite_detail_url(invite),
    }
    row.update(_invite_deal_payload(invite))
    return row


@sales_module_required
def client_renewal_list(request):
    company = _company(request)
    from business.renewal_read_tracking import poll_outlook_read_receipts_for_company

    _, poll_logs = poll_outlook_read_receipts_for_company(company)
    for message in poll_logs:
        if "failed" in message.lower():
            messages.warning(request, message)
    invites = list(_renewal_invite_queryset(company))
    return render(
        request,
        "business/client_renewal_list.html",
        {
            "company": company,
            "object_list": invites,
            "new_subscription_rows": _new_subscription_rows(invites),
            "existing_expiry_rows": _existing_expiry_rows(invites),
        },
    )


@sales_module_required
def client_renewal_live(request):
    """Poll Outlook and return current renewal invite statuses as JSON."""
    company = _company(request)
    from business.renewal_read_tracking import poll_outlook_read_receipts_for_company

    updated, poll_logs = poll_outlook_read_receipts_for_company(company)
    invites = list(_renewal_invite_queryset(company))
    return JsonResponse(
        {
            "ok": True,
            "updated": updated,
            "polled_at": _format_dt(timezone.now()),
            "logs": poll_logs,
            "rows": [_invite_status_row(invite) for invite in invites],
            "new_subscription_rows": _jsonify_tab_rows(
                _new_subscription_rows(invites)
            ),
            "existing_expiry_rows": _jsonify_tab_rows(
                _existing_expiry_rows(invites)
            ),
        }
    )


@sales_module_required
@require_http_methods(["GET", "POST"])
def client_renewal_set_deal_status(request, pk):
    company = _company(request)
    invite = get_object_or_404(
        SubscriptionRenewalInvite.objects.prefetch_related(
            _renewal_invite_line_prefetch()
        ),
        pk=pk,
        company=company,
        email_sent_at__isnull=False,
    )
    next_url = reverse("client_renewal_list")

    if request.method == "GET":
        return JsonResponse(
            {
                "ok": True,
                "items": invite.deal_line_items(next_url),
                **_invite_deal_payload(invite),
            }
        )

    valid = {
        ClientSubscription.DealStatus.WIN,
        ClientSubscription.DealStatus.LOST,
        ClientSubscription.DealStatus.CANCEL,
    }
    status = (request.POST.get("deal_status") or "").strip().lower()
    if status not in valid:
        return JsonResponse(
            {"ok": False, "error": "Choose Win, Lost, or Cancel."},
            status=400,
        )
    try:
        subscription_id = int(request.POST.get("subscription_id") or "")
    except (TypeError, ValueError):
        return JsonResponse(
            {"ok": False, "error": "Choose a company / product."},
            status=400,
        )

    items_before = invite.deal_line_items(next_url)
    target = next(
        (item for item in items_before if item["subscription_id"] == subscription_id),
        None,
    )
    if target is None:
        return JsonResponse(
            {"ok": False, "error": "Subscription is not part of this renewal email."},
            status=400,
        )

    try:
        invite.apply_line_deal_status(subscription_id, status)
    except ValueError as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=400)

    update_now = (request.POST.get("update_now") or "").strip() in {
        "1",
        "true",
        "yes",
    }
    renew_url = ""
    if status == ClientSubscription.DealStatus.WIN and update_now:
        renew_url = target.get("renew_url") or ""

    invite = SubscriptionRenewalInvite.objects.prefetch_related(
        _renewal_invite_line_prefetch()
    ).get(pk=invite.pk)
    return JsonResponse(
        {
            "ok": True,
            "deal_status": status,
            "subscription_id": subscription_id,
            "items": invite.deal_line_items(next_url),
            "renew_url": renew_url,
            **_invite_deal_payload(invite),
        }
    )


@sales_module_required
def client_renewal_detail(request, pk):
    company = _company(request)
    invite = get_object_or_404(
        SubscriptionRenewalInvite.objects.prefetch_related(
            "lines",
            "lines__subscription",
            "new_products",
            "recipients",
        ),
        pk=pk,
        company=company,
        submitted_at__isnull=False,
    )
    from business.zoho_crm import zoho_configured

    return render(
        request,
        "business/client_renewal_detail.html",
        {
            "company": company,
            "invite": invite,
            "lines": invite.lines.order_by("id"),
            "new_products": invite.new_products.order_by("id"),
            "zoho_configured": zoho_configured(),
            "zoho_connected": company.zoho_connected,
            "quoteable_count": invite.lines.filter(renewal_count__gt=0).count(),
        },
    )


@sales_module_required
@require_http_methods(["POST"])
def client_renewal_send_quotation(request, pk):
    company = _company(request)
    invite = get_object_or_404(
        SubscriptionRenewalInvite.objects.prefetch_related(
            "lines",
            "lines__subscription",
        ),
        pk=pk,
        company=company,
        submitted_at__isnull=False,
    )
    from business.zoho_crm import create_quote_from_renewal_invite, zoho_configured

    if not zoho_configured():
        messages.error(
            request,
            "Zoho app credentials are not configured on the server (.env).",
        )
        return redirect("client_renewal_detail", pk=invite.pk)
    if not company.zoho_connected:
        messages.error(
            request,
            "Connect Zoho CRM in Company profile before sending a quotation.",
        )
        return redirect("client_renewal_detail", pk=invite.pk)

    try:
        quote_id, quote_number, quote_url = create_quote_from_renewal_invite(
            company, invite
        )
        invite.zoho_quote_id = quote_id
        invite.zoho_quote_number = quote_number
        invite.zoho_quote_url = quote_url
        invite.quotation_sent_at = timezone.now()
        invite.quotation_error = ""
        invite.save(
            update_fields=[
                "zoho_quote_id",
                "zoho_quote_number",
                "zoho_quote_url",
                "quotation_sent_at",
                "quotation_error",
            ]
        )
        messages.success(
            request,
            f"Zoho quotation created ({quote_number}). Open it in Zoho to review and send.",
        )
    except Exception as exc:
        invite.quotation_error = str(exc)[:2000]
        invite.save(update_fields=["quotation_error"])
        messages.error(request, f"Could not create Zoho quotation: {exc}")
    return redirect("client_renewal_detail", pk=invite.pk)


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
    n, sub_count, product_count = _delete_vendors(company, qs)
    _delete_success_message(
        request, n, sub_count, "vendor(s)", product_count=product_count
    )
    return redirect("vendor_list")


@sales_module_required
@require_http_methods(["POST"])
def vendor_delete(request, pk):
    company = _company(request)
    obj = get_object_or_404(Vendor, pk=pk, company=company)
    n, sub_count, product_count = _delete_vendors(
        company, company.vendors.filter(pk=obj.pk)
    )
    _delete_success_message(
        request, n, sub_count, "vendor", product_count=product_count
    )
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
        request,
        Client,
        "business/client_list.html",
        (
            "name",
            "email",
            "email_2",
            "email_3",
            "phone",
            "phone_2",
            "phone_3",
            "gst_number",
        ),
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
        form = ClientForm(request.POST, company=company)
        if form.is_valid():
            c = form.save(commit=False)
            c.company = company
            c.save()
            return redirect("client_list")
    else:
        form = ClientForm(company=company)
    return render(
        request,
        "business/client_form.html",
        {
            "company": company,
            "form": form,
            "title": "Add Client",
            "subtitle": "Create client for client subscription mapping",
            "reminder_presets": {},
        },
    )


@sales_module_required
@require_http_methods(["POST"])
def client_toggle_renewal_emails(request, pk):
    company = _company(request)
    obj = get_object_or_404(Client, pk=pk, company=company)
    raw = (request.POST.get("enabled") or "").strip().lower()
    enabled = raw in {"1", "true", "on", "yes"}
    obj.renewal_emails_enabled = enabled
    obj.save(update_fields=["renewal_emails_enabled"])
    return JsonResponse(
        {
            "ok": True,
            "enabled": obj.renewal_emails_enabled,
            "label": "On" if obj.renewal_emails_enabled else "Off",
        }
    )


@sales_module_required
def client_edit(request, pk):
    company = _company(request)
    obj = get_object_or_404(Client, pk=pk, company=company)
    if request.method == "POST":
        form = ClientForm(request.POST, instance=obj, company=company)
        if form.is_valid():
            form.save()
            return redirect("client_list")
    else:
        form = ClientForm(instance=obj, company=company)
    return render(
        request,
        "business/client_form.html",
        {
            "company": company,
            "form": form,
            "title": "Edit Client",
            "subtitle": "Update client for client subscription mapping",
            "object": obj,
            "reminder_presets": {},
        },
    )


@sales_module_required
@require_http_methods(["POST"])
def client_bulk_delete(request):
    company = _company(request)
    ids = request.POST.getlist("client_ids")
    if not ids:
        messages.warning(request, "Select at least one client to delete.")
        return redirect("client_list")
    qs = company.clients.filter(pk__in=ids)
    n, sub_count = _delete_clients(company, qs)
    _delete_success_message(request, n, sub_count, "client(s)")
    return redirect("client_list")


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
            "product_options": _subscription_product_options(company, asset_type),
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
            new_qty = form.cleaned_data["quantity"]
            new_price = form.cleaned_data["billing_amount"]
            obj.start_date = new_start
            obj.end_date = new_end
            obj.quantity = new_qty
            obj.billing_amount = new_price
            obj.status = ClientSubscription.Status.ACTIVE
            obj.save(
                update_fields=[
                    "start_date",
                    "end_date",
                    "quantity",
                    "billing_amount",
                    "status",
                ]
            )
            end_label = new_end.strftime("%d %b %Y") if new_end else "no end date"
            messages.success(
                request,
                f"Renewed {obj.client.name} — {obj.product.name}: "
                f"{new_start.strftime('%d %b %Y')} to {end_label} "
                f"(qty {new_qty}, price {new_price}).",
            )
            return redirect(next_url)
    else:
        form = SubscriptionRenewForm(
            initial={
                "start_date": suggested_start,
                "end_date": suggested_end,
                "quantity": obj.quantity,
                "billing_amount": obj.billing_amount,
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
            "product_options": _subscription_product_options(company),
        },
    )


def _support_ticket_list_data(company, *, status_filter: str = ""):
    """Shared ticket list payload for HTML page and live JSON poll."""
    from business.sla import get_or_create_sla_settings

    status_filter = (status_filter or "").strip()
    sla_settings = get_or_create_sla_settings(company)

    all_wrapped = helpdesk_bridge.wrap_tickets(
        helpdesk_bridge.tickets_for_company(company),
        sla_settings=sla_settings,
    )
    base = list(all_wrapped)

    if status_filter in dict(helpdesk_bridge.STATUS_CHOICES):
        qs = [t for t in base if t.status == status_filter]
    else:
        status_filter = ""
        qs = base

    counts = {
        "total": len(base),
        "closed": sum(1 for t in base if t.status == "closed"),
        "pending": sum(1 for t in base if t.status in ("open", "in_progress")),
        "under_observation": sum(1 for t in base if t.status == "resolved"),
        "sla_breached": sum(1 for t in base if t.sla and t.sla.get("any_breached")),
    }
    return {
        "object_list": qs,
        "status_filter": status_filter,
        "counts": counts,
        "assignees": company_users(company),
        "sla_settings": sla_settings,
    }


def _format_ticket_dt(value, fmt: str) -> str:
    if not value:
        return ""
    return date_format(timezone.localtime(value), fmt)


@company_owner_required
def support_ticket_list(request):
    company = _company(request)
    helpdesk_bridge.sync_company_queue(company)

    # runserver-only: refresh page → poll mailbox → new tickets appear
    if request.method == "GET":
        from business.helpdesk_inbox import inbox_error_logs, maybe_fetch_on_dashboard

        result = maybe_fetch_on_dashboard(company)
        if result and int(result.get("created") or 0) > 0:
            messages.success(
                request,
                f"Imported {result['created']} new support ticket(s) from email.",
            )
        for line in inbox_error_logs(result):
            messages.error(request, line)

    data = _support_ticket_list_data(
        company,
        status_filter=request.GET.get("status", ""),
    )

    return render(
        request,
        "business/support_ticket_list.html",
        {
            "company": company,
            "object_list": data["object_list"],
            "status_filter": data["status_filter"],
            "status_choices": helpdesk_bridge.STATUS_CHOICES,
            "counts": data["counts"],
            "assignees": data["assignees"],
            "sla_settings": data["sla_settings"],
        },
    )


@company_owner_required
def support_ticket_live(request):
    """Poll mailbox (throttled) and return current ticket list as JSON."""
    company = _company(request)
    helpdesk_bridge.sync_company_queue(company)

    from business.helpdesk_inbox import inbox_error_logs, maybe_fetch_on_dashboard

    fetch_result = maybe_fetch_on_dashboard(company) or {}
    created = int(fetch_result.get("created") or 0)
    inbox_errors = inbox_error_logs(fetch_result)

    data = _support_ticket_list_data(
        company,
        status_filter=request.GET.get("status", ""),
    )
    next_qs = request.GET.urlencode()
    status_choices = [
        {"value": value, "label": label}
        for value, label in helpdesk_bridge.STATUS_CHOICES
    ]
    assignees = [
        {"id": u.pk, "email": u.email or ""}
        for u in data["assignees"]
    ]

    rows = []
    for t in data["object_list"]:
        email = t.source_sender or getattr(t.created_by, "email", "") or ""
        done = t.status in ("resolved", "closed")
        sla = t.sla or {}
        rows.append(
            {
                "id": t.pk,
                "reference": t.reference,
                "created_at": _format_ticket_dt(t.created_at, "d/m/Y H:i"),
                "subject": t.subject or "",
                "email": email,
                "department": t.get_department_display(),
                "priority": t.priority,
                "priority_label": t.get_priority_display(),
                "status": t.status,
                "assignee_id": t.assignee_id or "",
                "remarks": t.remarks or "",
                "completed_at": _format_ticket_dt(t.updated_at, "d/m/Y H:i") if done else "-",
                "resolve_due": _format_ticket_dt(sla.get("resolve_due"), "d/m/Y H:i") or "—",
                "response_due": _format_ticket_dt(sla.get("response_due"), "d/m/Y H:i") or "—",
                "resolve_label": sla.get("resolve_label") or "—",
                "response_label": sla.get("response_label") or "—",
                "resolve_status": sla.get("resolve_status") or "off",
                "response_status": sla.get("response_status") or "off",
                "tracks_resolve": bool(sla.get("tracks_resolve")),
                "tracks_response": bool(sla.get("tracks_response")),
                "detail_url": reverse("support_ticket_detail", kwargs={"pk": t.pk}),
                "update_url": reverse("support_ticket_update", kwargs={"pk": t.pk}),
                "is_closed": t.status == "closed",
            }
        )

    return JsonResponse(
        {
            "ok": True,
            "created": created,
            "inbox_errors": inbox_errors,
            "polled_at": _format_dt(timezone.now()),
            "status_filter": data["status_filter"],
            "next_qs": next_qs,
            "counts": data["counts"],
            "status_choices": status_choices,
            "assignees": assignees,
            "sla_mode": data["sla_settings"].sla_mode,
            "rows": rows,
        }
    )


@company_owner_required
@require_http_methods(["POST"])
def support_ticket_update(request, pk):
    company = _company(request)
    try:
        ticket = helpdesk_bridge.get_company_ticket(company, pk)
    except helpdesk_bridge.Ticket.DoesNotExist:
        from django.http import Http404

        raise Http404 from None

    assignees = company_users(company)
    status = request.POST.get("status", "").strip()
    status_changed = False
    if status and status in dict(helpdesk_bridge.STATUS_CHOICES):
        status_changed = helpdesk_bridge.set_ticket_status(
            ticket, status, user=request.user
        )
        ticket.refresh_from_db()

    if "assignee" in request.POST:
        assignee_raw = request.POST.get("assignee", "").strip()
        assignee = assignees.filter(pk=assignee_raw).first() if assignee_raw else None
        changed = helpdesk_bridge.set_ticket_assignee(
            ticket, assignee, user=request.user
        )
        if changed:
            from business.helpdesk_assign_mail import queue_assignment_emails

            ticket.refresh_from_db()
            if assignee is None:
                messages.success(request, "Ticket unassigned.")
            else:
                queue_assignment_emails(
                    company=company,
                    ticket=ticket,
                    assignee=assignee,
                    assigned_by=request.user,
                )
                messages.success(
                    request,
                    f"Ticket assigned to {assignee.email}. "
                    "Notification email is being sent.",
                )

    if status_changed and status == "closed":
        from business.helpdesk_assign_mail import queue_ticket_closed_email

        ticket.refresh_from_db()
        queue_ticket_closed_email(company=company, ticket=ticket)
        messages.success(
            request, "Ticket closed. Requester notification email is being sent."
        )

    if "remarks" in request.POST:
        helpdesk_bridge.set_ticket_remarks(
            ticket, request.POST.get("remarks", "").strip()
        )

    qs = request.POST.get("next_qs", "").strip()
    url = reverse("support_ticket_list")
    if qs:
        url = f"{url}?{qs}"
    return redirect(url)


@company_owner_required
def support_ticket_add(request):
    import json

    from business.priority_rules import rules_payload_for_ui

    company = _company(request)
    helpdesk_bridge.sync_company_queue(company)
    priority_rules = rules_payload_for_ui(company)
    if request.method == "POST":
        form = SupportTicketForm(request.POST)
        if form.is_valid():
            t = helpdesk_bridge.create_internal_ticket(
                company=company,
                user=request.user,
                subject=form.cleaned_data["subject"],
                description=form.cleaned_data["description"],
                department=form.cleaned_data["department"],
                priority=form.cleaned_data["priority"],
                priority_source="manual",
            )
            messages.success(
                request,
                f"Support ticket TK-{t.pk:05d} was submitted.",
            )
            return redirect("support_ticket_detail", pk=t.pk)
    else:
        form = SupportTicketForm()
    return render(
        request,
        "business/support_ticket_form.html",
        {
            "company": company,
            "form": form,
            "priority_rules_json": json.dumps(priority_rules),
        },
    )


@company_owner_required
@require_http_methods(["GET", "POST"])
def support_ticket_detail(request, pk):
    from business.sla import get_or_create_sla_settings

    company = _company(request)
    try:
        raw = helpdesk_bridge.get_company_ticket(company, pk)
    except helpdesk_bridge.Ticket.DoesNotExist:
        from django.http import Http404

        raise Http404 from None

    sla_settings = get_or_create_sla_settings(company)
    ticket = helpdesk_bridge.wrap_ticket(raw, sla_settings=sla_settings)
    comment_form = SupportTicketCommentForm()
    if request.method == "POST":
        action = request.POST.get("action", "").strip()
        if action == "comment":
            comment_form = SupportTicketCommentForm(request.POST)
            if comment_form.is_valid():
                helpdesk_bridge.add_comment(
                    ticket=raw,
                    user=request.user,
                    body=comment_form.cleaned_data["body"],
                    internal_note=comment_form.cleaned_data.get("internal_note", False),
                )
                messages.success(request, "Comment added.")
                return redirect("support_ticket_detail", pk=raw.pk)
        elif action == "priority":
            new_priority = request.POST.get("priority", "").strip()
            if new_priority in helpdesk_bridge.PRIORITY_UI_TO_HD:
                changed = helpdesk_bridge.set_ticket_priority(
                    company,
                    raw,
                    new_priority,
                    user=request.user,
                    priority_source="manual",
                )
                if changed:
                    messages.success(
                        request,
                        "Priority updated; SLA due times recalculated.",
                    )
                else:
                    messages.info(request, "Priority unchanged.")
            return redirect("support_ticket_detail", pk=raw.pk)
        elif action == "close" and ticket.status != "closed":
            changed = helpdesk_bridge.set_ticket_status(
                raw, "closed", user=request.user
            )
            if changed:
                from business.helpdesk_assign_mail import queue_ticket_closed_email

                raw.refresh_from_db()
                queue_ticket_closed_email(company=company, ticket=raw)
                messages.success(
                    request,
                    "Ticket marked as closed. Requester notification email is being sent.",
                )
            else:
                messages.success(request, "Ticket marked as closed.")
        elif action == "reopen" and ticket.status == "closed":
            helpdesk_bridge.set_ticket_status(raw, "open", user=request.user)
            messages.success(request, "Ticket reopened.")
        return redirect("support_ticket_detail", pk=raw.pk)
    return render(
        request,
        "business/support_ticket_detail.html",
        {
            "company": company,
            "ticket": ticket,
            "comments": helpdesk_bridge.followups_as_comments(raw),
            "comment_form": comment_form,
            "priority_choices": helpdesk_bridge.PRIORITY_LABELS,
            "sla_settings": sla_settings,
        },
    )


@company_owner_required
@require_http_methods(["GET", "POST"])
def support_sla_settings(request):
    from business.priority_rules import ensure_default_priority_rules
    from business.sla import get_or_create_sla_settings

    company = _company(request)
    helpdesk_bridge.sync_company_queue(company)
    settings_obj = get_or_create_sla_settings(company)
    ensure_default_priority_rules(company)

    rules_qs = list(
        TicketPriorityRule.objects.filter(company=company).order_by("sort_order", "id")
    )
    initial_rules = [
        {
            "priority": r.priority,
            "keywords": r.keywords,
            "sort_order": r.sort_order,
        }
        for r in rules_qs
        if r.is_active
    ]

    if request.method == "POST":
        form = SupportSLASettingsForm(request.POST, instance=settings_obj)
        formset = TicketPriorityRuleFormSet(request.POST, prefix="rules")
        if form.is_valid() and formset.is_valid():
            form.save()
            TicketPriorityRule.objects.filter(company=company).delete()
            for rule_form in formset:
                if not hasattr(rule_form, "cleaned_data") or not rule_form.cleaned_data:
                    continue
                if rule_form.cleaned_data.get("DELETE"):
                    continue
                keywords = (rule_form.cleaned_data.get("keywords") or "").strip()
                priority = rule_form.cleaned_data.get("priority")
                if not keywords or not priority:
                    continue
                TicketPriorityRule.objects.create(
                    company=company,
                    priority=priority,
                    keywords=keywords,
                    sort_order=rule_form.cleaned_data.get("sort_order") or 0,
                    is_active=True,
                )
            # Refresh dues on open tickets so new hours/mode apply immediately.
            from business.sla import apply_sla_to_ticket

            open_statuses = (
                helpdesk_bridge.Ticket.OPEN_STATUS,
                helpdesk_bridge.Ticket.REOPENED_STATUS,
            )
            for ticket in helpdesk_bridge.tickets_for_company(company).filter(
                status__in=open_statuses
            ):
                apply_sla_to_ticket(company, ticket)
            messages.success(request, "SLA and priority settings saved.")
            return redirect("support_sla_settings")
    else:
        form = SupportSLASettingsForm(instance=settings_obj)
        formset = TicketPriorityRuleFormSet(initial=initial_rules, prefix="rules")

    return render(
        request,
        "business/support_sla_settings.html",
        {
            "company": company,
            "form": form,
            "formset": formset,
            "settings_obj": settings_obj,
        },
    )


@require_http_methods(["GET"])
def renewal_track_open(request, invite_id):
    invite = get_object_or_404(SubscriptionRenewalInvite, pk=invite_id)
    from business.renewal_read_tracking import (
        READ_SOURCE_OPEN,
        TRANSPARENT_GIF,
        mark_renewal_invite_read,
    )

    mark_renewal_invite_read(invite, timezone.now(), source=READ_SOURCE_OPEN)
    return HttpResponse(TRANSPARENT_GIF, content_type="image/gif")


def _renewal_invalid_link_page(request, invite=None):
    return render(
        request,
        "business/renewal_confirm_invalid_guest.html",
        {"invite": invite, "public_page": True},
        status=403,
    )


def _renewal_submitter_from_token(invite, token: str) -> str:
    from business.renewal_tokens import unsign_renewal_confirm_token

    recipient_emails = {
        (row.email or "").strip().lower()
        for row in invite.recipients.all()
        if (row.email or "").strip()
    }
    token = (token or "").strip()
    if token:
        parsed = unsign_renewal_confirm_token(token)
        if parsed is None:
            return ""
        invite_id, email = parsed
        if str(invite.pk) != invite_id or not email:
            return ""
        if recipient_emails:
            return email if email in recipient_emails else ""
        return email if invite.is_allowed_submitter(email) else ""
    if recipient_emails:
        return ""
    return (invite.client_email or "").strip().lower()


def _invite_excluded_product_ids(lines):
    ids = set()
    for line in lines:
        sub = getattr(line, "subscription", None)
        if sub is not None and sub.product_id:
            ids.add(sub.product_id)
    return ids


def _available_renewal_products(company, excluded_ids):
    qs = company.products.exclude(pk__in=excluded_ids).order_by("name")
    return [{"id": p.pk, "name": p.name} for p in qs]


def _new_product_formset_kwargs(invite, lines):
    return {
        "prefix": "new_products",
        "company": invite.company,
        "excluded_product_ids": _invite_excluded_product_ids(lines),
    }


def _annotate_new_product_forms(formset, name_by_id):
    for form in formset.forms:
        raw = form["product_id"].value()
        try:
            form.product_display_name = name_by_id.get(int(raw), "Product")
        except (TypeError, ValueError):
            form.product_display_name = "Product"


def _renewal_quantity_page(
    request, invite, lines, formset, new_product_formset=None
):
    excluded_ids = _invite_excluded_product_ids(lines)
    available_products = _available_renewal_products(invite.company, excluded_ids)
    name_by_id = dict(invite.company.products.values_list("pk", "name"))
    if new_product_formset is None:
        new_product_formset = RenewalNewProductFormSet(
            **_new_product_formset_kwargs(invite, lines)
        )
    _annotate_new_product_forms(new_product_formset, name_by_id)
    return render(
        request,
        "business/renewal_confirm_form_guest.html",
        {
            "invite": invite,
            "lines": lines,
            "formset": formset,
            "rows": list(zip(formset.forms, lines)),
            "new_product_formset": new_product_formset,
            "available_products": available_products,
            "public_page": True,
        },
    )


def _save_renewal_confirmation(
    invite, lines, formset, submitter_email, new_product_formset=None
):
    line_map = {line.pk: line for line in lines}
    for form in formset.forms:
        line = line_map.get(form.cleaned_data["line_id"])
        if line is None:
            continue
        line.renewal_count = form.cleaned_data["renewal_count"]
        line.save(update_fields=["renewal_count"])

    invite.new_products.all().delete()
    product_ids = []
    extras = []
    if new_product_formset is not None:
        for form in new_product_formset.forms:
            if not form.cleaned_data:
                continue
            product_ids.append(form.cleaned_data["product_id"])
        products = {
            p.pk: p
            for p in Product.objects.filter(
                company=invite.company, pk__in=product_ids
            )
        }
        company_name = lines[0].company_name if lines else ""
        for form in new_product_formset.forms:
            if not form.cleaned_data:
                continue
            product = products.get(form.cleaned_data["product_id"])
            extras.append(
                SubscriptionRenewalInviteNewProduct(
                    invite=invite,
                    product=product,
                    company_name=company_name,
                    product_name=product.name if product else "",
                    quantity=form.cleaned_data["quantity"],
                )
            )
    if extras:
        SubscriptionRenewalInviteNewProduct.objects.bulk_create(extras)

    invite.submitted_at = timezone.now()
    invite.submitted_by_email = submitter_email
    invite.save(update_fields=["submitted_at", "submitted_by_email"])
    from business.expiry_notifications import send_renewal_confirmation_email

    lines = list(
        invite.lines.select_related("subscription__product").order_by("id")
    )
    new_products = list(invite.new_products.select_related("product").order_by("id"))
    send_renewal_confirmation_email(invite, lines, new_products=new_products)
    return new_products


@require_http_methods(["GET", "POST"])
def renewal_confirm_public(request, invite_id, token=""):
    invite = get_object_or_404(
        SubscriptionRenewalInvite.objects.select_related("company").prefetch_related(
            "lines",
            "lines__subscription__client",
            "lines__subscription__product",
            "new_products",
            "recipients",
        ),
        pk=invite_id,
    )
    lines = list(invite.lines.order_by("id"))
    new_products = list(invite.new_products.order_by("id"))
    submitter_email = _renewal_submitter_from_token(invite, token)
    if not submitter_email:
        return _renewal_invalid_link_page(request, invite)

    invite.mark_recipient_opened(submitter_email)

    if invite.is_submitted:
        return render(
            request,
            "business/renewal_confirm_done_guest.html",
            {
                "invite": invite,
                "lines": lines,
                "new_products": new_products,
                "public_page": True,
            },
        )

    if request.method == "POST":
        formset = RenewalConfirmLineFormSet(request.POST)
        new_product_formset = RenewalNewProductFormSet(
            request.POST, **_new_product_formset_kwargs(invite, lines)
        )
        if formset.is_valid() and new_product_formset.is_valid():
            invite.refresh_from_db()
            if invite.is_submitted:
                return render(
                    request,
                    "business/renewal_confirm_done_guest.html",
                    {
                        "invite": invite,
                        "lines": lines,
                        "new_products": list(invite.new_products.order_by("id")),
                        "public_page": True,
                    },
                )
            saved_new = _save_renewal_confirmation(
                invite,
                lines,
                formset,
                submitter_email,
                new_product_formset=new_product_formset,
            )
            return render(
                request,
                "business/renewal_confirm_thanks_guest.html",
                {
                    "invite": invite,
                    "lines": lines,
                    "new_products": saved_new,
                    "public_page": True,
                },
            )
        return _renewal_quantity_page(
            request,
            invite,
            lines,
            formset,
            new_product_formset=new_product_formset,
        )

    formset = RenewalConfirmLineFormSet(initial=[{"line_id": line.pk} for line in lines])
    return _renewal_quantity_page(request, invite, lines, formset)


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
