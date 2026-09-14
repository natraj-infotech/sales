import csv
from datetime import timedelta
from io import StringIO
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.db.models import Count, Exists, OuterRef, Prefetch, Q, Sum
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
    support_ticket_access_required,
)

from .forms import (
    CaseLockProductForm,
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
from .analytics import sales_dashboard_charts, ticket_charts_from_views
from .models import (
    CaseLockInvite,
    CaseLockProduct,
    CaseLockSubmission,
    annotate_internal_case_locks,
    Client,
    ClientSubscription,
    Product,
    SubscriptionRenewalInvite,
    SubscriptionRenewalInviteLine,
    SubscriptionRenewalInviteNewProduct,
    TicketPriorityRule,
    Vendor,
)
from .caselock_inbox import (
    maybe_fetch_on_dashboard,
    poll_error_logs,
    send_caselock_decision_email,
    should_poll_mailbox_on_request,
)
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


def _delete_subscriptions(company, qs) -> int:
    qs = qs.filter(company=company)
    entity_count = qs.count()
    if entity_count:
        qs.delete()
    return entity_count


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
    charts = sales_dashboard_charts(company, expiring_qs=expiring_qs, today=today)
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
        "charts": charts,
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
        "deal_form_empty": state.get("form_empty", False),
        "deal_form_empty_count": state.get("form_empty_count", 0),
        "deal_status_url": reverse(
            "client_renewal_set_deal_status", kwargs={"pk": invite.pk}
        ),
    }


def _company_vendor_choices(company):
    return [
        {"id": vendor.pk, "name": vendor.name}
        for vendor in company.vendors.all().order_by("name")
    ]


def _form_first_error(form) -> str:
    if form.non_field_errors():
        return str(form.non_field_errors()[0])
    for errors in form.errors.values():
        if errors:
            return str(errors[0])
    return "Please check the form."


def _deal_status_json(invite, company, next_url, extra=None):
    payload = {
        "ok": True,
        "items": invite.deal_line_items(next_url),
        "vendors": _company_vendor_choices(company),
        **_invite_deal_payload(invite),
    }
    if extra:
        payload.update(extra)
    return JsonResponse(payload)


def _format_dt(value):
    if not value:
        return ""
    return date_format(timezone.localtime(value), "d M Y, H:i:s")




def _invite_is_quoteable(invite):
    if not invite.submitted_at:
        return False
    return any(
        line.renewal_count is not None and int(line.renewal_count) > 0
        for line in invite.lines.all()
    )


def _company_group_key(name):
    label = (name or "").strip()
    return (label.lower() or "\uffff", label or "—")


def _append_invite_event(events, invite):
    events.append(
        {
            "email_sent_at": _format_dt(invite.email_sent_at),
            "read_at": _format_dt(invite.read_at),
            "read_source": invite.read_source or "",
            "submitted_at": _format_dt(invite.submitted_at),
            "submitted_by_email": invite.submitted_by_email or "",
        }
    )


def _timestamps_from_events(events, deal_status, deal_status_at):
    sent = [item["email_sent_at"] for item in events if item.get("email_sent_at")]
    read = next((item for item in events if item.get("read_at")), None)
    response = next((item for item in events if item.get("submitted_at")), None)
    deal = None
    if deal_status:
        deal = {
            "status": deal_status,
            "at": _format_dt(deal_status_at) if deal_status_at else "",
        }
    return {
        "sent": sent,
        "read_at": (read or {}).get("read_at", ""),
        "read_source": (read or {}).get("read_source", ""),
        "submitted_at": (response or {}).get("submitted_at", ""),
        "submitted_by_email": (response or {}).get("submitted_by_email", ""),
        "deal": deal,
    }


def _zoho_payload(invite, *, zoho_connected, zoho_configured, quote_url=""):
    quote_url = (quote_url or "").strip()
    if not quote_url and invite:
        quote_url = (getattr(invite, "zoho_quote_url", "") or "").strip()
    if not zoho_configured or not zoho_connected:
        return {
            "kind": "connect",
            "setup_url": reverse("company_setup"),
            "send_url": "",
            "quote_url": quote_url,
        }
    if invite and _invite_is_quoteable(invite):
        return {
            "kind": "send",
            "setup_url": "",
            "send_url": reverse(
                "client_renewal_send_quotation", kwargs={"pk": invite.pk}
            ),
            "quote_url": quote_url,
        }
    return {
        "kind": "none",
        "setup_url": "",
        "send_url": "",
        "quote_url": quote_url,
    }


def _renewal_product_groups(invites, *, zoho_connected=False, zoho_configured=False):
    """One product row per company, collapsing reminder emails for the same item."""
    buckets = {}
    for invite in invites:
        emails = invite.sent_to_emails or [invite.client_email]
        deal_url = reverse(
            "client_renewal_set_deal_status", kwargs={"pk": invite.pk}
        )
        for line in invite.lines.all():
            sub = getattr(line, "subscription", None)
            key = (
                "line",
                sub.pk if sub else f"{invite.pk}-{line.pk}",
                (invite.client_email or "").strip().lower(),
            )
            row = buckets.get(key)
            if row is None:
                status = (getattr(sub, "deal_status", "") or "") if sub else ""
                buckets[key] = {
                    "id": f"line-{sub.pk if sub else line.pk}",
                    "kind": "line",
                    "company_name": (line.company_name or "").strip(),
                    "product_name": line.product_name,
                    "is_new": False,
                    "client_email": invite.client_email,
                    "recipient_emails": emails,
                    "previous_quantity": line.current_quantity,
                    "renewal_quantity": line.renewal_count,
                    "deal_status": status,
                    "deal_status_url": deal_url,
                    "subscription_id": sub.pk if sub else None,
                    "form_empty": bool(getattr(sub, "win_form_empty", False))
                    if sub
                    else False,
                    "zoho_invite": invite if invite.submitted_at else None,
                    "zoho_quote_url": (invite.zoho_quote_url or "")
                    if invite.submitted_at
                    else "",
                    "events": [],
                    "deal_status_set_at": getattr(sub, "deal_status_set_at", None)
                    if sub
                    else None,
                }
                row = buckets[key]
            _append_invite_event(row["events"], invite)
            if invite.submitted_at and row["zoho_invite"] is None:
                row["zoho_invite"] = invite
            if invite.zoho_quote_url:
                row["zoho_quote_url"] = invite.zoho_quote_url
            if row["renewal_quantity"] is None and line.renewal_count is not None:
                row["renewal_quantity"] = line.renewal_count
                row["previous_quantity"] = line.current_quantity
        for item in invite.new_products.all():
            if not invite.submitted_at:
                continue
            prod_key = item.product_id or item.pk
            key = (
                "new",
                prod_key,
                (item.company_name or "").strip().lower(),
                (invite.client_email or "").strip().lower(),
            )
            row = buckets.get(key)
            if row is None:
                buckets[key] = {
                    "id": f"new-{prod_key}-{invite.pk}",
                    "kind": "new",
                    "company_name": (item.company_name or "").strip(),
                    "product_name": item.product_name,
                    "is_new": True,
                    "client_email": invite.client_email,
                    "recipient_emails": emails,
                    "previous_quantity": None,
                    "renewal_quantity": item.quantity,
                    "deal_status": "",
                    "deal_status_url": "",
                    "subscription_id": None,
                    "form_empty": False,
                    "zoho_invite": invite,
                    "zoho_quote_url": invite.zoho_quote_url or "",
                    "events": [],
                    "deal_status_set_at": None,
                }
                row = buckets[key]
            _append_invite_event(row["events"], invite)
            if invite.zoho_quote_url:
                row["zoho_quote_url"] = invite.zoho_quote_url

    grouped = {}
    for row in buckets.values():
        payload = {
            "id": row["id"],
            "kind": row["kind"],
            "company_name": row["company_name"],
            "product_name": row["product_name"],
            "is_new": row["is_new"],
            "client_email": row["client_email"],
            "recipient_emails": row["recipient_emails"],
            "previous_quantity": row["previous_quantity"],
            "renewal_quantity": row["renewal_quantity"],
            "deal_status": row["deal_status"],
            "deal_status_url": row["deal_status_url"],
            "subscription_id": row.get("subscription_id"),
            "form_empty": row.get("form_empty", False),
            "zoho": _zoho_payload(
                row["zoho_invite"],
                zoho_connected=zoho_connected,
                zoho_configured=zoho_configured,
                quote_url=row.get("zoho_quote_url") or "",
            ),
            "timestamps": _timestamps_from_events(
                row["events"],
                row["deal_status"],
                row["deal_status_set_at"],
            ),
        }
        _sort_key, label = _company_group_key(row["company_name"])
        bucket = grouped.setdefault(
            (_sort_key, label),
            {"company_name": label, "rows": []},
        )
        bucket["rows"].append(payload)

    groups = []
    for (_sort_key, _label), group in sorted(grouped.items(), key=lambda item: item[0]):
        group["rows"].sort(
            key=lambda row: (
                not row["is_new"],
                (row["product_name"] or "").lower(),
            )
        )
        groups.append(group)
    return groups


@sales_module_required
def client_renewal_list(request):
    company = _company(request)
    from business.renewal_read_tracking import poll_outlook_read_receipts_for_company

    _, poll_logs = poll_outlook_read_receipts_for_company(company)
    for message in poll_logs:
        if "failed" in message.lower():
            messages.warning(request, message)
    invites = list(_renewal_invite_queryset(company))
    from business.zoho_crm import zoho_configured

    configured = zoho_configured()
    return render(
        request,
        "business/client_renewal_list.html",
        {
            "company": company,
            "renewal_groups": _renewal_product_groups(
                invites,
                zoho_connected=company.zoho_connected,
                zoho_configured=configured,
            ),
            "zoho_connected": company.zoho_connected,
            "zoho_configured": configured,
        },
    )


@sales_module_required
def client_renewal_live(request):
    """Poll Outlook and return current renewal invite statuses as JSON."""
    company = _company(request)
    from business.renewal_read_tracking import poll_outlook_read_receipts_for_company

    updated, poll_logs = poll_outlook_read_receipts_for_company(company)
    invites = list(_renewal_invite_queryset(company))
    from business.zoho_crm import zoho_configured

    return JsonResponse(
        {
            "ok": True,
            "updated": updated,
            "polled_at": _format_dt(timezone.now()),
            "logs": poll_logs,
            "groups": _renewal_product_groups(
                invites,
                zoho_connected=company.zoho_connected,
                zoho_configured=zoho_configured(),
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
        return _deal_status_json(invite, company, next_url)

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

    action = (request.POST.get("action") or "").strip()
    extra = {"subscription_id": subscription_id}
    try:
        if action == "save_win_form":
            form = SubscriptionRenewForm(request.POST, company=company)
            if not form.is_valid():
                return JsonResponse(
                    {"ok": False, "error": _form_first_error(form)},
                    status=400,
                )
            invite.save_line_renewal_draft(
                subscription_id,
                start_date=form.cleaned_data["start_date"],
                end_date=form.cleaned_data.get("end_date"),
                quantity=form.cleaned_data["quantity"],
                billing_amount=form.cleaned_data["billing_amount"],
                vendor=form.cleaned_data.get("resolved_vendor"),
            )
            extra["deal_status"] = ClientSubscription.DealStatus.WIN
        else:
            valid = {
                ClientSubscription.DealStatus.WIN,
                ClientSubscription.DealStatus.LOST,
            }
            status = (request.POST.get("deal_status") or "").strip().lower()
            if status not in valid:
                return JsonResponse(
                    {"ok": False, "error": "Choose Win or Lost."},
                    status=400,
                )
            invite.apply_line_deal_status(subscription_id, status)
            extra["deal_status"] = status
            if status == ClientSubscription.DealStatus.WIN:
                extra["open_win_form"] = subscription_id
    except ValueError as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=400)

    invite = SubscriptionRenewalInvite.objects.prefetch_related(
        _renewal_invite_line_prefetch()
    ).get(pk=invite.pk)
    return _deal_status_json(invite, company, next_url, extra)


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
        return _after_quotation_redirect(request, invite)
    if not company.zoho_connected:
        messages.error(
            request,
            "Connect Zoho CRM in Company profile before sending a quotation.",
        )
        return _after_quotation_redirect(request, invite)

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
    return _after_quotation_redirect(request, invite)


def _after_quotation_redirect(request, invite):
    if request.POST.get("next") == reverse("client_renewal_list"):
        return redirect("client_renewal_list")
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
    next_url = _safe_next_url(
        request.POST.get("next") or request.GET.get("next"),
        "",
    )
    if request.method == "POST":
        form = VendorForm(request.POST)
        if form.is_valid():
            v = form.save(commit=False)
            v.company = company
            v.save()
            if next_url:
                return redirect(
                    _url_with_query(next_url, preselect_vendor=v.pk)
                )
            return redirect("vendor_list")
    else:
        form = VendorForm()
    return render(
        request,
        "business/vendor_form.html",
        {
            "company": company,
            "form": form,
            "title": "Add vendor",
            "next_url": next_url,
        },
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


@sales_module_required
@require_http_methods(["POST"])
def subscription_bulk_delete(request):
    company = _company(request)
    ids = request.POST.getlist("subscription_ids")
    if not ids:
        messages.warning(request, "Select at least one subscription to delete.")
        return redirect("subscription_list")
    qs = company.subscriptions.filter(pk__in=ids)
    n = _delete_subscriptions(company, qs)
    _delete_success_message(request, n, 0, "subscription(s)")
    return redirect("subscription_list")


@sales_module_required
@require_http_methods(["POST"])
def subscription_delete(request, pk):
    company = _company(request)
    obj = get_object_or_404(ClientSubscription, pk=pk, company=company)
    n = _delete_subscriptions(company, company.subscriptions.filter(pk=obj.pk))
    _delete_success_message(request, n, 0, "subscription")
    return redirect("subscription_list")


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


def _url_with_query(url: str, **extra) -> str:
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    for key, value in extra.items():
        if value is None:
            continue
        query[key] = str(value)
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
    )


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
    initial = obj.renewal_form_initial()

    if request.method == "POST":
        if request.POST.get("vendor") == SubscriptionRenewForm.NEW_VENDOR:
            renew_return = reverse("subscription_renew", kwargs={"pk": pk})
            posted_next = (request.POST.get("next") or "").strip()
            if posted_next:
                renew_return = _url_with_query(renew_return, next=posted_next)
            return redirect(
                f"{reverse('vendor_add')}?{urlencode({'next': renew_return})}"
            )
        form = SubscriptionRenewForm(request.POST, company=company)
        if form.is_valid():
            new_start = form.cleaned_data["start_date"]
            new_end = form.cleaned_data["end_date"]
            new_qty = form.cleaned_data["quantity"]
            new_price = form.cleaned_data["billing_amount"]
            new_vendor = form.cleaned_data["resolved_vendor"]
            obj.apply_saved_renewal(
                start_date=new_start,
                end_date=new_end,
                quantity=new_qty,
                billing_amount=new_price,
                vendor=new_vendor,
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
        initial_vendor = initial["vendor"]
        preselect = (request.GET.get("preselect_vendor") or "").strip()
        if preselect and company.vendors.filter(pk=preselect).exists():
            initial_vendor = preselect
        form = SubscriptionRenewForm(
            company=company,
            initial={
                "start_date": initial["start_date"] or suggested_start,
                "end_date": (
                    initial["end_date"]
                    if obj.win_form_completed
                    else (initial["end_date"] or suggested_end)
                ),
                "quantity": initial["quantity"],
                "billing_amount": initial["billing_amount"],
                "vendor": initial_vendor,
            },
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
        "charts": ticket_charts_from_views(base),
    }


def _format_ticket_dt(value, fmt: str) -> str:
    if not value:
        return ""
    return date_format(timezone.localtime(value), fmt)


@support_ticket_access_required
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
            "charts": data["charts"],
        },
    )


@support_ticket_access_required
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
            "charts": data["charts"],
        }
    )


@support_ticket_access_required
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


@support_ticket_access_required
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


@support_ticket_access_required
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


@support_ticket_access_required
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
        CaseLockInvite.objects.select_related("company", "inbox", "inbox__company"),
        pk=invite_id,
    )
    company = invite.company
    if company is None and invite.inbox_id:
        company = invite.inbox.company
    if CaseLockSubmission.objects.filter(invite=invite).exists():
        return render(
            request,
            "business/caselock_public_done_guest.html",
            {"invite": invite, "public_page": True},
        )
    if request.method == "POST":
        form = CaseLockPublicForm(request.POST, company=company)
        if form.is_valid():
            sub = form.save(commit=False)
            sub.invite = invite
            product = form.cleaned_data["product"]
            sub.product = product
            sub.product_name = product.name
            sub.save()
            return render(
                request,
                "business/caselock_public_thanks_guest.html",
                {"invite": invite, "public_page": True},
            )
    else:
        form = CaseLockPublicForm(company=company)
    return render(
        request,
        "business/caselock_public_form_guest.html",
        {
            "form": form,
            "invite": invite,
            "company": company,
            "public_page": True,
        },
    )


def _caselock_page_data(company):
    submissions = list(
        CaseLockSubmission.objects.filter(invite__company=company)
        .select_related("invite", "product")
        .order_by("-timestamp")
    )
    extra = list(
        CaseLockSubmission.objects.filter(
            invite__company__isnull=True, invite__inbox__company=company
        )
        .select_related("invite", "product")
        .order_by("-timestamp")
    )
    if extra:
        seen = {s.pk for s in submissions}
        submissions.extend(s for s in extra if s.pk not in seen)
        submissions.sort(key=lambda s: s.timestamp, reverse=True)
    annotate_internal_case_locks(submissions)

    invites = list(
        CaseLockInvite.objects.filter(Q(company=company) | Q(inbox__company=company))
        .select_related("company", "inbox")
        .annotate(
            has_submission=Exists(
                CaseLockSubmission.objects.filter(invite_id=OuterRef("pk"))
            )
        )
        .order_by("-received_at", "-created_at")
        .distinct()
    )
    return submissions, invites


def _caselock_live_payload(company):
    submissions, invites = _caselock_page_data(company)
    mail_rows = []
    for invite in invites:
        mail_rows.append(
            {
                "id": str(invite.pk),
                "from": invite.sender_email or "",
                "subject": invite.mail_subject or "",
                "received_at": _format_ticket_dt(invite.inbound_at, "d/m/Y H:i:s")
                or "—",
                "form_sent_at": _format_ticket_dt(invite.email_sent_at, "d/m/Y H:i:s")
                or "—",
                "form_submitted": bool(getattr(invite, "has_submission", False)),
            }
        )
    submission_rows = []
    for sub in submissions:
        invite = sub.invite
        submission_rows.append(
            {
                "id": sub.pk,
                "mail_sent_at": _format_ticket_dt(invite.email_sent_at, "d/m/Y H:i:s")
                or "—",
                "submitted_at": _format_ticket_dt(sub.timestamp, "d/m/Y H:i:s") or "—",
                "product_name": sub.product_name or "",
                "client_name": sub.client_name or "",
                "client_gst": sub.client_gst or "",
                "quantity": sub.product_quantity,
                "submitted_by": invite.sender_email or "",
                "has_internal_case_lock": bool(
                    getattr(sub, "has_internal_case_lock", False)
                ),
                "status": sub.status,
                "status_label": sub.get_status_display(),
                "status_url": reverse(
                    "caselock_submission_status_update", args=[sub.pk]
                ),
            }
        )
    return {
        "mail_rows": mail_rows,
        "submission_rows": submission_rows,
        "mail_count": len(mail_rows),
        "submission_count": len(submission_rows),
    }


def _maybe_poll_caselock_mailbox(company):
    if not should_poll_mailbox_on_request():
        return {}
    return maybe_fetch_on_dashboard(company) or {}


@company_owner_required
def caselock_inbox_list(request):
    company = _company(request)
    fetch_result = _maybe_poll_caselock_mailbox(company)
    created = int(fetch_result.get("created") or 0)
    if created:
        messages.success(
            request,
            f"Imported {created} new Case lock mail(s) and sent form links.",
        )

    tab = (request.GET.get("tab") or "dashboard").strip().lower()
    if tab not in ("dashboard", "mails"):
        tab = "dashboard"

    submissions, invites = _caselock_page_data(company)
    product_form = CaseLockProductForm(company=company)
    products = company.caselock_products.order_by("name")

    return render(
        request,
        "business/caselock_inbox_list.html",
        {
            "company": company,
            "active_tab": tab,
            "submissions": submissions,
            "invites": invites,
            "products": products,
            "product_form": product_form,
            "mailbox_email": (company.caselock_email or "").strip(),
        },
    )


@company_owner_required
def caselock_inbox_live(request):
    """Return current Case lock rows as JSON. Mailbox poll is local-only; server uses Celery."""
    company = _company(request)
    fetch_result = _maybe_poll_caselock_mailbox(company)
    created = int(fetch_result.get("created") or 0)
    payload = _caselock_live_payload(company)
    return JsonResponse(
        {
            "ok": True,
            "created": created,
            "inbox_errors": poll_error_logs(fetch_result),
            "polled_at": _format_dt(timezone.now()),
            "mailbox_poll_on_request": should_poll_mailbox_on_request(),
            "mail_rows": payload["mail_rows"],
            "submission_rows": payload["submission_rows"],
            "mail_count": payload["mail_count"],
            "submission_count": payload["submission_count"],
        }
    )


@company_owner_required
@require_http_methods(["POST"])
def caselock_product_add(request):
    company = _company(request)
    form = CaseLockProductForm(request.POST, company=company)
    if form.is_valid():
        obj = form.save(commit=False)
        obj.company = company
        obj.save()
        messages.success(request, f'Product "{obj.name}" added.')
    else:
        messages.error(request, " ".join(form.errors.get("name", ["Could not add product."])))
    return redirect(f"{reverse('caselock_inbox_list')}?tab=dashboard")


@company_owner_required
@require_http_methods(["POST"])
def caselock_product_delete(request, pk):
    company = _company(request)
    obj = get_object_or_404(CaseLockProduct, pk=pk, company=company)
    name = obj.name
    obj.delete()
    messages.success(request, f'Product "{name}" removed.')
    return redirect(f"{reverse('caselock_inbox_list')}?tab=dashboard")


@company_owner_required
def caselock_inbox_add(request):
    messages.info(
        request,
        "Set the Case lock email on Company profile. Products are created on the Case lock dashboard.",
    )
    return redirect("caselock_inbox_list")


@company_owner_required
def caselock_inbox_edit(request, pk):
    return redirect("caselock_inbox_list")


@company_owner_required
def caselock_submissions(request):
    return redirect("caselock_inbox_list")


@company_owner_required
@require_http_methods(["POST"])
def caselock_submission_status_update(request, pk):
    company = _company(request)
    sub = get_object_or_404(
        CaseLockSubmission.objects.select_related("invite", "invite__inbox", "invite__company"),
        pk=pk,
    )
    invite_company_id = sub.invite.company_id
    if invite_company_id is None and sub.invite.inbox_id:
        invite_company_id = sub.invite.inbox.company_id
    if invite_company_id != company.pk:
        messages.error(request, "Submission not found.")
        return redirect("caselock_inbox_list")
    action = (request.POST.get("action") or "").strip().lower()
    if action == "approve" or action == "final":
        sub.status = CaseLockSubmission.Status.APPROVED
        sub.status_changed_at = timezone.now()
        sub.save(update_fields=["status", "status_changed_at"])
        if send_caselock_decision_email(sub):
            messages.success(request, "Final case lock email sent.")
        else:
            messages.warning(request, "Marked final, but notification email failed.")
    elif action == "decline":
        sub.status = CaseLockSubmission.Status.DECLINED
        sub.status_changed_at = timezone.now()
        sub.save(update_fields=["status", "status_changed_at"])
        if send_caselock_decision_email(sub):
            messages.success(request, "Case lock declined and email sent.")
        else:
            messages.warning(request, "Case lock declined, but notification email failed.")
    else:
        messages.error(request, "Invalid action.")

    return redirect(f"{reverse('caselock_inbox_list')}?tab=dashboard")
