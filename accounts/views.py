from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods

from .forms import AddCompanyMemberForm, CompanyProfileForm, UpdateCompanyMemberForm
from .models import CompanyMember, CompanyProfile
from .permissions import (
    get_company_for_user,
    sales_grant_required,
    user_has_sales_access,
    user_is_owner,
)


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


def _company_setup_context(form, profile):
    from business.reminder_schedule import light_schedule, recommended_schedule

    return {
        "form": form,
        "editing": profile is not None,
        "company": profile,
        "zoho_configured": _zoho_app_configured(),
        "reminder_presets": {
            "light": light_schedule(),
            "recommended": recommended_schedule(),
        },
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

    if action not in {"client", "team", "both"}:
        messages.error(request, "Unknown send action.")
        return redirect("company_setup")

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
