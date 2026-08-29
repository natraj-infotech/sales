from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from .forms import (
    AddCompanyMemberForm,
    CompanyProfileForm,
    UpdateCompanyMemberForm,
)
from .models import CompanyMember, CompanyProfile
from .permissions import (
    company_owner_required,
    get_company_for_user,
    sales_grant_required,
    user_has_sales_access,
    user_is_owner,
)

MANUAL_SEND_THROTTLE_SECONDS = 20


def root_redirect(request):
    if request.user.is_authenticated:
        company = get_company_for_user(request.user)
        if company and user_has_sales_access(request.user, company):
            return redirect("dashboard")
        if company:
            return redirect("access_pending")
        return redirect("company_setup")
    return redirect("account_login")


@login_required
def access_pending(request):
    company = get_company_for_user(request.user)
    if company is None:
        return redirect("company_setup")
    if user_has_sales_access(request.user, company):
        return redirect("dashboard")
    return render(
        request,
        "accounts/access_pending.html",
        {"company": company},
    )


@login_required
def company_setup(request):
    company = get_company_for_user(request.user)
    membership = CompanyMember.objects.filter(user=request.user).first()
    if membership and not user_is_owner(request.user, membership.company):
        messages.info(
            request,
            "Your account is linked to a company team. Company profile setup is only for account owners.",
        )
        return redirect("access_pending")

    profile = CompanyProfile.objects.filter(user=request.user).first()

    if request.method == "POST":
        form = CompanyProfileForm(
            request.POST, request.FILES, instance=profile
        )
        if form.is_valid():
            obj = form.save(commit=False)
            obj.user = request.user
            obj.save()
            form.save_m2m()
            CompanyMember.objects.update_or_create(
                company=obj,
                user=request.user,
                defaults={
                    "role": CompanyMember.Role.OWNER,
                    "sales_access": True,
                    "can_grant_sales": True,
                },
            )
            try:
                from business.helpdesk_bridge import sync_company_queue

                sync_company_queue(obj)
            except Exception:
                pass
            return redirect("dashboard")
    else:
        form = CompanyProfileForm(instance=profile)

    return render(
        request,
        "accounts/company_setup.html",
        _company_setup_context(form, profile),
    )


@company_owner_required
def reminder_template(request):
    import json

    from django.core.exceptions import ValidationError

    from business.models import Client, ReminderTemplate
    from business.reminder_schedule import default_reminder_cycle_steps, recommended_schedule
    from business.reminder_templates import (
        create_reminder_template,
        delete_reminder_template,
        ensure_default_reminder_template,
        save_reminder_templates,
    )

    company = get_company_for_user(request.user)
    ensure_default_reminder_template(company)

    if request.method == "POST":
        delete_id = (request.POST.get("delete_template_id") or "").strip()
        if delete_id.isdigit():
            template = ReminderTemplate.objects.filter(
                pk=int(delete_id), company=company
            ).first()
            if template is None:
                messages.error(request, "Reminder template not found.")
            else:
                try:
                    delete_reminder_template(template)
                    messages.success(request, "Reminder template deleted.")
                except ValidationError as exc:
                    messages.error(request, str(exc))
            return redirect("reminder_template")

        action = (request.POST.get("action") or "save").strip()
        if action == "create":
            name = (request.POST.get("new_template_name") or "").strip()
            try:
                template = create_reminder_template(company, name or "New template")
                messages.success(request, "Reminder schedule cycle created.")
                return redirect(f"{reverse('reminder_template')}?active={template.pk}")
            except ValidationError as exc:
                messages.error(request, str(exc))
            return redirect("reminder_template")

        template_payloads = []
        for template in ReminderTemplate.objects.filter(company=company):
            template_payloads.append(
                {
                    "id": template.pk,
                    "name": request.POST.get(f"template_{template.pk}_name", ""),
                    "schedule": request.POST.get(
                        f"template_{template.pk}_schedule_json", "[]"
                    ),
                }
            )
        try:
            save_reminder_templates(
                company,
                template_payloads=template_payloads,
            )
            messages.success(request, "Reminder profile saved.")
        except ValidationError as exc:
            detail = (
                " ".join(exc.messages) if getattr(exc, "messages", None) else str(exc)
            )
            messages.error(request, detail)
        active = (request.POST.get("active_template_id") or "").strip()
        if active.isdigit():
            return redirect(f"{reverse('reminder_template')}?active={active}")
        return redirect("reminder_template")

    templates = list(
        ReminderTemplate.objects.filter(company=company).prefetch_related("clients")
    )
    all_clients = list(Client.objects.filter(company=company).order_by("name"))
    default_template = next((item for item in templates if item.is_default), None)

    template_rows = []
    for template in templates:
        if template.is_default:
            assigned_clients = [
                client
                for client in all_clients
                if client.reminder_template_id in (None, template.pk)
            ]
        else:
            assigned_clients = list(template.clients.all())
        template_rows.append(
            {
                "template": template,
                "schedule_json": json.dumps(template.schedule or []),
                "assigned_clients": assigned_clients,
                "builder_id": f"template-builder-{template.pk}",
                "hidden_id": f"template-{template.pk}-schedule-json",
            }
        )

    active_raw = (request.GET.get("active") or "").strip()
    active_template_id = None
    if active_raw.isdigit():
        active_template_id = int(active_raw)
    elif template_rows:
        active_template_id = template_rows[0]["template"].pk

    return render(
        request,
        "accounts/reminder_template.html",
        {
            "company": company,
            "template_rows": template_rows,
            "all_clients": all_clients,
            "default_template": default_template,
            "active_template_id": active_template_id,
            "default_cycle_steps": default_reminder_cycle_steps(),
            "reminder_presets": {
                "recommended": recommended_schedule(),
            },
        },
    )


def _format_price_input(value) -> str:
    from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

    try:
        amount = Decimal(value if value is not None else "0")
    except (InvalidOperation, TypeError, ValueError):
        amount = Decimal("0")
    return format(amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), "f")


def _parse_unit_price(raw: str):
    from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

    text = (raw or "").strip().replace(",", "").replace("₹", "").replace("Rs.", "")
    if not text:
        return Decimal("0.00"), None
    try:
        value = Decimal(text)
    except InvalidOperation:
        return None, "Enter a valid price."
    if value < 0:
        return None, "Price cannot be negative."
    quantized = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if quantized >= Decimal("10000000000"):
        return None, "Price is too large."
    return quantized, None


def _product_price_rows(products, posted=None):
    rows = []
    for product in products:
        if posted is not None:
            price_input = posted.get(f"price_{product.pk}", "")
        else:
            price_input = _format_price_input(product.unit_price)
        rows.append({"product": product, "price_input": price_input})
    return rows


@company_owner_required
def company_product_prices(request):
    from django.db.models import Q

    from business.models import Product

    company = get_company_for_user(request.user)
    q = (request.POST.get("q") or request.GET.get("q") or "").strip()
    products = company.products.all().order_by("name")
    if q:
        products = products.filter(
            Q(name__icontains=q)
            | Q(company_name__icontains=q)
            | Q(sku__icontains=q)
            | Q(category__icontains=q)
        )
    products = list(products)

    if request.method == "POST":
        errors = []
        changed = []
        id_set = {product.pk for product in products}
        for product in products:
            value, err = _parse_unit_price(request.POST.get(f"price_{product.pk}", ""))
            if err:
                errors.append(f"{product.name}: {err}")
                continue
            if product.unit_price != value:
                product.unit_price = value
                changed.append(product)
        extra_ids = [
            key[6:]
            for key in request.POST
            if key.startswith("price_") and key[6:].isdigit()
        ]
        missing_ids = [
            int(raw) for raw in extra_ids if int(raw) not in id_set
        ]
        if missing_ids:
            extra_products = list(
                Product.objects.filter(company=company, pk__in=missing_ids)
            )
            for product in extra_products:
                value, err = _parse_unit_price(
                    request.POST.get(f"price_{product.pk}", "")
                )
                if err:
                    errors.append(f"{product.name}: {err}")
                    continue
                if product.unit_price != value:
                    product.unit_price = value
                    changed.append(product)

        if errors:
            messages.error(request, " ".join(errors))
            return render(
                request,
                "accounts/product_prices.html",
                {
                    "company": company,
                    "rows": _product_price_rows(products, request.POST),
                    "q": q,
                },
            )

        if changed:
            Product.objects.bulk_update(changed, ["unit_price"])
            messages.success(
                request,
                f"Saved list prices for {len(changed)} product(s).",
            )
        else:
            messages.info(request, "No price changes to save.")
        if q:
            return redirect(
                f"{reverse('company_product_prices')}?{urlencode({'q': q})}"
            )
        return redirect("company_product_prices")

    return render(
        request,
        "accounts/product_prices.html",
        {
            "company": company,
            "rows": _product_price_rows(products),
            "q": q,
        },
    )


def _company_setup_context(form, profile):
    return {
        "form": form,
        "editing": profile is not None,
        "company": profile,
        "zoho_configured": _zoho_app_configured(),
    }


def _zoho_app_configured() -> bool:
    from business.zoho_crm import zoho_configured

    return zoho_configured()


@login_required
@require_http_methods(["GET"])
def zoho_connect(request):
    company = get_company_for_user(request.user)
    if company is None or not user_is_owner(request.user, company):
        messages.error(request, "Only the company owner can connect Zoho CRM.")
        return redirect("company_setup")

    from business.zoho_crm import build_authorize_url, new_oauth_state, zoho_configured

    if not zoho_configured():
        messages.error(
            request,
            "Zoho app credentials are not configured. Add ZOHO_CLIENT_ID and "
            "ZOHO_CLIENT_SECRET to the server .env file.",
        )
        return redirect("company_setup")

    state = new_oauth_state()
    request.session["zoho_oauth_state"] = state
    request.session["zoho_oauth_company_id"] = company.pk
    return redirect(build_authorize_url(state=state))


@login_required
@require_http_methods(["GET"])
def zoho_callback(request):
    company = get_company_for_user(request.user)
    if company is None or not user_is_owner(request.user, company):
        messages.error(request, "Only the company owner can connect Zoho CRM.")
        return redirect("company_setup")

    error = (request.GET.get("error") or "").strip()
    if error:
        messages.error(request, f"Zoho authorization failed: {error}")
        return redirect("company_setup")

    state = (request.GET.get("state") or "").strip()
    expected = (request.session.pop("zoho_oauth_state", None) or "").strip()
    if not state or state != expected:
        messages.error(request, "Zoho authorization state mismatch. Try connecting again.")
        return redirect("company_setup")

    code = (request.GET.get("code") or "").strip()
    if not code:
        messages.error(request, "Zoho did not return an authorization code.")
        return redirect("company_setup")

    from business.zoho_crm import (
        exchange_code_for_tokens,
        fetch_connected_user_email,
        save_oauth_tokens,
    )

    try:
        payload = exchange_code_for_tokens(code)
        if not payload.get("refresh_token") and not company.zoho_refresh_token:
            messages.error(
                request,
                "Zoho did not return a refresh token. Disconnect the app in Zoho "
                "API Console and reconnect with consent.",
            )
            return redirect("company_setup")
        save_oauth_tokens(company, payload)
        email = fetch_connected_user_email(company)
        if email:
            company.zoho_connected_email = email
            company.save(update_fields=["zoho_connected_email", "updated_at"])
        messages.success(request, "Zoho CRM connected successfully.")
    except Exception as exc:
        messages.error(request, f"Could not connect Zoho CRM: {exc}")
    return redirect("company_setup")


@login_required
@require_http_methods(["POST"])
def zoho_disconnect(request):
    company = get_company_for_user(request.user)
    if company is None or not user_is_owner(request.user, company):
        messages.error(request, "Only the company owner can disconnect Zoho CRM.")
        return redirect("company_setup")

    from business.zoho_crm import disconnect_zoho

    disconnect_zoho(company)
    messages.success(request, "Zoho CRM disconnected.")
    return redirect("company_setup")


@login_required
@require_http_methods(["POST"])
def company_send_expiry_notifications(request):
    company = get_company_for_user(request.user)
    if company is None or not user_is_owner(request.user, company):
        messages.error(request, "Only the company owner can send expiry notifications.")
        return redirect("company_setup")

    form = CompanyProfileForm(request.POST, request.FILES, instance=company)
    if not form.is_valid():
        messages.error(
            request,
            "Fix the company profile settings below before sending expiry notifications.",
        )
        return render(
            request,
            "accounts/company_setup.html",
            _company_setup_context(form, company),
        )

    company = form.save(commit=False)
    company.user = request.user
    company.save()
    form.save_m2m()
    try:
        from business.helpdesk_bridge import sync_company_queue

        sync_company_queue(company)
    except Exception:
        pass

    action = request.POST.get("action", "").strip()
    if action not in {"client", "team", "both"}:
        messages.error(request, "Unknown send action.")
        return redirect("company_setup")

    # Manual sends are allowed as often as the owner wants, so the only guard is
    # against an accidental double submit of the same button.
    throttle_key = f"expiry-manual-send:{company.pk}:{action}"
    if not cache.add(throttle_key, "1", MANUAL_SEND_THROTTLE_SECONDS):
        messages.info(
            request,
            "That send was already submitted a moment ago. "
            f"Wait {MANUAL_SEND_THROTTLE_SECONDS} seconds before sending again.",
        )
        return redirect("company_setup")

    from business.expiry_notifications import (
        send_client_expiry_emails,
        send_team_expiry_digest,
    )

    client_sent = 0
    team_sent = 0
    logs: list[str] = []

    if action in {"client", "both"}:
        count, client_logs = send_client_expiry_emails(company, force=True)
        client_sent += count
        logs.extend(client_logs)
    if action in {"team", "both"}:
        count, team_logs = send_team_expiry_digest(company, force=True)
        team_sent += count
        logs.extend(team_logs)

    for line in logs:
        if any(word in line.lower() for word in ("failed", "skipped")):
            messages.warning(request, line)
        else:
            messages.success(request, line)

    if not logs:
        messages.info(request, "No expiry notifications were sent.")

    if client_sent or team_sent:
        summary_parts = []
        if client_sent:
            summary_parts.append(f"{client_sent} client email batch(es)")
        if team_sent:
            summary_parts.append(f"{team_sent} team digest(s)")
        messages.success(
            request,
            "Sent " + " and ".join(summary_parts) + ".",
        )

    return redirect("company_setup")


@sales_grant_required
def company_team(request):
    company = get_company_for_user(request.user)
    requester_is_owner = user_is_owner(request.user, company)
    add_form = AddCompanyMemberForm(
        company=company,
        requester_is_owner=requester_is_owner,
    )

    if request.method == "POST":
        action = request.POST.get("action", "").strip()
        if action == "add":
            add_form = AddCompanyMemberForm(
                request.POST,
                company=company,
                requester_is_owner=requester_is_owner,
            )
            if add_form.is_valid():
                member = add_form.save()
                messages.success(
                    request,
                    f"Added {member.user.email} to your team.",
                )
                return redirect("company_team")
        elif action == "update":
            member = get_object_or_404(
                CompanyMember,
                pk=request.POST.get("member_id"),
                company=company,
            )
            update_form = UpdateCompanyMemberForm(
                request.POST,
                member=member,
                requester_is_owner=requester_is_owner,
            )
            if update_form.is_valid():
                update_form.save()
                messages.success(
                    request,
                    f"Updated access for {member.user.email}.",
                )
                return redirect("company_team")
        elif action == "remove":
            member = get_object_or_404(
                CompanyMember,
                pk=request.POST.get("member_id"),
                company=company,
            )
            if member.role == CompanyMember.Role.OWNER:
                messages.error(request, "The company owner cannot be removed.")
            else:
                email = member.user.email
                member.delete()
                messages.success(request, f"Removed {email} from your team.")
            return redirect("company_team")

    members = company.members.select_related("user").order_by("role", "user__email")

    return render(
        request,
        "accounts/company_team.html",
        {
            "company": company,
            "members": members,
            "add_form": add_form,
            "requester_is_owner": requester_is_owner,
        },
    )
