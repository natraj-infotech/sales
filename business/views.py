from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth import get_user_model
from django.db.models import Count, ProtectedError, Q, Sum
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods
from openpyxl import Workbook

from accounts.models import CompanyProfile

from .forms import (
    CaseLockInboxForm,
    CaseLockPublicForm,
    ClientForm,
    ClientSubscriptionForm,
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
from .subscription_import import import_subscriptions_from_xlsx

User = get_user_model()


def _company(request):
    return get_object_or_404(CompanyProfile, user=request.user)


@login_required
def dashboard(request):
    company = _company(request)
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


@login_required
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


@login_required
@require_http_methods(["POST"])
def vendor_bulk_delete(request):
    company = _company(request)
    ids = request.POST.getlist("vendor_ids")
    if not ids:
        messages.warning(request, "Select at least one vendor to delete.")
        return redirect("vendor_list")
    qs = company.vendors.filter(pk__in=ids)
    n = qs.count()
    qs.delete()
    messages.success(request, f"Deleted {n} vendor(s).")
    return redirect("vendor_list")


@login_required
def vendor_import(request):
    return render(request, "business/vendor_import.html", {})


@login_required
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


@login_required
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


@login_required
def client_list(request):
    return _crud_list(
        request, Client, "business/client_list.html", ("name", "email", "phone", "gst_number")
    )


@login_required
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


@login_required
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


@login_required
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


@login_required
@require_http_methods(["POST"])
def product_bulk_delete(request):
    company = _company(request)
    ids = request.POST.getlist("product_ids")
    if not ids:
        messages.warning(request, "Select at least one product to delete.")
        return redirect("product_list")
    qs = company.products.filter(pk__in=ids)
    n = qs.count()
    try:
        qs.delete()
    except ProtectedError:
        messages.error(
            request,
            "Cannot delete: one or more products are linked to client subscriptions. "
            "Remove or change those subscriptions first.",
        )
        return redirect("product_list")
    messages.success(request, f"Deleted {n} product(s).")
    return redirect("product_list")


@login_required
def product_import(request):
    return render(request, "business/product_import.html", {})


@login_required
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


@login_required
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


@login_required
def subscription_list(request):
    company = _company(request)
    qs = company.subscriptions.select_related("client", "product", "vendor")
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
    if expiring == "7":
        until = today + timedelta(days=7)
        qs = qs.filter(
            end_date__isnull=False,
            end_date__gte=today,
            end_date__lte=until,
        )
    elif expiring == "30":
        until = today + timedelta(days=30)
        qs = qs.filter(
            end_date__isnull=False,
            end_date__gte=today,
            end_date__lte=until,
        )

    categories = (
        company.products.exclude(category="")
        .values_list("category", flat=True)
        .distinct()
        .order_by("category")
    )

    return render(
        request,
        "business/subscription_list.html",
        {
            "company": company,
            "object_list": qs,
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
            "expiring_choices": [
                ("", "All"),
                ("7", "Next 7 days"),
                ("30", "Next 30 days"),
            ],
        },
    )


@login_required
def subscription_import_template(request):
    _company(request)
    wb = Workbook()
    ws = wb.active
    ws.title = "Subscriptions"
    headers = [
        "client",
        "product",
        "vendor",
        "quantity",
        "start_date",
        "end_date",
        "billing_amount",
        "invoice_number",
        "serial_number",
        "subscription_code",
        "status",
        "notes",
    ]
    ws.append(headers)
    ws.append(
        [
            "Acme Ltd",
            "Example product name",
            "",
            1,
            "2026-01-01",
            "",
            0,
            "",
            "",
            "",
            "active",
            "",
        ]
    )
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


@login_required
@require_http_methods(["GET", "POST"])
def subscription_import(request):
    company = _company(request)
    if request.method == "POST":
        upload = request.FILES.get("file")
        if not upload:
            messages.error(request, "Choose an Excel file (.xlsx) to upload.")
            return redirect("subscription_import")
        if not (upload.name or "").lower().endswith(".xlsx"):
            messages.error(request, "Please upload an .xlsx file.")
            return redirect("subscription_import")
        created, errs = import_subscriptions_from_xlsx(company, upload.read())
        if created:
            messages.success(request, f"Imported {created} subscription(s).")
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
        return redirect("subscription_list")

    return render(
        request,
        "business/subscription_import.html",
        {"company": company},
    )


@login_required
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


@login_required
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


@login_required
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

    assignees = User.objects.filter(company_profile=company).order_by("email")

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


@login_required
@require_http_methods(["POST"])
def support_ticket_update(request, pk):
    company = _company(request)
    ticket = get_object_or_404(SupportTicket, pk=pk, company=company)
    assignees = User.objects.filter(company_profile=company).order_by("email")
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


@login_required
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


@login_required
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


@login_required
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


@login_required
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


@login_required
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


@login_required
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


@login_required
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
