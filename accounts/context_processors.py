import os

from .permissions import (
    get_company_for_user,
    user_can_grant_sales,
    user_has_sales_access,
    user_has_support_ticket_access,
    user_is_owner,
)


def oauth_availability(_request):
    return {
        "google_oauth_ready": bool(os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "").strip()),
        "microsoft_oauth_ready": bool(
            os.environ.get("MICROSOFT_OAUTH_CLIENT_ID", "").strip()
        ),
    }


def company_access(request):
    if not request.user.is_authenticated:
        return {}
    company = get_company_for_user(request.user)
    if company is None:
        return {}
    return {
        "user_company": company,
        "user_has_sales_access": user_has_sales_access(request.user, company),
        "user_can_grant_sales": user_can_grant_sales(request.user, company),
        "user_has_support_ticket_access": user_has_support_ticket_access(
            request.user, company
        ),
        "user_is_company_owner": user_is_owner(request.user, company),
    }
