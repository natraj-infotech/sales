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
        form = CompanyProfileForm(request.POST, instance=profile)
        if form.is_valid():
            obj = form.save(commit=False)
            obj.user = request.user
            obj.save()
            CompanyMember.objects.update_or_create(
                company=obj,
                user=request.user,
                defaults={
                    "role": CompanyMember.Role.OWNER,
                    "sales_access": True,
                    "can_grant_sales": True,
                },
            )
            return redirect("dashboard")
    else:
        form = CompanyProfileForm(instance=profile)

    return render(
        request,
        "accounts/company_setup.html",
        {"form": form, "editing": profile is not None, "company": profile},
    )


@login_required
@require_http_methods(["POST"])
def company_send_expiry_notifications(request):
    company = get_company_for_user(request.user)
    if company is None or not user_is_owner(request.user, company):
        messages.error(request, "Only the company owner can send expiry notifications.")
        return redirect("company_setup")

    form = CompanyProfileForm(request.POST, instance=company)
    if not form.is_valid():
        messages.error(
            request,
            "Fix the company profile settings below before sending expiry notifications.",
        )
        return render(
            request,
            "accounts/company_setup.html",
            {"form": form, "editing": True, "company": company},
        )

    company = form.save(commit=False)
    company.user = request.user
    company.save()

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
