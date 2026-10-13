import csv
import logging
import os
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from io import StringIO
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.exceptions import SuspiciousFileOperation, ValidationError
from django.core.paginator import Paginator
from django.db.models import Count, Exists, OuterRef, Prefetch, Q, Sum
from django.http import FileResponse, Http404, HttpResponse, JsonResponse, QueryDict
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.formats import date_format
from django.views.decorators.http import require_http_methods
from openpyxl import Workbook

logger = logging.getLogger(__name__)

from accounts.models import CompanyProfile
from accounts.permissions import (
    backup_access_required,
    company_owner_required,
    get_company_for_user,
    sales_module_required,
    support_ticket_access_required,
)

from .forms import (
    CaseLockProductForm,
    CaseLockPublicForm,
    ClientBulkEmailForm,
    ClientForm,
    ClientSubscriptionForm,
    LeadForm,
    LeadPublicForm,
    LeadStatusForm,
    RenewalConfirmLineFormSet,
    RenewalNewProductFormSet,
    SubscriptionRenewForm,
    ProductForm,
    SupportSLASettingsForm,
    SupportTicketCommentForm,
    SupportTicketForm,
    TicketPriorityRuleFormSet,
    VendorForm,
    RenewalSLASettingsForm,
)
from . import helpdesk_bridge
from .analytics import sales_dashboard_charts, ticket_charts_from_views
from .support_team import assignable_users, get_support_team, team_picker_context
from .models import (
    CaseLockInvite,
    CaseLockProduct,
    CaseLockSubmission,
    annotate_internal_case_locks,
    Client,
    ClientSubscription,
    Lead,
    Product,
    SubscriptionRenewalInvite,
    SubscriptionRenewalInviteLine,
    SubscriptionRenewalInviteNewProduct,
    VendorPriceRequest,
    lost_reminder_resume_on,
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
from .subscription_export import (
    DEFAULT_EXPORT_COLUMNS,
    EXPORT_COLUMNS,
    NO_COMPANY_LABEL,
    NO_COMPANY_VALUE,
    apply_company_selection,
    build_subscription_export_bytes,
    distinct_product_company_names,
    normalize_column_keys,
)
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


def _json_error(message: str, status: int = 500):
    return JsonResponse({"ok": False, "error": message}, status=status)


def _xlsx_response(content, filename: str):
    response = HttpResponse(
        content,
        content_type=(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
    )
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


def _pdf_response(content, filename: str):
    response = HttpResponse(content, content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


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


def _product_company_vendor_map(company):
    """Company names (case-insensitive) and the vendors tagged with each on the Vendor page."""
    labels = {}
    vendor_companies = {}

    def add_label(raw):
        label = (raw or "").strip()
        if not label:
            return ""
        key = label.lower()
        current = labels.get(key)
        if current is None or (label[:1].isupper() and not current[:1].isupper()):
            labels[key] = label
        return key

    for name in company.products.values_list("company_name", flat=True):
        add_label(name)
    for vendor_id, catalog in company.vendors.values_list("pk", "catalog_companies"):
        for name in catalog or []:
            key = add_label(name)
            if key:
                vendor_companies.setdefault(vendor_id, set()).add(key)
    companies = [
        {"key": key, "label": label}
        for key, label in sorted(labels.items(), key=lambda item: item[1].lower())
    ]
    return companies, vendor_companies


def _attach_dashboard_zoho(company, subs):
    """Set ``zoho`` and ``zoho_ref_id`` on each subscription from its latest renewal email."""
    from business.zoho_crm import zoho_configured

    configured = zoho_configured()
    lines = (
        SubscriptionRenewalInviteLine.objects.filter(
            subscription__in=subs,
            invite__company=company,
            invite__email_sent_at__isnull=False,
        )
        .select_related("invite")
        .order_by("-invite__email_sent_at", "-invite__created_at")
    )
    best = {}
    for line in lines:
        invite = line.invite
        current = best.get(line.subscription_id)
        if current is None or (invite.zoho_quote_url and not current.zoho_quote_url):
            best[line.subscription_id] = invite
    for sub in subs:
        invite = best.get(sub.pk)
        if invite is None:
            sub.zoho = None
            sub.zoho_ref_id = ""
            continue
        sub.zoho = _zoho_payload(
            invite,
            zoho_connected=company.zoho_connected,
            zoho_configured=configured,
        )
        sub.zoho_ref_id = (
            (invite.zoho_quote_number or "").strip()
            or (invite.zoho_quote_id or "").strip()
        )


@sales_module_required
def dashboard(request):
    company = _company(request)
    expiring_days = _parse_expiring_days(request.GET.get("expiring_days", ""))
    today = timezone.now().date()
    expiring_qs = _filter_expiring_soon(
        company.subscriptions.filter(status=ClientSubscription.Status.ACTIVE),
        expiring_days,
        today,
    ).exclude(
        deal_status=ClientSubscription.DealStatus.WIN,
        win_form_completed=True,
    ).exclude(
        deal_status=ClientSubscription.DealStatus.LOST,
    )
    expiring_subs = list(
        expiring_qs.select_related("client", "product", "vendor").order_by(
            "end_date", "client__name"
        )[:50]
    )
    _attach_dashboard_zoho(company, expiring_subs)
    quote_companies, vendor_companies = _product_company_vendor_map(company)
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
        "quote_vendors": [
            {
                "id": vendor.pk,
                "name": vendor.name,
                "has_email": bool(vendor.contact_emails()),
                "companies": sorted(vendor_companies.get(vendor.pk, ())),
            }
            for vendor in company.vendors.all().order_by("name")
        ],
        "quote_companies": quote_companies,
        "charts": charts,
    }
    return render(request, "business/dashboard.html", ctx)


@sales_module_required
@require_http_methods(["POST"])
def dashboard_set_deal_lost(request, pk):
    company = _company(request)
    sub = get_object_or_404(ClientSubscription, pk=pk, company=company)
    if sub.deal_status and sub.deal_status != ClientSubscription.DealStatus.CANCEL:
        return JsonResponse(
            {"ok": False, "error": "Deal status is already set."},
            status=400,
        )
    sub.deal_status = ClientSubscription.DealStatus.LOST
    if not sub.deal_status_set_at:
        sub.deal_status_set_at = timezone.now()
    sub.lost_remark = (request.POST.get("lost_remark") or "").strip()
    sub.lost_reminder_resume_on = lost_reminder_resume_on(sub.end_date)
    sub.save(
        update_fields=[
            "deal_status",
            "deal_status_set_at",
            "lost_remark",
            "lost_reminder_resume_on",
        ]
    )
    invite_line = (
        SubscriptionRenewalInviteLine.objects.filter(subscription=sub)
        .select_related("invite")
        .order_by("-id")
        .first()
    )
    if invite_line and invite_line.invite_id and not invite_line.invite.deal_status_set_at:
        invite_line.invite.deal_status_set_at = sub.deal_status_set_at
        invite_line.invite.save(update_fields=["deal_status_set_at"])
    return JsonResponse({"ok": True, "deal_status": ClientSubscription.DealStatus.LOST})


@sales_module_required
@require_http_methods(["POST"])
def dashboard_quote_vendors(request, pk):
    company = _company(request)
    sub = get_object_or_404(
        ClientSubscription.objects.select_related("client", "product"),
        pk=pk,
        company=company,
    )
    raw_ids = request.POST.getlist("vendor_ids")
    vendor_ids = []
    for raw in raw_ids:
        try:
            vendor_ids.append(int(raw))
        except (TypeError, ValueError):
            continue
    if not vendor_ids:
        return JsonResponse(
            {"ok": False, "error": "Select at least one vendor."},
            status=400,
        )
    vendors = list(company.vendors.filter(pk__in=vendor_ids).order_by("name"))
    if not vendors:
        return JsonResponse(
            {"ok": False, "error": "Select at least one vendor."},
            status=400,
        )
    from business.vendor_quote import send_subscription_price_requests

    result = send_subscription_price_requests(company, sub, vendors)
    if not result["queued"]:
        return JsonResponse(
            {
                "ok": False,
                "error": " ".join(result["errors"]) or "Could not send the email.",
            },
            status=400,
        )
    names = ", ".join(result["queued"])
    deal_no = result.get("deal_no")
    deal_suffix = f" (Deal No: {deal_no})" if deal_no else ""
    message = f"Price request is being sent to {names}{deal_suffix}."
    if result["errors"]:
        message = f"{message} {' '.join(result['errors'])}"
    return JsonResponse(
        {"ok": True, "message": message, "queued": result["queued"], "deal_no": deal_no}
    )


@sales_module_required
@require_http_methods(["GET"])
def dashboard_subscription_renewal_info(request, pk):
    company = _company(request)
    sub = get_object_or_404(
        ClientSubscription.objects.select_related("client", "product"),
        pk=pk,
        company=company,
    )
    payload = {
        "id": sub.pk,
        "client_name": sub.client.name,
        "product_name": sub.product.name,
        "subscription_list_id": sub.subscription_list_id or "",
        "quantity": sub.quantity,
        "start_date": _format_date(sub.start_date),
        "end_date": _format_date(sub.end_date),
        "days_left": sub.days_until_expiry,
        "deal_status": sub.deal_status or "",
        "lost_remark": sub.lost_remark or "",
        "client_emails": sub.client.contact_emails(),
    }

    vendor_quotes = []
    deal_no = ""
    deal_url = ""
    try:
        from business.models import ProcurementDeal, VendorPriceRequest

        proc_deal = (
            ProcurementDeal.objects.filter(
                company=company, deal_name=f"Subscription:{sub.pk}"
            )
            .order_by("-id")
            .first()
        )
        if proc_deal:
            deal_no = proc_deal.deal_no
            deal_url = reverse("procurement_detail", kwargs={"deal_no": deal_no})

        requests = (
            VendorPriceRequest.objects.filter(company=company, subscription=sub)
            .select_related("vendor")
            .order_by("vendor__name", "vendor_id", "created_at", "id")
        )
        email_numbers = {}
        for req in requests:
            email_numbers[req.vendor_id] = email_numbers.get(req.vendor_id, 0) + 1
            vendor_quotes.append(
                {
                    "vendor_id": req.vendor_id,
                    "vendor_name": req.vendor.name,
                    "email_no": email_numbers[req.vendor_id],
                    "sent_at": _format_dt(req.email_sent_at or req.created_at),
                    "price": float(req.price) if req.price is not None else None,
                    "submitted_at": _format_dt(req.submitted_at) if req.submitted_at else "",
                    "status": "Submitted" if req.submitted_at else ("Sent" if req.email_sent_at else "Pending"),
                }
            )
        vendor_quotes.sort(key=lambda vq: (vq["vendor_name"].lower(), vq["vendor_id"], -vq["email_no"]))
    except Exception:
        logger.exception("Failed to load vendor quotes for subscription %s", sub.pk)

    try:
        invite_ids = SubscriptionRenewalInviteLine.objects.filter(
            subscription=sub
        ).values("invite_id")
        invites = list(_renewal_invite_queryset(company).filter(pk__in=invite_ids))
        if not invites:
            return JsonResponse(
                {
                    "ok": True,
                    "sent": False,
                    "subscription": payload,
                    "row": None,
                    "new_products": [],
                    "vendor_quotes": vendor_quotes,
                    "procurement_deal_no": deal_no,
                    "procurement_deal_url": deal_url,
                }
            )
        from business.renewal_sla import get_or_create_renewal_sla_settings
        from business.zoho_crm import zoho_configured

        groups = _renewal_product_groups(
            invites,
            company=company,
            zoho_connected=company.zoho_connected,
            zoho_configured=zoho_configured(),
            sla_settings=get_or_create_renewal_sla_settings(company),
            include_won=False,
        )
        rows = [row for group in groups for row in group["rows"]]
        line_rows = [
            row for row in rows if row.get("kind") == "line" and row.get("subscription_id") == sub.pk
        ]
        chosen = None
        for row in line_rows:
            if row.get("renewal_quantity") is not None:
                chosen = row
                break
        if chosen is None and line_rows:
            chosen = line_rows[0]
        if chosen is None:
            return JsonResponse(
                {
                    "ok": True,
                    "sent": False,
                    "subscription": payload,
                    "row": None,
                    "new_products": [],
                    "vendor_quotes": vendor_quotes,
                    "procurement_deal_no": deal_no,
                    "procurement_deal_url": deal_url,
                }
            )
        new_products = [row for row in rows if row.get("kind") == "new"]
        return JsonResponse(
            {
                "ok": True,
                "sent": True,
                "subscription": payload,
                "row": chosen,
                "new_products": new_products,
                "vendor_quotes": vendor_quotes,
                "procurement_deal_no": deal_no,
                "procurement_deal_url": deal_url,
            }
        )
    except Exception:
        logger.exception(
            "dashboard_subscription_renewal_info failed company_id=%s sub_id=%s",
            company.pk,
            sub.pk,
        )
        return _json_error("Could not load renewal info.")


@require_http_methods(["GET", "POST"])
def vendor_quote_public(request, token):
    quote = get_object_or_404(
        VendorPriceRequest.objects.select_related("company", "vendor"),
        token=token,
    )
    if quote.submitted_at:
        return render(
            request,
            "business/vendor_quote_public.html",
            {"quote": quote, "submitted": True, "public_page": True},
        )
    error = ""
    posted_price = ""
    if request.method == "POST":
        posted_price = (request.POST.get("price") or "").strip()
        try:
            price = Decimal(posted_price)
        except (InvalidOperation, ValueError):
            price = None
        if price is None or price < 0:
            error = "Enter a valid price."
        else:
            quote.price = price.quantize(Decimal("0.01"))
            quote.submitted_at = timezone.now()
            quote.save(update_fields=["price", "submitted_at"])

            try:
                from business.models import (
                    DealRound,
                    DealRoundVendorPrice,
                    DealVendor,
                    ProcurementDeal,
                    VendorPriceSubmission,
                    VendorProductPrice,
                )
                from business.team_notifications import notify_quotation_submitted

                proc_deal = (
                    ProcurementDeal.objects.filter(
                        company=quote.company, deal_name=f"Subscription:{quote.subscription_id}"
                    )
                    .order_by("-id")
                    .first()
                )
                if proc_deal:
                    deal_no = proc_deal.deal_no
                    DealVendor.objects.filter(
                        company=quote.company, deal_no=deal_no, vendor=quote.vendor
                    ).update(price=quote.price)
                    round_obj = (
                        DealRound.objects.filter(company=quote.company, deal_no=deal_no)
                        .order_by("-round_number")
                        .first()
                    )
                    if round_obj:
                        DealRoundVendorPrice.objects.update_or_create(
                            deal_round=round_obj,
                            vendor=quote.vendor,
                            product=quote.subscription.product,
                            defaults={"price": quote.price},
                        )
                    subm = VendorPriceSubmission.objects.filter(
                        company=quote.company, deal_no=deal_no, vendor=quote.vendor
                    ).first()
                    if subm:
                        subm.is_submitted = True
                        subm.submitted_at = quote.submitted_at
                        subm.save(update_fields=["is_submitted", "submitted_at"])
                        VendorProductPrice.objects.update_or_create(
                            submission=subm,
                            product=quote.subscription.product,
                            defaults={"price": quote.price},
                        )
                        notify_quotation_submitted(subm)
            except Exception:
                logger.exception("Failed to sync vendor quote to procurement deal")

            return render(
                request,
                "business/vendor_quote_public.html",
                {"quote": quote, "submitted": True, "public_page": True},
            )
    return render(
        request,
        "business/vendor_quote_public.html",
        {
            "quote": quote,
            "submitted": False,
            "error": error,
            "posted_price": posted_price,
            "public_page": True,
        },
    )



def _renewal_invite_line_prefetch():
    return Prefetch(
        "lines",
        queryset=SubscriptionRenewalInviteLine.objects.select_related(
            "subscription",
            "subscription__client",
            "subscription__product",
            "subscription__renewal_draft_product",
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


def _invite_deal_status_url(invite):
    return reverse("client_renewal_set_deal_status", kwargs={"pk": invite.pk})


def _invite_deal_payload(invite):
    state = invite.deal_table_state()
    return {
        "deal_pending": state["pending"],
        "deal_badges": state["badges"],
        "deal_form_empty": state.get("form_empty", False),
        "deal_form_empty_count": state.get("form_empty_count", 0),
        "deal_status_url": _invite_deal_status_url(invite),
    }


def _line_deal_payload(invite, line):
    sub = getattr(line, "subscription", None)
    status = getattr(sub, "deal_status", "") or ""
    form_empty = bool(getattr(sub, "win_form_empty", False))
    return {
        "deal_pending": not status,
        "deal_badges": [status] if status else [],
        "deal_form_empty": form_empty,
        "deal_form_empty_count": 1 if form_empty else 0,
        "deal_status_url": _invite_deal_status_url(invite),
        "subscription_id": sub.pk if sub else None,
    }


def _company_vendor_choices(company):
    return [
        {"id": vendor.pk, "name": vendor.name}
        for vendor in company.vendors.all().order_by("name")
    ]


def _company_product_choices(company):
    return [
        {
            "id": product.pk,
            "name": product.name,
            "price": f"{product.unit_price:.2f}",
        }
        for product in company.products.all().order_by("unit_price", "name")
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
        "products": _company_product_choices(company),
        **_invite_deal_payload(invite),
    }
    if extra:
        payload.update(extra)
    return JsonResponse(payload)


def _format_dt(value):
    if not value:
        return ""
    return date_format(timezone.localtime(value), "d M Y, H:i:s")


def _format_date(value):
    if not value:
        return ""
    return date_format(value, "d M Y")


def _sla_off_payload():
    return {
        "status": "off",
        "label": "—",
        "zoho_status": "none",
        "zoho_status_label": "—",
        "zoho_stage": "",
        "target_hours": 0,
        "elapsed_hours": None,
        "elapsed_label": "—",
        "remaining_hours": None,
        "remaining_label": "—",
        "qty_to_quote_hours": None,
        "qty_to_quote_label": "—",
        "quote_to_send_hours": None,
        "quote_to_send_label": "—",
        "clock_mode": "",
        "quantity_at": "",
        "quote_number_at": "",
        "quote_sent_at": "",
        "due_at": "",
    }


def _sla_row_payload(invite, sla_settings):
    if invite is None:
        return _sla_off_payload()
    from business.renewal_sla import sla_status_for_invite

    raw = sla_status_for_invite(invite, sla_settings)
    return {
        "status": raw["status"],
        "label": raw["label"],
        "zoho_status": raw["zoho_status"],
        "zoho_status_label": raw["zoho_status_label"],
        "zoho_stage": raw["zoho_stage"],
        "target_hours": raw["target_hours"],
        "elapsed_hours": raw["elapsed_hours"],
        "elapsed_label": raw["elapsed_label"],
        "remaining_hours": raw["remaining_hours"],
        "remaining_label": raw["remaining_label"],
        "qty_to_quote_hours": raw["qty_to_quote_hours"],
        "qty_to_quote_label": raw["qty_to_quote_label"],
        "quote_to_send_hours": raw["quote_to_send_hours"],
        "quote_to_send_label": raw["quote_to_send_label"],
        "clock_mode": raw["clock_mode"],
        "quantity_at": _format_dt(raw["quantity_at"]),
        "quote_number_at": _format_dt(raw["quote_number_at"]),
        "quote_sent_at": _format_dt(raw["quote_sent_at"]),
        "due_at": _format_dt(raw["due_at"]),
    }


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
    if invite:
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


def _display_ref_id(row):
    return (
        (row.get("zoho_quote_number") or "").strip()
        or (row.get("zoho_quote_id") or "").strip()
    )


def _renewed_product_name(sub, original_name):
    if not sub or not sub.win_form_completed:
        return ""
    draft = getattr(sub, "renewal_draft_product", None)
    if draft is None:
        return ""
    name = (draft.name or "").strip()
    if name.lower() == (original_name or "").strip().lower():
        return ""
    return name


def _renewal_product_groups(
    invites,
    *,
    company=None,
    zoho_connected=False,
    zoho_configured=False,
    sla_settings=None,
    include_won=True,
):
    """One product row per company, collapsing reminder emails for the same item."""
    if company is None and invites:
        company = invites[0].company
    if sla_settings is None and company is not None:
        from business.renewal_sla import get_or_create_renewal_sla_settings

        sla_settings = get_or_create_renewal_sla_settings(company)
    buckets = {}
    seen_subscription_ids = set()
    for invite in invites:
        emails = invite.sent_to_emails or [invite.client_email]
        deal_url = reverse(
            "client_renewal_set_deal_status", kwargs={"pk": invite.pk}
        )
        for line in invite.lines.all():
            sub = getattr(line, "subscription", None)
            if sub:
                seen_subscription_ids.add(sub.pk)
            key = (
                "line",
                sub.pk if sub else f"{invite.pk}-{line.pk}",
                (invite.client_email or "").strip().lower(),
            )
            row = buckets.get(key)
            if row is None:
                status = (getattr(sub, "deal_status", "") or "") if sub else ""
                renew_url = ""
                if sub and not sub.win_form_completed:
                    renew_url = "{}?{}".format(
                        reverse("subscription_renew", kwargs={"pk": sub.pk}),
                        urlencode(
                            {
                                "next": reverse("client_renewal_list"),
                                "required": "1",
                            }
                        ),
                    )
                buckets[key] = {
                    "id": f"line-{sub.pk if sub else line.pk}",
                    "kind": "line",
                    "company_name": (line.company_name or "").strip(),
                    "product_name": line.product_name,
                    "renewed_product_name": _renewed_product_name(
                        sub, line.product_name
                    ),
                    "is_new": False,
                    "client_email": invite.client_email,
                    "recipient_emails": emails,
                    "previous_quantity": line.current_quantity,
                    "renewal_quantity": line.renewal_count,
                    "deal_status": status,
                    "deal_status_url": deal_url,
                    "renew_url": renew_url,
                    "subscription_id": sub.pk if sub else None,
                    "form_empty": bool(getattr(sub, "win_form_empty", False))
                    if sub
                    else False,
                    "lost_remark": (getattr(sub, "lost_remark", "") or "") if sub else "",
                    "zoho_invite": invite,
                    "zoho_quote_url": invite.zoho_quote_url or "",
                    "zoho_quote_id": invite.zoho_quote_id or "",
                    "zoho_quote_number": invite.zoho_quote_number or "",
                    "events": [],
                    "deal_status_set_at": getattr(sub, "deal_status_set_at", None)
                    if sub
                    else None,
                    "start_date": _format_date(getattr(sub, "start_date", None))
                    if sub
                    else "",
                    "end_date": _format_date(getattr(sub, "end_date", None))
                    if sub
                    else "",
                    "subscription_list_id": (
                        getattr(sub, "subscription_list_id", "") if sub else ""
                    )
                    or "",
                }
                row = buckets[key]
            _append_invite_event(row["events"], invite)
            if invite.submitted_at:
                row["zoho_invite"] = invite
            if invite.zoho_quote_url:
                row["zoho_quote_url"] = invite.zoho_quote_url
            if invite.zoho_quote_id:
                row["zoho_quote_id"] = invite.zoho_quote_id
            if invite.zoho_quote_number:
                row["zoho_quote_number"] = invite.zoho_quote_number
            if row["renewal_quantity"] is None and line.renewal_count is not None:
                row["renewal_quantity"] = line.renewal_count
                row["previous_quantity"] = line.current_quantity
            if sub and sub.win_form_completed:
                row["renew_url"] = ""
                row["form_empty"] = False
                row["deal_status"] = ClientSubscription.DealStatus.WIN
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
                    "renew_url": "",
                    "subscription_id": None,
                    "form_empty": False,
                    "lost_remark": "",
                    "zoho_invite": invite,
                    "zoho_quote_url": invite.zoho_quote_url or "",
                    "zoho_quote_id": invite.zoho_quote_id or "",
                    "zoho_quote_number": invite.zoho_quote_number or "",
                    "events": [],
                    "deal_status_set_at": None,
                    "start_date": "",
                    "end_date": "",
                    "subscription_list_id": "",
                }
                row = buckets[key]
            _append_invite_event(row["events"], invite)
            if invite.zoho_quote_url:
                row["zoho_quote_url"] = invite.zoho_quote_url
            if invite.zoho_quote_id:
                row["zoho_quote_id"] = invite.zoho_quote_id
            if invite.zoho_quote_number:
                row["zoho_quote_number"] = invite.zoho_quote_number

    if include_won and company is not None:
        won_subs = (
            company.subscriptions.filter(
                deal_status=ClientSubscription.DealStatus.WIN,
                win_form_completed=True,
            )
            .exclude(pk__in=seen_subscription_ids)
            .select_related("client", "product")
            .order_by("client__name", "product__name", "id")
        )
        for sub in won_subs:
            client_name = (sub.client.name or "").strip()
            renewal_qty = (
                sub.renewal_draft_quantity
                if sub.renewal_draft_quantity is not None
                else sub.quantity
            )
            buckets[("won", sub.pk)] = {
                "id": f"won-{sub.pk}",
                "kind": "line",
                "company_name": client_name,
                "product_name": sub.product.name,
                "is_new": False,
                "client_email": sub.client.email or "",
                "recipient_emails": sub.client.contact_emails(),
                "previous_quantity": sub.quantity,
                "renewal_quantity": renewal_qty,
                "deal_status": ClientSubscription.DealStatus.WIN,
                "deal_status_url": "",
                "renew_url": "",
                "subscription_id": sub.pk,
                "form_empty": False,
                "lost_remark": sub.lost_remark or "",
                "zoho_invite": None,
                "zoho_quote_url": "",
                "zoho_quote_id": "",
                "zoho_quote_number": "",
                "events": [],
                "deal_status_set_at": sub.deal_status_set_at,
                "start_date": _format_date(sub.start_date),
                "end_date": _format_date(sub.end_date),
                "subscription_list_id": sub.subscription_list_id or "",
            }

    grouped = {}
    for row in buckets.values():
        payload = {
            "id": row["id"],
            "kind": row["kind"],
            "company_name": row["company_name"],
            "product_name": row["product_name"],
            "renewed_product_name": row.get("renewed_product_name") or "",
            "is_new": row["is_new"],
            "client_email": row["client_email"],
            "recipient_emails": row["recipient_emails"],
            "previous_quantity": row["previous_quantity"],
            "renewal_quantity": row["renewal_quantity"],
            "deal_status": row["deal_status"],
            "deal_status_url": row["deal_status_url"],
            "renew_url": row.get("renew_url") or "",
            "subscription_id": row.get("subscription_id"),
            "form_empty": row.get("form_empty", False),
            "lost_remark": row.get("lost_remark") or "",
            "start_date": row.get("start_date") or "",
            "end_date": row.get("end_date") or "",
            "subscription_list_id": row.get("subscription_list_id") or "",
            "zoho_quote_id": row.get("zoho_quote_id") or "",
            "zoho_quote_number": row.get("zoho_quote_number") or "",
            "ref_id": _display_ref_id(row),
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
            "sla": _sla_row_payload(row["zoho_invite"], sla_settings),
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
    from business.renewal_sla import (
        get_or_create_renewal_sla_settings,
        sync_zoho_quote_sent_status,
    )

    sla_settings = get_or_create_renewal_sla_settings(company)
    sla_form = RenewalSLASettingsForm(instance=sla_settings)
    sla_modal_open = False
    if request.method == "POST" and request.POST.get("form") == "renewal_sla":
        sla_form = RenewalSLASettingsForm(request.POST, instance=sla_settings)
        if sla_form.is_valid():
            sla_form.save()
            messages.success(request, "Renewal SLA settings saved.")
            return redirect("client_renewal_list")
        sla_modal_open = True

    _, poll_logs = poll_outlook_read_receipts_for_company(company)
    for message in poll_logs:
        if "failed" in message.lower():
            messages.warning(request, message)
    invites = list(_renewal_invite_queryset(company))
    from business.zoho_crm import zoho_configured

    configured = zoho_configured()
    try:
        sync_zoho_quote_sent_status(company, invites)
    except Exception:
        logger.exception(
            "renewal SLA Zoho sync failed company_id=%s", company.pk
        )
    return render(
        request,
        "business/client_renewal_list.html",
        {
            "company": company,
            "renewal_groups": _renewal_product_groups(
                invites,
                company=company,
                zoho_connected=company.zoho_connected,
                zoho_configured=configured,
                sla_settings=sla_settings,
            ),
            "zoho_connected": company.zoho_connected,
            "zoho_configured": configured,
            "sla_form": sla_form,
            "sla_settings": sla_settings,
            "sla_modal_open": sla_modal_open,
        },
    )


@sales_module_required
def client_renewal_report(request):
    company = _company(request)
    range_key = (request.GET.get("range") or "year").strip().lower()
    if range_key not in {"30", "90", "year"}:
        range_key = "year"
    try:
        from .renewal_report_export import build_renewal_report_bytes

        content, filename = build_renewal_report_bytes(company, range_key=range_key)
    except Exception:
        logger.exception(
            "client_renewal_report failed company_id=%s", company.pk
        )
        messages.error(request, "Could not build the renewal report.")
        return redirect("client_renewal_list")
    return _pdf_response(content, filename)


@sales_module_required
def client_renewal_product_changes(request):
    company = _company(request)
    rows = company.subscription_product_renewals.order_by("-created_at", "-id")
    return JsonResponse(
        {
            "ok": True,
            "items": [
                {
                    "id": row.pk,
                    "client_name": row.client_name,
                    "old_product_name": row.old_product_name,
                    "new_product_name": row.new_product_name,
                    "applied": row.applied,
                    "created_at": _format_dt(row.created_at),
                }
                for row in rows
            ],
        }
    )


@sales_module_required
def client_renewal_live(request):
    """Poll Outlook and return current renewal invite statuses as JSON."""
    company = _company(request)
    from business.renewal_read_tracking import poll_outlook_read_receipts_for_company

    try:
        updated, poll_logs = poll_outlook_read_receipts_for_company(company)
        invites = list(_renewal_invite_queryset(company))
        from business.renewal_sla import (
            get_or_create_renewal_sla_settings,
            sync_zoho_quote_sent_status,
        )
        from business.zoho_crm import zoho_configured

        sla_settings = get_or_create_renewal_sla_settings(company)
        try:
            sync_zoho_quote_sent_status(company, invites)
        except Exception:
            logger.exception(
                "renewal SLA Zoho live sync failed company_id=%s", company.pk
            )
        return JsonResponse(
            {
                "ok": True,
                "updated": updated,
                "polled_at": _format_dt(timezone.now()),
                "logs": poll_logs,
                "groups": _renewal_product_groups(
                    invites,
                    company=company,
                    zoho_connected=company.zoho_connected,
                    zoho_configured=zoho_configured(),
                    sla_settings=sla_settings,
                ),
            }
        )
    except Exception:
        logger.exception(
            "client_renewal_live failed company_id=%s", company.pk
        )
        return _json_error("Could not refresh renewal status.")


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
            form = SubscriptionRenewForm(
                request.POST, request.FILES, company=company
            )
            if not form.is_valid():
                return JsonResponse(
                    {"ok": False, "error": _form_first_error(form)},
                    status=400,
                )
            raw_years = (request.POST.get("duration") or "1").strip()
            try:
                duration_years = int(raw_years)
            except (TypeError, ValueError):
                duration_years = 0
            if duration_years < 1 or duration_years > 30:
                return JsonResponse(
                    {
                        "ok": False,
                        "error": "Enter duration in years, from 1 to 30.",
                    },
                    status=400,
                )
            invite.apply_line_renewal(
                subscription_id,
                start_date=form.cleaned_data["start_date"],
                quantity=form.cleaned_data["quantity"],
                billing_amount=form.cleaned_data["billing_amount"],
                vendor=form.cleaned_data.get("resolved_vendor"),
                invoice_number=form.cleaned_data.get("invoice_number") or "",
                invoice_pdf=form.cleaned_data.get("invoice_pdf"),
                product=form.cleaned_data.get("resolved_product"),
                duration_years=duration_years,
            )
            extra["deal_status"] = ClientSubscription.DealStatus.WIN
        else:
            status = (request.POST.get("deal_status") or "").strip().lower()
            if status == ClientSubscription.DealStatus.WIN:
                extra["open_win_form"] = subscription_id
            elif status == ClientSubscription.DealStatus.LOST:
                invite.apply_line_deal_status(
                    subscription_id,
                    status,
                    remark=request.POST.get("lost_remark") or "",
                )
                extra["deal_status"] = status
            else:
                return JsonResponse(
                    {"ok": False, "error": "Choose Win or Lost."},
                    status=400,
                )
    except ValueError as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=400)
    except Exception:
        logger.exception(
            "client_renewal_set_deal_status failed company_id=%s invite_id=%s",
            company.pk,
            invite.pk,
        )
        return _json_error("Could not update deal status.")

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
    from business.renewal_sla import (
        get_or_create_renewal_sla_settings,
        sync_zoho_quote_sent_status,
    )
    from business.zoho_crm import zoho_configured

    sla_settings = get_or_create_renewal_sla_settings(company)
    try:
        sync_zoho_quote_sent_status(company, [invite])
        invite.refresh_from_db(
            fields=["zoho_quote_stage", "zoho_quote_sent_at", "zoho_quote_number"]
        )
    except Exception:
        logger.exception(
            "renewal detail Zoho SLA sync failed company_id=%s invite_id=%s",
            company.pk,
            invite.pk,
        )
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
            "sla": _sla_row_payload(invite, sla_settings),
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
        invite.zoho_quote_stage = "Draft"
        invite.zoho_quote_sent_at = None
        invite.quotation_error = ""
        invite.save(
            update_fields=[
                "zoho_quote_id",
                "zoho_quote_number",
                "zoho_quote_url",
                "quotation_sent_at",
                "zoho_quote_stage",
                "zoho_quote_sent_at",
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
        logger.exception(
            "Zoho quotation failed company_id=%s invite_id=%s",
            company.pk,
            invite.pk,
        )
        messages.error(
            request,
            str(exc).strip()
            or "Could not create Zoho quotation. Check the Zoho connection and try again.",
        )
    return _after_quotation_redirect(request, invite)


def _after_quotation_redirect(request, invite):
    next_url = (request.POST.get("next") or "").strip()
    if next_url == reverse("client_renewal_list"):
        return redirect("client_renewal_list")
    if next_url.startswith(reverse("dashboard")):
        return redirect(_safe_next_url(next_url, reverse("dashboard")))
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
    try:
        content = build_vendor_export_bytes(company)
    except Exception:
        logger.exception("Vendor export failed company_id=%s", company.pk)
        messages.error(request, "Could not export vendors.")
        return redirect("vendor_list")
    return _xlsx_response(content, "vendors_export.xlsx")


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

        try:
            created, updated, errs = import_vendors_from_file(
                company,
                upload.read(),
                upload.name or "",
            )
        except Exception:
            logger.exception(
                "Vendor import failed company_id=%s filename=%s",
                company.pk,
                upload.name,
            )
            messages.error(
                request,
                "Could not read the vendor file. Upload a valid .xlsx or .csv file.",
            )
            return redirect("vendor_import")
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
        form = VendorForm(request.POST, company=company)
        if form.is_valid():
            v = form.save(commit=False)
            v.company = company
            v.save()
            form.save_m2m()
            if next_url:
                return redirect(
                    _url_with_query(next_url, preselect_vendor=v.pk)
                )
            return redirect("vendor_list")
    else:
        form = VendorForm(company=company)
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
        form = VendorForm(request.POST, instance=obj, company=company)
        if form.is_valid():
            form.save()
            return redirect("vendor_list")
    else:
        form = VendorForm(instance=obj, company=company)
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
    try:
        content = build_client_export_bytes(company)
    except Exception:
        logger.exception("Client export failed company_id=%s", company.pk)
        messages.error(request, "Could not export clients.")
        return redirect("client_list")
    return _xlsx_response(content, "clients_export.xlsx")


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

        try:
            created, updated, errs = import_clients_from_file(
                company,
                upload.read(),
                upload.name or "",
            )
        except Exception:
            logger.exception(
                "Client import failed company_id=%s filename=%s",
                company.pk,
                upload.name,
            )
            messages.error(
                request,
                "Could not read the client file. Upload a valid .xlsx or .csv file.",
            )
            return redirect("client_import")
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


CLIENT_BULK_EMAIL_THROTTLE_SECONDS = 20
CLIENT_EMAIL_DEFAULT_PAGE_SIZE = 15
CLIENT_EMAIL_MIN_PAGE_SIZE = 3
CLIENT_EMAIL_MAX_PAGE_SIZE = 100


def _client_email_page_number(params) -> int:
    try:
        return max(1, int((params.get("page") or "1").strip()))
    except (TypeError, ValueError):
        return 1


def _client_email_per_page(params) -> int:
    raw = (params.get("per_page") or "").strip()
    if not raw:
        return CLIENT_EMAIL_DEFAULT_PAGE_SIZE
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return CLIENT_EMAIL_DEFAULT_PAGE_SIZE
    return max(CLIENT_EMAIL_MIN_PAGE_SIZE, min(CLIENT_EMAIL_MAX_PAGE_SIZE, n))


def _client_email_url(
    filters: dict[str, str],
    *,
    page: int = 1,
    per_page: int = CLIENT_EMAIL_DEFAULT_PAGE_SIZE,
) -> str:
    url = reverse("client_email")
    parts = {key: value for key, value in filters.items() if value}
    if page > 1:
        parts["page"] = str(page)
    if per_page != CLIENT_EMAIL_DEFAULT_PAGE_SIZE:
        parts["per_page"] = str(per_page)
    query = urlencode(parts)
    return f"{url}?{query}" if query else url


def _client_email_page_url(
    filters: dict[str, str], page: int, per_page: int = CLIENT_EMAIL_DEFAULT_PAGE_SIZE
) -> str:
    return _client_email_url(filters, page=page, per_page=per_page)


def _client_email_filter_values(params) -> dict[str, str]:
    def _get(name: str) -> str:
        return (params.get(name) or "").strip()

    return {
        "q": _get("q"),
        "company_name": _get("company_name"),
        "product": _get("product"),
    }


def _client_email_filter_querystring(filters: dict[str, str]) -> str:
    return urlencode({key: value for key, value in filters.items() if value})


def _client_email_subscription_client_ids(company, filters: dict[str, str]) -> list[int] | None:
    """Return client ids with matching subscriptions, or None if no product filters."""
    if not filters["product"] and not filters["company_name"]:
        return None
    sub_qs = company.subscriptions.all()
    if filters["product"]:
        sub_qs = sub_qs.filter(product_id=filters["product"])
    if filters["company_name"]:
        if filters["company_name"] == NO_COMPANY_VALUE:
            sub_qs = sub_qs.filter(
                Q(product__company_name="") | Q(product__company_name__isnull=True)
            )
        else:
            sub_qs = sub_qs.filter(product__company_name__iexact=filters["company_name"])
    return list(sub_qs.values_list("client_id", flat=True).distinct())


def _client_email_queryset(company, filters: dict[str, str]):
    qs = company.clients.all().order_by("name")
    client_ids = _client_email_subscription_client_ids(company, filters)
    if client_ids is not None:
        qs = qs.filter(pk__in=client_ids)
    q = filters.get("q") or ""
    if q:
        qs = qs.filter(
            Q(name__icontains=q)
            | Q(email__icontains=q)
            | Q(email_2__icontains=q)
            | Q(email_3__icontains=q)
            | Q(gst_number__icontains=q)
        )
    return qs


def _client_email_filter_options(company, filters: dict[str, str]):
    base_subs = company.subscriptions.select_related("product")
    company_names, has_blank_company = distinct_product_company_names(base_subs)
    products = company.products.all().order_by("name")
    if filters["company_name"]:
        if filters["company_name"] == NO_COMPANY_VALUE:
            products = products.filter(
                Q(company_name="") | Q(company_name__isnull=True)
            )
        else:
            products = products.filter(company_name__iexact=filters["company_name"])
    return company_names, has_blank_company, products


def _client_email_recipients(company, ids):
    if not ids:
        return []
    return list(company.clients.filter(pk__in=ids).order_by("name").distinct())


def _client_email_selected_ids(raw_ids) -> set[int]:
    selected: set[int] = set()
    for raw in raw_ids:
        try:
            selected.add(int(raw))
        except (TypeError, ValueError):
            continue
    return selected


def _render_client_email(
    request,
    company,
    *,
    form,
    filters: dict[str, str] | None = None,
    selected_ids: set[int] | None = None,
    page_number: int = 1,
    per_page: int = CLIENT_EMAIL_DEFAULT_PAGE_SIZE,
):
    from business.client_bulk_email import BULK_EMAIL_MERGE_TAGS

    filters = filters or _client_email_filter_values({})
    selected_ids = selected_ids or set()
    company_names, has_blank_company, products = _client_email_filter_options(
        company, filters
    )
    has_active_filters = any(filters.values())
    all_clients = list(_client_email_queryset(company, filters))
    all_client_ids = [c.pk for c in all_clients]
    emailable_client_ids = [c.pk for c in all_clients if c.contact_emails()]
    emailable_count = len(emailable_client_ids)
    paginator = Paginator(all_clients, per_page)
    page_obj = paginator.get_page(page_number)
    pagination_pages = [
        {
            "num": num,
            "url": _client_email_page_url(filters, num, per_page),
            "current": num == page_obj.number,
        }
        for num in paginator.page_range
    ]
    prev_page_url = (
        _client_email_page_url(filters, page_obj.number - 1, per_page)
        if page_obj.number > 1
        else ""
    )
    next_page_url = (
        _client_email_page_url(filters, page_obj.number + 1, per_page)
        if page_obj.number < paginator.num_pages
        else ""
    )
    outgoing = (company.outgoing_email or "").strip()
    smtp_ready = bool(outgoing and (company.outgoing_email_app_password or "").strip())
    return render(
        request,
        "business/client_email.html",
        {
            "company": company,
            "object_list": page_obj.object_list,
            "page_obj": page_obj,
            "shown_count": paginator.count,
            "emailable_count": emailable_count,
            "filters": filters,
            "filter_pills": _client_email_filter_pills(company, filters, per_page),
            "per_page": per_page,
            "per_page_default": CLIENT_EMAIL_DEFAULT_PAGE_SIZE,
            "has_active_filters": has_active_filters,
            "selected_ids": selected_ids,
            "selected_ids_json": list(selected_ids),
            "all_client_ids_json": all_client_ids,
            "emailable_client_ids_json": emailable_client_ids,
            "form": form,
            "merge_tags": BULK_EMAIL_MERGE_TAGS,
            "company_names": company_names,
            "has_blank_company": has_blank_company,
            "no_company_value": NO_COMPANY_VALUE,
            "no_company_label": NO_COMPANY_LABEL,
            "products": products,
            "smtp_ready": smtp_ready,
            "outgoing_email": outgoing,
            "pagination_pages": pagination_pages,
            "prev_page_url": prev_page_url,
            "next_page_url": next_page_url,
        },
    )


def _client_email_redirect(
    filters: dict[str, str], *, per_page: int = CLIENT_EMAIL_DEFAULT_PAGE_SIZE
):
    return redirect(_client_email_url(filters, page=1, per_page=per_page))


def _client_email_filter_pills(
    company, filters: dict[str, str], per_page: int = CLIENT_EMAIL_DEFAULT_PAGE_SIZE
) -> list[dict[str, str]]:
    pills: list[dict[str, str]] = []

    def _pill_url(**overrides) -> str:
        merged = {**filters, **overrides}
        return _client_email_url(merged, page=1, per_page=per_page)

    if filters["company_name"]:
        if filters["company_name"] == NO_COMPANY_VALUE:
            label = NO_COMPANY_LABEL
        else:
            label = filters["company_name"]
        pills.append(
            {
                "label": label,
                "url": _pill_url(company_name=""),
            }
        )
    if filters["product"]:
        product = company.products.filter(pk=filters["product"]).first()
        label = product.name if product else f"Product #{filters['product']}"
        pills.append(
            {
                "label": label,
                "url": _pill_url(product=""),
            }
        )
    if filters["q"]:
        pills.append(
            {
                "label": f"Search: {filters['q']}",
                "url": _pill_url(q=""),
            }
        )
    return pills


@sales_module_required
@require_http_methods(["POST"])
def client_email_preview(request):
    company = _company(request)
    try:
        client_pk = int(request.POST.get("client_id") or "")
    except (TypeError, ValueError):
        return _json_error("Choose a client to preview.", status=400)
    client = company.clients.filter(pk=client_pk).first()
    if client is None:
        return _json_error("Client not found.", status=404)
    subject = (request.POST.get("subject") or "").strip()
    body = request.POST.get("body") or ""
    from business.client_bulk_email import preview_client_email

    data = preview_client_email(company, client, subject, body)
    if not data["to_emails"]:
        return _json_error(
            f"{data['client_name'] or 'This client'} has no email address on file.",
            status=400,
        )
    return JsonResponse({"ok": True, **data})


@sales_module_required
@require_http_methods(["GET", "POST"])
def client_email(request):
    company = _company(request)
    params = request.GET if request.method == "GET" else request.POST
    filters = _client_email_filter_values(params)
    page_number = _client_email_page_number(params)
    per_page = _client_email_per_page(params)

    if request.method == "GET":
        return _render_client_email(
            request,
            company,
            form=ClientBulkEmailForm(),
            filters=filters,
            page_number=page_number,
            per_page=per_page,
        )

    ids = request.POST.getlist("client_ids")
    selected_ids = _client_email_selected_ids(ids)
    clients = _client_email_recipients(company, ids)
    form = ClientBulkEmailForm(request.POST)
    page_number = _client_email_page_number(request.POST)

    if not clients:
        messages.warning(request, "Select at least one client to email.")
        return _render_client_email(
            request,
            company,
            form=form,
            filters=filters,
            selected_ids=selected_ids,
            page_number=page_number,
            per_page=per_page,
        )

    if not form.is_valid():
        return _render_client_email(
            request,
            company,
            form=form,
            filters=filters,
            selected_ids=selected_ids,
            page_number=page_number,
            per_page=per_page,
        )

    from business.client_bulk_email import collect_attachments, enqueue_client_bulk_emails

    try:
        attachments = collect_attachments(request.FILES.getlist("attachments"))
    except ValidationError as exc:
        messages.error(request, str(exc))
        return _render_client_email(
            request,
            company,
            form=form,
            filters=filters,
            selected_ids=selected_ids,
            page_number=page_number,
            per_page=per_page,
        )

    throttle_key = f"client-bulk-email:{company.pk}"
    if not cache.add(throttle_key, "1", CLIENT_BULK_EMAIL_THROTTLE_SECONDS):
        messages.info(
            request,
            "That send was already submitted a moment ago. "
            f"Wait {CLIENT_BULK_EMAIL_THROTTLE_SECONDS} seconds before sending again.",
        )
        return _render_client_email(
            request,
            company,
            form=form,
            filters=filters,
            selected_ids=selected_ids,
            page_number=page_number,
            per_page=per_page,
        )

    try:
        enqueue_client_bulk_emails(
            company,
            clients,
            form.cleaned_data["subject"],
            form.cleaned_data["body"],
            attachments=attachments,
        )
    except Exception:
        logger.exception(
            "Client bulk email queue failed company_id=%s client_count=%s",
            company.pk,
            len(clients),
        )
        cache.delete(throttle_key)
        messages.error(
            request,
            "Could not queue bulk email. Ensure the background worker is running and try again.",
        )
        return _render_client_email(
            request,
            company,
            form=form,
            filters=filters,
            selected_ids=selected_ids,
            page_number=page_number,
            per_page=per_page,
        )

    n = len(clients)
    messages.success(
        request,
        f"Sending email to {n} client(s) in the background. "
        "Clients without an email address are skipped automatically.",
    )
    return _client_email_redirect(filters, per_page=per_page)


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
    try:
        content = build_product_export_bytes(company)
    except Exception:
        logger.exception("Product export failed company_id=%s", company.pk)
        messages.error(request, "Could not export products.")
        return redirect("product_list")
    return _xlsx_response(content, "products_export.xlsx")


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

        try:
            created, updated, errs = import_products_bulk_from_file(
                company,
                upload.read(),
                upload.name or "",
            )
        except Exception:
            logger.exception(
                "Product bulk import failed company_id=%s filename=%s",
                company.pk,
                upload.name,
            )
            messages.error(
                request,
                "Could not read the product file. Upload a valid .xlsx or .csv file.",
            )
            return redirect("product_import")
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
        try:
            _stash_product_import(request, upload.read(), upload.name or "")
        except OSError:
            logger.exception(
                "Product import stash failed user_id=%s", request.user.pk
            )
            messages.error(request, "Could not save the uploaded file. Try again.")
            return redirect("product_import_advanced")
        except Exception:
            logger.exception(
                "Product import parse failed company_id=%s filename=%s",
                company.pk,
                upload.name,
            )
            messages.error(
                request,
                "Could not read the product file. Upload a valid .xlsx or .csv file.",
            )
            return redirect("product_import_advanced")
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


def _subscription_filter_values(params) -> dict[str, str]:
    def _get(name: str) -> str:
        return (params.get(name) or "").strip()

    return {
        "client": _get("client"),
        "product": _get("product"),
        "vendor": _get("vendor"),
        "category": _get("category"),
        "company_name": _get("company_name"),
        "start_date": _get("start_date"),
        "end_date": _get("end_date"),
        "expiring_soon": _get("expiring_soon"),
    }


def _filtered_subscription_qs(company, params, *, apply_company_name: bool = True):
    qs = company.subscriptions.select_related("client", "product", "vendor")
    filters = _subscription_filter_values(params)
    if filters["client"]:
        qs = qs.filter(client_id=filters["client"])
    if filters["product"]:
        qs = qs.filter(product_id=filters["product"])
    if filters["vendor"]:
        qs = qs.filter(vendor_id=filters["vendor"])
    if filters["category"]:
        qs = qs.filter(product__category__iexact=filters["category"])
    if apply_company_name and filters["company_name"]:
        if filters["company_name"] == NO_COMPANY_VALUE:
            qs = qs.filter(
                Q(product__company_name="") | Q(product__company_name__isnull=True)
            )
        else:
            qs = qs.filter(product__company_name__iexact=filters["company_name"])
    if filters["start_date"]:
        qs = qs.filter(start_date__gte=filters["start_date"])
    if filters["end_date"]:
        qs = qs.filter(end_date__lte=filters["end_date"])
    if filters["expiring_soon"]:
        days = _parse_expiring_days(filters["expiring_soon"], default=0)
        if days:
            qs = _filter_expiring_soon(qs, days)
    return qs, filters


def _subscription_filter_querystring(params) -> str:
    filters = _subscription_filter_values(params)
    return urlencode({key: value for key, value in filters.items() if value})


@sales_module_required
def subscription_list(request):
    company = _company(request)
    base_qs = company.subscriptions.select_related("client", "product", "vendor")
    total_count = base_qs.count()
    qs, filters = _filtered_subscription_qs(company, request.GET)
    company_names, has_blank_company = distinct_product_company_names(base_qs)

    categories = (
        company.products.exclude(category="")
        .values_list("category", flat=True)
        .distinct()
        .order_by("category")
    )

    has_active_filters = any(filters.values())
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
            "company_names": company_names,
            "has_blank_company": has_blank_company,
            "no_company_value": NO_COMPANY_VALUE,
            "no_company_label": NO_COMPANY_LABEL,
            "export_columns": EXPORT_COLUMNS,
            "default_export_columns": list(DEFAULT_EXPORT_COLUMNS),
            "filters": filters,
            "expiring_choices": _expiring_subscription_choices(),
        },
    )


@sales_module_required
@require_http_methods(["POST"])
def subscription_export(request):
    company = _company(request)
    list_url = reverse("subscription_list")
    query = _subscription_filter_querystring(request.POST)
    if query:
        list_url = f"{list_url}?{query}"

    column_keys = normalize_column_keys(request.POST.getlist("columns"))
    if not column_keys:
        messages.error(request, "Select at least one column to export.")
        return redirect(list_url)

    companies = [
        (value or "").strip()
        for value in request.POST.getlist("companies")
        if (value or "").strip()
    ]
    if not companies:
        messages.error(request, "Select at least one company to export.")
        return redirect(list_url)

    qs, _filters = _filtered_subscription_qs(
        company, request.POST, apply_company_name=False
    )
    qs = apply_company_selection(qs, companies).order_by(
        "client__name", "product__name", "pk"
    )
    try:
        content = build_subscription_export_bytes(qs, column_keys)
    except ValueError as exc:
        messages.error(request, str(exc))
        return redirect(list_url)
    except Exception:
        logger.exception("Subscription export failed company_id=%s", company.pk)
        messages.error(request, "Could not export subscriptions.")
        return redirect(list_url)
    return _xlsx_response(content, "subscriptions_export.xlsx")


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
        try:
            _stash_import_upload(
                request,
                upload.read(),
                upload.name or "",
                vendor.pk,
                vendor.name,
            )
        except OSError:
            logger.exception(
                "Subscription import stash failed user_id=%s", request.user.pk
            )
            messages.error(request, "Could not save the uploaded file. Try again.")
            return redirect("subscription_import")
        except Exception:
            logger.exception(
                "Subscription import parse failed company_id=%s filename=%s",
                company.pk,
                upload.name,
            )
            messages.error(
                request,
                "Could not read the subscription file. Upload a valid .xlsx or .csv file.",
            )
            return redirect("subscription_import")
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
            request.FILES,
            company=company,
            asset_type_prefilter=asset_type or None,
        )
        if form.is_valid():
            try:
                s = form.save(commit=False)
                s.company = company
                s.save()
            except (OSError, SuspiciousFileOperation):
                logger.exception(
                    "Subscription add save failed company_id=%s", company.pk
                )
                messages.error(
                    request,
                    "Could not save the subscription or invoice PDF. Try another file.",
                )
            else:
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
        ClientSubscription.objects.select_related(
            "client", "product", "vendor", "renewal_draft_product"
        ),
        pk=pk,
        company=company,
    )
    default_next = reverse("dashboard")
    next_url = _safe_next_url(
        request.POST.get("next") or request.GET.get("next"),
        default_next,
    )
    require_submit = (
        request.POST.get("required") or request.GET.get("required") or ""
    ).strip() == "1"
    suggested_start, suggested_end = obj.renewal_dates()
    initial = obj.renewal_form_initial()

    if request.method == "POST":
        if request.POST.get("vendor") == SubscriptionRenewForm.NEW_VENDOR:
            renew_return = reverse("subscription_renew", kwargs={"pk": pk})
            posted_next = (request.POST.get("next") or "").strip()
            extra = {}
            if posted_next:
                extra["next"] = posted_next
            if require_submit:
                extra["required"] = "1"
            if extra:
                renew_return = _url_with_query(renew_return, **extra)
            return redirect(
                f"{reverse('vendor_add')}?{urlencode({'next': renew_return})}"
            )
        form = SubscriptionRenewForm(request.POST, request.FILES, company=company)
        if form.is_valid():
            new_start = form.cleaned_data["start_date"]
            new_end = form.cleaned_data["end_date"]
            new_qty = form.cleaned_data["quantity"]
            new_price = form.cleaned_data["billing_amount"]
            new_vendor = form.cleaned_data["resolved_vendor"]
            new_product = form.cleaned_data.get("resolved_product")
            if not form.cleaned_data.get("renew_other_plan"):
                obj.renewal_draft_product = None
                new_product = None
            try:
                obj.apply_saved_renewal(
                    start_date=new_start,
                    end_date=new_end,
                    quantity=new_qty,
                    billing_amount=new_price,
                    vendor=new_vendor,
                    invoice_number=form.cleaned_data.get("invoice_number") or "",
                    invoice_pdf=form.cleaned_data.get("invoice_pdf"),
                    product=new_product,
                )
            except (OSError, SuspiciousFileOperation):
                logger.exception(
                    "Subscription renew save failed company_id=%s subscription_id=%s",
                    company.pk,
                    obj.pk,
                )
                messages.error(
                    request,
                    "Could not save the renewal or invoice PDF. Try another file.",
                )
            else:
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
                "invoice_number": initial.get("invoice_number") or "",
                "product": initial.get("product") or "",
                "renew_other_plan": initial.get("renew_other_plan") or False,
                "duration": 1,
            },
        )

    form.fields["quantity"].label = "Renewal quantity"
    form.fields["end_date"].widget.attrs["readonly"] = "readonly"

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
            "require_submit": require_submit,
            "draft_product": (
                obj.renewal_draft_product if obj.win_form_completed else None
            ),
        },
    )


@sales_module_required
def subscription_edit(request, pk):
    company = _company(request)
    obj = get_object_or_404(ClientSubscription, pk=pk, company=company)
    if request.method == "POST":
        form = ClientSubscriptionForm(
            request.POST, request.FILES, instance=obj, company=company
        )
        if form.is_valid():
            try:
                form.save()
            except (OSError, SuspiciousFileOperation):
                logger.exception(
                    "Subscription edit save failed company_id=%s subscription_id=%s",
                    company.pk,
                    obj.pk,
                )
                messages.error(
                    request,
                    "Could not save the subscription or invoice PDF. Try another file.",
                )
            else:
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


def _support_ticket_list_data(
    company, *, status_filter: str = "", source: str = "internal"
):
    """Shared ticket list payload for HTML page and live JSON poll."""
    from business.sla import get_or_create_sla_settings

    status_filter = (status_filter or "").strip()
    source = "oem" if source == "oem" else "internal"
    sla_settings = get_or_create_sla_settings(company)

    internal_wrapped = helpdesk_bridge.wrap_tickets(
        helpdesk_bridge.tickets_for_company(company, oem=False),
        sla_settings=sla_settings,
    )
    oem_wrapped = helpdesk_bridge.wrap_tickets(
        helpdesk_bridge.tickets_for_company(company, oem=True),
        sla_settings=sla_settings,
    )
    base = oem_wrapped if source == "oem" else internal_wrapped

    if status_filter in dict(helpdesk_bridge.STATUS_CHOICES):
        qs = [t for t in base if t.status == status_filter]
    else:
        status_filter = ""
        qs = base

    counts = {
        "total": len(internal_wrapped),
        "closed": sum(1 for t in internal_wrapped if t.status == "closed"),
        "pending": sum(1 for t in internal_wrapped if t.status in ("open", "in_progress")),
        "under_observation": sum(1 for t in internal_wrapped if t.status == "resolved"),
        "sla_breached": sum(
            1 for t in internal_wrapped if t.sla and t.sla.get("any_breached")
        ),
    }
    oem_counts = {
        "total": len(oem_wrapped),
        "active": sum(1 for t in oem_wrapped if t.status != "closed"),
        "open": sum(1 for t in oem_wrapped if t.status in ("open", "in_progress")),
        "closed": sum(1 for t in oem_wrapped if t.status == "closed"),
    }
    return {
        "object_list": qs,
        "status_filter": status_filter,
        "source": source,
        "counts": counts,
        "oem_counts": oem_counts,
        "assignees": assignable_users(
            company, include_ids={t.assignee_id for t in base}
        ),
        "auto_assign_enabled": get_support_team(company).auto_assign_enabled,
        "sla_settings": sla_settings,
        "charts": ticket_charts_from_views(internal_wrapped),
    }


def _ticket_list_source(request) -> str:
    return "oem" if request.GET.get("source") == "oem" else "internal"


def _validation_message(exc) -> str:
    messages_list = getattr(exc, "messages", None)
    if messages_list:
        return "; ".join(str(m) for m in messages_list)
    return str(exc)


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
        source=_ticket_list_source(request),
    )

    return render(
        request,
        "business/support_ticket_list.html",
        {
            "company": company,
            "object_list": data["object_list"],
            "status_filter": data["status_filter"],
            "source": data["source"],
            "status_choices": helpdesk_bridge.STATUS_CHOICES,
            "counts": data["counts"],
            "oem_counts": data["oem_counts"],
            "assignees": data["assignees"],
            "auto_assign_enabled": data["auto_assign_enabled"],
            "sla_settings": data["sla_settings"],
            "charts": data["charts"],
            **team_picker_context(company),
        },
    )


@support_ticket_access_required
def support_ticket_live(request):
    """Poll mailbox (throttled) and return current ticket list as JSON."""
    company = _company(request)
    try:
        helpdesk_bridge.sync_company_queue(company)

        from business.helpdesk_inbox import inbox_error_logs, maybe_fetch_on_dashboard

        fetch_result = maybe_fetch_on_dashboard(company) or {}
        created = int(fetch_result.get("created") or 0)
        inbox_errors = inbox_error_logs(fetch_result)

        data = _support_ticket_list_data(
            company,
            status_filter=request.GET.get("status", ""),
            source=_ticket_list_source(request),
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
        source = data["source"]
        for t in data["object_list"]:
            email = t.contact_display
            done = t.status in ("resolved", "closed")
            sla = t.sla or {}
            row = {
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
                "is_oem": bool(t.is_oem),
                "oem_company": t.oem_company_name or "",
                "oem_email": t.oem_to_email or "",
                "subscription_id": t.subscription_id or "",
                "transferred_to_id": t.transferred_to_id or "",
                "transferred_to_reference": t.transferred_to_reference or "",
                "transferred_to_url": (
                    reverse("support_ticket_detail", kwargs={"pk": t.transferred_to_id})
                    if t.transferred_to_id
                    else ""
                ),
                "transfer_url": "",
            }
            if source != "oem" and not t.is_oem and not t.transferred_to_id:
                row["transfer_url"] = reverse(
                    "support_ticket_transfer_oem", kwargs={"pk": t.pk}
                )
            rows.append(row)

        return JsonResponse(
            {
                "ok": True,
                "created": created,
                "inbox_errors": inbox_errors,
                "polled_at": _format_dt(timezone.now()),
                "status_filter": data["status_filter"],
                "source": source,
                "next_qs": next_qs,
                "counts": data["counts"],
                "oem_counts": data["oem_counts"],
                "status_choices": status_choices,
                "assignees": assignees,
                "sla_mode": data["sla_settings"].sla_mode,
                "rows": rows,
                "charts": data["charts"],
            }
        )
    except Exception:
        logger.exception(
            "support_ticket_live failed company_id=%s", company.pk
        )
        return _json_error("Could not refresh tickets.")


@support_ticket_access_required
@require_http_methods(["POST"])
def support_ticket_update(request, pk):
    company = _company(request)
    try:
        ticket = helpdesk_bridge.get_company_ticket(company, pk)
    except helpdesk_bridge.Ticket.DoesNotExist:
        from django.http import Http404

        raise Http404 from None

    assignees = assignable_users(company, include_ids={ticket.assigned_to_id})
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
                try:
                    from business.team_notifications import notify_ticket_assigned

                    notify_ticket_assigned(ticket, assignee, assigned_by=request.user)
                except Exception:
                    logger.exception(
                        "Team notification failed for assigned ticket_id=%s", ticket.pk
                    )
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


def _oem_compose_initial(ticket=None) -> dict:
    if ticket is None:
        return {
            "mode": "quick",
            "to_email": "",
            "cc": "",
            "oem_company": "",
            "subject": "",
            "description": "",
            "subscription_id": "",
        }
    wrapped = helpdesk_bridge.wrap_ticket(ticket)
    return {
        "mode": "transfer",
        "to_email": "",
        "cc": "",
        "oem_company": "",
        "subject": wrapped.subject or "",
        "description": wrapped.description or "",
        "subscription_id": "",
    }


@support_ticket_access_required
@require_http_methods(["GET", "POST"])
def support_oem_ticket_add(request):
    from django.core.exceptions import ValidationError

    from business.oem_tickets import create_oem_ticket_and_send

    company = _company(request)
    helpdesk_bridge.sync_company_queue(company)
    mode = (request.POST.get("mode") or request.GET.get("mode") or "quick").strip()
    if mode not in ("quick", "standard"):
        mode = "quick"
    form_data = _oem_compose_initial()
    form_data["mode"] = mode
    if request.method == "POST":
        form_data.update(
            {
                "mode": mode,
                "to_email": request.POST.get("to_email", "").strip(),
                "cc": request.POST.get("cc", "").strip(),
                "oem_company": request.POST.get("oem_company", "").strip(),
                "subject": request.POST.get("subject", "").strip(),
                "description": request.POST.get("description", "").strip(),
                "subscription_id": request.POST.get("subscription_id", "").strip(),
            }
        )
        try:
            ticket = create_oem_ticket_and_send(
                company=company,
                user=request.user,
                subject=form_data["subject"],
                description=form_data["description"],
                to_email=form_data["to_email"],
                oem_company_name=form_data["oem_company"],
                cc_raw=form_data["cc"],
                subscription_id=form_data["subscription_id"],
                attachments=request.FILES.getlist("attachments"),
            )
            messages.success(
                request,
                f"OEM ticket TK-{ticket.pk:05d} created and email sent.",
            )
            return redirect("support_ticket_detail", pk=ticket.pk)
        except ValidationError as exc:
            messages.error(request, _validation_message(exc))
        except Exception:
            logger.exception(
                "OEM ticket create/send failed company_id=%s", company.pk
            )
            messages.error(
                request,
                "Could not create or send the OEM ticket. Check OEM email settings.",
            )
    return render(
        request,
        "business/support_oem_form.html",
        {
            "company": company,
            "mode": mode,
            "form_data": form_data,
            "transfer_ticket": None,
            "oem_from": (company.oem_email or "").strip(),
        },
    )


@support_ticket_access_required
@require_http_methods(["GET", "POST"])
def support_ticket_transfer_oem(request, pk):
    from django.core.exceptions import ValidationError

    from business.oem_tickets import create_oem_ticket_and_send

    company = _company(request)
    try:
        raw = helpdesk_bridge.get_company_ticket(company, pk)
    except helpdesk_bridge.Ticket.DoesNotExist:
        from django.http import Http404

        raise Http404 from None

    ticket = helpdesk_bridge.wrap_ticket(raw)
    if ticket.is_oem:
        messages.error(request, "OEM tickets cannot be transferred again.")
        return redirect("support_ticket_detail", pk=raw.pk)
    if ticket.transferred_to_id:
        messages.info(
            request,
            f"Already transferred to {ticket.transferred_to_reference}.",
        )
        return redirect("support_ticket_detail", pk=raw.pk)

    form_data = _oem_compose_initial(raw)
    if request.method == "POST":
        form_data.update(
            {
                "to_email": request.POST.get("to_email", "").strip(),
                "cc": request.POST.get("cc", "").strip(),
                "oem_company": request.POST.get("oem_company", "").strip(),
                "subject": request.POST.get("subject", "").strip(),
                "description": request.POST.get("description", "").strip(),
                "subscription_id": request.POST.get("subscription_id", "").strip(),
            }
        )
        try:
            oem_ticket = create_oem_ticket_and_send(
                company=company,
                user=request.user,
                subject=form_data["subject"],
                description=form_data["description"],
                to_email=form_data["to_email"],
                oem_company_name=form_data["oem_company"],
                cc_raw=form_data["cc"],
                subscription_id=form_data["subscription_id"],
                attachments=request.FILES.getlist("attachments"),
                transferred_from=raw,
                assignee=raw.assigned_to,
            )
            messages.success(
                request,
                f"Transferred to OEM TK-{oem_ticket.pk:05d}. Email sent.",
            )
            return redirect("support_ticket_detail", pk=oem_ticket.pk)
        except ValidationError as exc:
            messages.error(request, _validation_message(exc))
        except Exception:
            logger.exception(
                "OEM transfer failed company_id=%s ticket_id=%s",
                company.pk,
                raw.pk,
            )
            messages.error(
                request,
                "Could not transfer this ticket to OEM. Check OEM email settings.",
            )
    return render(
        request,
        "business/support_oem_form.html",
        {
            "company": company,
            "mode": "transfer",
            "form_data": form_data,
            "transfer_ticket": ticket,
            "oem_from": (company.oem_email or "").strip(),
        },
    )


@support_ticket_access_required
@require_http_methods(["GET", "POST"])
def support_oem_diary(request):
    from django.core.exceptions import ValidationError

    from business.oem_tickets import add_diary_company, add_diary_email, diary_rows

    company = _company(request)
    if request.method == "GET":
        try:
            return JsonResponse({"ok": True, "rows": diary_rows(company)})
        except Exception:
            logger.exception(
                "OEM diary load failed company_id=%s", company.pk
            )
            return _json_error("Could not load email diary.")

    action = (request.POST.get("action") or "").strip()
    name = request.POST.get("name", "").strip()
    email = request.POST.get("email", "").strip()
    try:
        if action == "add_company":
            add_diary_company(company, name, email)
        elif action == "add_email":
            add_diary_email(company, name, email)
        else:
            return JsonResponse({"ok": False, "error": "Unknown action."}, status=400)
    except ValidationError as exc:
        return JsonResponse(
            {"ok": False, "error": _validation_message(exc)}, status=400
        )
    except Exception:
        logger.exception(
            "OEM diary action failed company_id=%s action=%s",
            company.pk,
            action,
        )
        return _json_error("Could not update email diary.")
    return JsonResponse({"ok": True, "rows": diary_rows(company)})


@support_ticket_access_required
@require_http_methods(["GET"])
def support_oem_contacts(request):
    from business.oem_tickets import contact_options

    company = _company(request)
    try:
        return JsonResponse({"ok": True, "contacts": contact_options(company)})
    except Exception:
        logger.exception(
            "OEM contacts lookup failed company_id=%s", company.pk
        )
        return _json_error("Could not load OEM contacts.")


@support_ticket_access_required
@require_http_methods(["GET"])
def support_oem_subscription_lookup(request):
    from business.oem_tickets import lookup_subscription, subscription_options

    company = _company(request)
    query = (request.GET.get("q") or request.GET.get("subscription_id") or "").strip()
    try:
        if not query:
            return JsonResponse(
                {"ok": True, "subscriptions": subscription_options(company)}
            )
        found = lookup_subscription(company, query)
        if not found:
            return JsonResponse({"ok": False, "error": "No subscription found."})
        return JsonResponse({"ok": True, "subscription": found})
    except Exception:
        logger.exception(
            "OEM subscription lookup failed company_id=%s query=%s",
            company.pk,
            query,
        )
        return _json_error("Could not look up the subscription.")



@support_ticket_access_required
@require_http_methods(["POST"])
def support_ticket_summary_email(request, pk):
    """Email this ticket's public conversation to an address the staff member types."""
    from django.core.exceptions import ValidationError
    from django.core.validators import validate_email

    from business.ticket_summary_mail import send_ticket_conversation_summary

    company = _company(request)
    try:
        raw = helpdesk_bridge.get_company_ticket(company, pk)
    except helpdesk_bridge.Ticket.DoesNotExist:
        return _json_error("Ticket not found.", status=404)

    to_email = (request.POST.get("to") or "").strip()
    try:
        validate_email(to_email)
    except ValidationError:
        return _json_error("Enter a valid email address.", status=400)

    status, err = send_ticket_conversation_summary(
        company=company,
        ticket=raw,
        to_email=to_email,
    )
    if status == "sent":
        return JsonResponse(
            {"ok": True, "message": f"Conversation sent to {to_email}."}
        )
    return _json_error(err or "Could not send the email.", status=400)


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
@require_http_methods(["GET"])
def support_ticket_attachment(request, pk, attachment_id):
    """Serve a file that arrived on the customer email, for this company's ticket."""
    from helpdesk.models import FollowUpAttachment

    company = _company(request)
    try:
        raw = helpdesk_bridge.get_company_ticket(company, pk)
    except helpdesk_bridge.Ticket.DoesNotExist:
        raise Http404 from None
    attachment = (
        FollowUpAttachment.objects.filter(pk=attachment_id, followup__ticket=raw)
        .only("file", "filename", "mime_type")
        .first()
    )
    if attachment is None or not attachment.file:
        raise Http404
    mime = (attachment.mime_type or "application/octet-stream").split(";")[0].strip()
    is_image = mime.startswith("image/") and "svg" not in mime
    if not is_image and not mime.startswith(
        ("application/pdf", "text/plain", "application/octet-stream")
    ):
        # Staff download other office files; never let the browser sniff HTML.
        mime = "application/octet-stream"
    name = os.path.basename(attachment.filename or "attachment") or "attachment"
    return FileResponse(
        attachment.file.open("rb"),
        content_type=mime if is_image or mime == "application/pdf" else "application/octet-stream",
        as_attachment=not is_image,
        filename=name,
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


@support_ticket_access_required
@require_http_methods(["GET", "POST"])
def support_team_settings(request):
    """Saves the support team modal; GET opens the ticket list with the modal shown."""
    from business import support_team

    url = reverse("support_ticket_list")
    if request.method == "GET":
        return redirect(f"{url}?team=1")

    company = _company(request)
    support_team.save_team(
        company,
        member_ids=request.POST.getlist("members"),
        auto_assign_enabled=request.POST.get("auto_assign_enabled") == "on",
    )
    messages.success(request, "Support team saved.")
    qs = QueryDict(request.POST.get("next_qs", "").strip(), mutable=True)
    qs.pop("team", None)
    if qs:
        url = f"{url}?{qs.urlencode()}"
    return redirect(url)


@support_ticket_access_required
@require_http_methods(["POST"])
def support_team_auto_assign_toggle(request):
    from business import support_team

    company = _company(request)
    enabled = request.POST.get("auto_assign_enabled") == "on"
    support_team.set_auto_assign(company, enabled)
    if enabled and not support_team.team_members(company).exists():
        text = (
            "Auto-assign is on, but the support team is empty. "
            "Add engineers so tickets can be assigned."
        )
        level = "warning"
    else:
        text = (
            "Auto-assign turned on. New tickets will go to a support engineer."
            if enabled
            else "Auto-assign turned off. Assign new tickets manually."
        )
        level = "success"
    if request.headers.get("x-requested-with") == "XMLHttpRequest":
        return JsonResponse(
            {"ok": True, "enabled": enabled, "message": text, "level": level}
        )
    if level == "warning":
        messages.warning(request, text)
    else:
        messages.success(request, text)
    url = reverse("support_ticket_list")
    qs = request.POST.get("next_qs", "").strip()
    if qs:
        url = f"{url}?{qs}"
    return redirect(url)


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
    request, invite, lines, formset, new_product_formset=None, save_error=""
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
            "save_error": save_error,
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
    try:
        from business.team_notifications import notify_renewal_confirmed

        notify_renewal_confirmed(invite, lines, new_products=new_products)
    except Exception:
        logger.exception(
            "Team notification failed for renewal invite_id=%s", invite.pk
        )
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
            try:
                saved_new = _save_renewal_confirmation(
                    invite,
                    lines,
                    formset,
                    submitter_email,
                    new_product_formset=new_product_formset,
                )
            except Exception:
                logger.exception(
                    "Renewal confirm save failed invite_id=%s", invite.pk
                )
                return _renewal_quantity_page(
                    request,
                    invite,
                    lines,
                    formset,
                    new_product_formset=new_product_formset,
                    save_error="Could not save this confirmation. Try again.",
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
            try:
                sub.save()
            except Exception:
                logger.exception(
                    "Case lock public save failed invite_id=%s", invite.pk
                )
                form.add_error(
                    None,
                    "Could not save this form. Try again.",
                )
            else:
                try:
                    from business.team_notifications import notify_caselock_submitted

                    notify_caselock_submitted(sub)
                except Exception:
                    logger.exception(
                        "Team notification failed for case lock submission_id=%s",
                        sub.pk,
                    )
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


@require_http_methods(["GET", "POST"])
def lead_public_enquiry(request, token):
    from business.lead_ingest import create_lead_from_whatsapp
    from business.lead_public_tokens import unsign_lead_public_token

    parsed = unsign_lead_public_token(token)
    if parsed is None:
        return render(
            request,
            "business/lead_public_invalid.html",
            {"public_page": True},
            status=400,
        )

    company = None
    company_id = parsed.get("company_id")
    if company_id is not None:
        company = CompanyProfile.objects.filter(pk=company_id).first()
        if company is None:
            return render(
                request,
                "business/lead_public_invalid.html",
                {"public_page": True},
                status=404,
            )

    wa_id = parsed.get("whatsapp_wa_id") or ""
    initial = {
        "contact_name": parsed.get("contact_name") or "",
        "phone": wa_id,
    }

    if request.method == "POST":
        form = LeadPublicForm(request.POST, company=company)
        if form.is_valid():
            try:
                create_lead_from_whatsapp(
                    company=company,
                    contact_name=form.cleaned_data["contact_name"],
                    company_name=form.cleaned_data.get("company_name") or "",
                    email=form.cleaned_data.get("email") or "",
                    phone=form.cleaned_data.get("phone") or wa_id,
                    requirement=form.cleaned_data.get("requirement") or "",
                    product_ids=form.product_ids(),
                    whatsapp_wa_id=wa_id,
                    raw_payload={"public_form": True},
                )
            except ValueError as exc:
                form.add_error(None, str(exc))
            except Exception:
                logger.exception("Public lead enquiry save failed company_id=%s", company_id)
                form.add_error(None, "Could not save your enquiry. Please try again.")
            else:
                return render(
                    request,
                    "business/lead_public_thanks.html",
                    {"company": company, "public_page": True},
                )
    else:
        form = LeadPublicForm(initial=initial, company=company)

    return render(
        request,
        "business/lead_public_form.html",
        {
            "form": form,
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
    try:
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
    except Exception:
        logger.exception(
            "caselock_inbox_live failed company_id=%s", company.pk
        )
        return _json_error("Could not refresh Case lock inbox.")


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


def _leads_visible_to_company(company):
    """Company leads plus WhatsApp/email ingest with no company assigned."""
    return Lead.objects.filter(Q(company=company) | Q(company__isnull=True))


@sales_module_required
def lead_list(request):
    company = _company(request)
    q = request.GET.get("q", "").strip()
    source = request.GET.get("source", "").strip().lower()
    status = request.GET.get("status", "").strip().lower()
    qs = (
        _leads_visible_to_company(company)
        .select_related("created_by")
        .prefetch_related("products")
        .order_by("-created_at", "-id")
    )
    if source in Lead.Source.values:
        qs = qs.filter(source=source)
    if status in Lead.Status.values:
        qs = qs.filter(status=status)
    if q:
        qs = qs.filter(
            Q(contact_name__icontains=q)
            | Q(company_name__icontains=q)
            | Q(email__icontains=q)
            | Q(phone__icontains=q)
            | Q(requirement__icontains=q)
            | Q(whatsapp_wa_id__icontains=q)
        )
    return render(
        request,
        "business/lead_list.html",
        {
            "company": company,
            "object_list": qs,
            "q": q,
            "source": source,
            "status": status,
            "source_choices": Lead.Source.choices,
            "status_choices": Lead.Status.choices,
        },
    )


@sales_module_required
def lead_detail(request, pk):
    company = _company(request)
    lead = get_object_or_404(
        Lead.objects.select_related("created_by").prefetch_related("products"),
        pk=pk,
    )
    if lead.company_id is not None and lead.company_id != company.pk:
        raise Http404
    status_form = LeadStatusForm(initial={"status": lead.status})
    if request.method == "POST":
        status_form = LeadStatusForm(request.POST)
        if status_form.is_valid():
            lead.status = status_form.cleaned_data["status"]
            lead.save(update_fields=["status", "updated_at"])
            messages.success(request, "Lead status updated.")
            return redirect("lead_detail", pk=lead.pk)
    return render(
        request,
        "business/lead_detail.html",
        {
            "company": company,
            "lead": lead,
            "status_form": status_form,
            "products": lead.products.all().order_by("name"),
        },
    )


@sales_module_required
def lead_add(request):
    company = _company(request)
    if request.method == "POST":
        form = LeadForm(request.POST, company=company)
        if form.is_valid():
            lead = form.save(commit=False)
            lead.company = company
            lead.created_by = request.user
            if not lead.source:
                lead.source = Lead.Source.MANUAL
            lead.save()
            form.save_m2m()
            try:
                from business.team_notifications import notify_new_lead

                notify_new_lead(lead)
            except Exception:
                logger.exception("Team notification failed for lead_id=%s", lead.pk)
            messages.success(request, "Lead created.")
            return redirect("lead_detail", pk=lead.pk)
    else:
        form = LeadForm(
            company=company,
            initial={"source": Lead.Source.MANUAL, "status": Lead.Status.NEW},
        )
    return render(
        request,
        "business/lead_form.html",
        {"company": company, "form": form, "title": "Add lead"},
    )


def _format_bytes(n: int) -> str:
    try:
        n = int(n or 0)
    except (TypeError, ValueError):
        return "—"
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(n)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"


@backup_access_required
def backup_list(request):
    from django.conf import settings

    from business.backup import backup_root, backups_enabled, retention_days
    from business.restore import backups_for_ui

    company = get_company_for_user(request.user)
    return render(
        request,
        "business/backup_list.html",
        {
            "company": company,
            "object_list": backups_for_ui(),
            "backup_root": str(backup_root()),
            "backup_enabled": backups_enabled(),
            "retention_days": retention_days(),
            "schedule_hour": getattr(settings, "BACKUP_SCHEDULE_HOUR", 2),
            "schedule_minute": getattr(settings, "BACKUP_SCHEDULE_MINUTE", 0),
            "restore_is_merge": company is not None,
        },
    )


@backup_access_required
@require_http_methods(["POST"])
def backup_create(request):
    from business.backup import BackupError, create_backup

    try:
        result = create_backup(prune=True)
        messages.success(
            request,
            f"Backup created: {result.stamp} "
            f"(DB {_format_bytes(result.db_bytes)}, media {_format_bytes(result.media_bytes)}).",
        )
        if result.pruned:
            messages.info(
                request,
                f"Pruned {len(result.pruned)} old backup(s).",
            )
    except BackupError as exc:
        messages.error(request, f"Backup failed: {exc}")
    return redirect("backup_list")


@backup_access_required
@require_http_methods(["POST"])
def backup_download_db_now(request):
    """Run pg_dump and stream the file to the browser as a local download."""
    from business.backup import BackupError, create_db_dump_export

    try:
        path, filename = create_db_dump_export()
    except BackupError as exc:
        messages.error(request, f"Database dump failed: {exc}")
        return redirect("backup_list")

    response = FileResponse(
        path.open("rb"),
        as_attachment=True,
        filename=filename,
        content_type="application/octet-stream",
    )
    return response


@backup_access_required
@require_http_methods(["GET"])
def backup_download(request, stamp, kind):
    from business.backup import backup_root

    stamp = (stamp or "").strip()
    kind = (kind or "").strip().lower()
    if not stamp or "/" in stamp or "\\" in stamp or ".." in stamp:
        raise Http404
    folder = backup_root() / stamp
    if not folder.is_dir():
        raise Http404

    if kind == "db":
        path = folder / "db.dump"
        download_name = f"sales_db_{stamp}.dump"
        content_type = "application/octet-stream"
    elif kind == "media":
        path = folder / "media.tar.gz"
        download_name = f"sales_media_{stamp}.tar.gz"
        content_type = "application/gzip"
    elif kind == "manifest":
        path = folder / "manifest.json"
        download_name = f"sales_manifest_{stamp}.json"
        content_type = "application/json"
    else:
        raise Http404

    if not path.is_file():
        messages.error(request, f"File not found for backup {stamp}.")
        return redirect("backup_list")

    return FileResponse(
        path.open("rb"),
        as_attachment=True,
        filename=download_name,
        content_type=content_type,
    )


@backup_access_required
@require_http_methods(["POST"])
def backup_delete(request, stamp):
    import shutil

    from business.backup import backup_root

    stamp = (stamp or "").strip()
    if not stamp or "/" in stamp or "\\" in stamp or ".." in stamp:
        messages.error(request, "Invalid backup id.")
        return redirect("backup_list")
    folder = backup_root() / stamp
    if not folder.is_dir():
        messages.error(request, "Backup not found.")
        return redirect("backup_list")
    shutil.rmtree(folder, ignore_errors=True)
    messages.success(request, f"Deleted backup {stamp}.")
    return redirect("backup_list")


@backup_access_required
@require_http_methods(["POST"])
def backup_restore(request, stamp):
    from business.backup import BackupError
    from business.restore import restore_from_stamp

    company = get_company_for_user(request.user)
    merge = company is not None
    next_name = "backup_list" if merge else "restore_page"

    try:
        result = restore_from_stamp(stamp, merge=merge)
    except BackupError as exc:
        messages.error(request, str(exc))
        return redirect(next_name)
    except Exception as exc:
        messages.error(request, f"Restore failed: {exc}")
        return redirect(next_name)

    return _complete_restore(request, result, merge=merge, next_name=next_name)


@backup_access_required
def restore_page(request):
    from django.conf import settings

    company = get_company_for_user(request.user)
    if request.method == "POST":
        return _restore_from_upload(request, company)
    return render(
        request,
        "business/restore.html",
        {
            "company": company,
            "restore_is_merge": company is not None,
            "upload_max_label": _format_bytes(
                getattr(settings, "BACKUP_UPLOAD_MAX_BYTES", 0)
            ),
        },
    )


def _restore_from_upload(request, company):
    import tempfile
    from pathlib import Path

    from django.conf import settings

    from business.backup import BackupError
    from business.restore import restore_from_uploaded_files

    merge = company is not None
    uploads = list(request.FILES.getlist("files"))
    for key in ("db_file", "media_file", "zip_file"):
        f = request.FILES.get(key)
        if f:
            uploads.append(f)

    if not uploads:
        messages.error(
            request,
            "Choose the backup file(s) you downloaded — database dump (.dump), "
            "media archive (.tar.gz), or a zip that contains them.",
        )
        return redirect("restore_page")

    max_bytes = int(getattr(settings, "BACKUP_UPLOAD_MAX_BYTES", 0) or 0)
    try:
        with tempfile.TemporaryDirectory(prefix="sales-restore-") as tmp_name:
            tmp_path = Path(tmp_name)
            saved: list[Path] = []
            for uploaded in uploads:
                name = Path(getattr(uploaded, "name", "") or "").name
                if not name or name in {".", ".."} or "/" in name or "\\" in name:
                    raise BackupError("One of the uploaded files has an invalid name.")
                size = getattr(uploaded, "size", None)
                if size == 0:
                    raise BackupError(f"{name} is empty and cannot be restored.")
                if max_bytes and size and size > max_bytes:
                    raise BackupError(
                        f"{name} is larger than the restore upload limit "
                        f"({_format_bytes(max_bytes)})."
                    )
                dest = tmp_path / name
                with dest.open("wb") as out:
                    for chunk in uploaded.chunks():
                        out.write(chunk)
                saved.append(dest)
            result = restore_from_uploaded_files(saved, merge=merge)
    except BackupError as exc:
        messages.error(request, str(exc))
        return redirect("restore_page")
    except Exception as exc:
        logger.exception("Restore upload failed")
        messages.error(request, f"Restore failed: {exc}")
        return redirect("restore_page")

    return _complete_restore(request, result, merge=merge, next_name="restore_page")


def _complete_restore(request, result, *, merge: bool, next_name: str):
    from django.contrib.auth import logout

    from business.restore import format_restore_message

    text = format_restore_message(result)
    try:
        if result.status == "ok":
            messages.success(request, text)
        else:
            messages.error(request, text)
    except Exception:
        logger.exception("Could not store restore flash message")

    if not merge:
        try:
            logout(request)
        except Exception:
            logger.exception("Could not log out after full restore")
        return redirect("account_login")
    return redirect(next_name)
