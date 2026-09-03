from functools import wraps

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ObjectDoesNotExist
from django.shortcuts import redirect

from .models import CompanyMember, CompanyProfile


def get_membership(user, company=None):
    qs = CompanyMember.objects.filter(user=user).select_related("company")
    if company is not None:
        qs = qs.filter(company=company)
    return qs.first()


def get_company_for_user(user):
    try:
        return user.company_profile
    except ObjectDoesNotExist:
        membership = get_membership(user)
        return membership.company if membership else None


def user_is_owner(user, company):
    return CompanyProfile.objects.filter(pk=company.pk, user=user).exists()


def user_has_sales_access(user, company):
    if user_is_owner(user, company):
        return True
    membership = get_membership(user, company)
    return bool(membership and membership.sales_access)


def user_can_grant_sales(user, company):
    if user_is_owner(user, company):
        return True
    membership = get_membership(user, company)
    return bool(membership and membership.can_grant_sales)


def user_has_support_ticket_access(user, company):
    if user_is_owner(user, company):
        return True
    membership = get_membership(user, company)
    return bool(membership and membership.support_ticket_access)


def company_users(company):
    from django.contrib.auth import get_user_model

    User = get_user_model()
    return User.objects.filter(id__in=company.company_user_ids()).order_by("email")


def sales_module_required(view_func):
    @login_required
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        company = get_company_for_user(request.user)
        if company is None:
            return redirect("company_setup")
        if not user_has_sales_access(request.user, company):
            messages.error(
                request,
                "You do not have access to the sales module. Ask a sales manager on your team to grant access.",
            )
            return redirect("access_pending")
        return view_func(request, *args, **kwargs)

    return wrapper


def company_owner_required(view_func):
    @login_required
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        company = get_company_for_user(request.user)
        if company is None or not user_is_owner(request.user, company):
            messages.error(request, "Only the company owner can manage this page.")
            if company and user_has_sales_access(request.user, company):
                return redirect("dashboard")
            if company:
                return redirect("access_pending")
            return redirect("company_setup")
        return view_func(request, *args, **kwargs)

    return wrapper


def sales_grant_required(view_func):
    @login_required
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        company = get_company_for_user(request.user)
        if company is None or not user_can_grant_sales(request.user, company):
            messages.error(
                request,
                "You do not have permission to manage sales module access.",
            )
            if company and user_has_sales_access(request.user, company):
                return redirect("dashboard")
            return redirect("access_pending")
        return view_func(request, *args, **kwargs)

    return wrapper


def support_ticket_access_required(view_func):
    @login_required
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        company = get_company_for_user(request.user)
        if company is None:
            return redirect("company_setup")
        if not user_has_support_ticket_access(request.user, company):
            messages.error(
                request,
                "You do not have access to support tickets. Ask your company owner to grant access from Team access.",
            )
            if user_has_sales_access(request.user, company):
                return redirect("dashboard")
            return redirect("access_pending")
        return view_func(request, *args, **kwargs)

    return wrapper
