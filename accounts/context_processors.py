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
        return {"last_restore_notice": _last_restore_notice()}
    company = get_company_for_user(request.user)
    notice = _last_restore_notice()
    if company is None:
        return {
            "user_can_manage_backups": True,
            "last_restore_notice": notice,
        }
    is_owner = user_is_owner(request.user, company)
    return {
        "user_company": company,
        "user_has_sales_access": user_has_sales_access(request.user, company),
        "user_can_grant_sales": user_can_grant_sales(request.user, company),
        "user_has_support_ticket_access": user_has_support_ticket_access(
            request.user, company
        ),
        "user_is_company_owner": is_owner,
        "user_can_manage_backups": is_owner,
        "last_restore_notice": notice,
    }


def _last_restore_notice():
    try:
        from business.restore import read_last_restore

        return read_last_restore()
    except Exception:
        return None
