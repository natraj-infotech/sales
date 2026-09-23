import os

from .permissions import (
    get_company_for_user,
    user_can_grant_sales,
    user_has_sales_access,
    user_has_support_ticket_access,
    user_is_owner,
)


# Client-facing and auth screens must never show an internal restore banner.
_PUBLIC_URL_NAMES = frozenset(
    {
        "account_login",
        "account_logout",
        "account_signup",
        "privacy_policy",
        "renewal_confirm_public",
        "renewal_confirm_public_legacy",
        "renewal_track_open",
        "caselock_public_form",
        "vendor_price_submit",
        "vendor_price_submit_post",
    }
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
        return {"last_restore_notice": None}
    company = get_company_for_user(request.user)
    notice = None if _is_public_request(request) else _last_restore_notice()
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


def _is_public_request(request):
    match = getattr(request, "resolver_match", None)
    url_name = getattr(match, "url_name", "") or ""
    return url_name in _PUBLIC_URL_NAMES


def _last_restore_notice():
    try:
        from business.restore import read_last_restore

        return read_last_restore()
    except Exception:
        return None
