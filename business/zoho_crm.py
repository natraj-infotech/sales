"""Zoho CRM OAuth and Quotes helpers for renewal quotations."""

from __future__ import annotations

import logging
import secrets
from datetime import timedelta
from decimal import Decimal
from urllib.parse import urlencode

import requests
from django.conf import settings
from django.utils import timezone

from accounts.models import CompanyProfile
from business.models import SubscriptionRenewalInvite, SubscriptionRenewalInviteLine

logger = logging.getLogger(__name__)

ZOHO_SCOPES = "ZohoCRM.modules.ALL,ZohoCRM.settings.ALL,ZohoCRM.users.READ,offline_access"


def zoho_configured() -> bool:
    return bool(
        getattr(settings, "ZOHO_CLIENT_ID", "")
        and getattr(settings, "ZOHO_CLIENT_SECRET", "")
    )


def zoho_dc() -> str:
    dc = (getattr(settings, "ZOHO_DC", "in") or "in").strip().lower()
    if dc not in {"in", "com", "eu", "com.au", "jp", "ca", "sa"}:
        return "in"
    return dc


def accounts_base_url(dc: str | None = None) -> str:
    return f"https://accounts.zoho.{dc or zoho_dc()}"


def api_base_url(dc: str | None = None) -> str:
    return f"https://www.zohoapis.{dc or zoho_dc()}"


def crm_ui_base_url(dc: str | None = None) -> str:
    return f"https://crm.zoho.{dc or zoho_dc()}"


def redirect_uri() -> str:
    configured = (getattr(settings, "ZOHO_REDIRECT_URI", "") or "").strip()
    if configured:
        return configured
    public = (getattr(settings, "PUBLIC_BASE_URL", "") or "").strip().rstrip("/")
    if public:
        return f"{public}/company/zoho/callback/"
    return "http://127.0.0.1:8000/company/zoho/callback/"


def build_authorize_url(*, state: str) -> str:
    params = {
        "scope": ZOHO_SCOPES,
        "client_id": settings.ZOHO_CLIENT_ID,
        "response_type": "code",
        "access_type": "offline",
        "prompt": "consent",
        "redirect_uri": redirect_uri(),
        "state": state,
    }
    return f"{accounts_base_url()}/oauth/v2/auth?{urlencode(params)}"


def exchange_code_for_tokens(code: str) -> dict:
    try:
        resp = requests.post(
            f"{accounts_base_url()}/oauth/v2/token",
            data={
                "grant_type": "authorization_code",
                "client_id": settings.ZOHO_CLIENT_ID,
                "client_secret": settings.ZOHO_CLIENT_SECRET,
                "redirect_uri": redirect_uri(),
                "code": code,
            },
            timeout=30,
        )
    except requests.RequestException:
        logger.exception("Zoho token exchange network error")
        raise RuntimeError("Could not reach Zoho. Try again.") from None
    payload = resp.json() if resp.content else {}
    if not resp.ok or "error" in payload:
        logger.warning(
            "Zoho token exchange failed status=%s body=%s",
            resp.status_code,
            (resp.text or "")[:300],
        )
        raise RuntimeError(
            payload.get("error")
            or payload.get("error_description")
            or f"Zoho token exchange failed ({resp.status_code})"
        )
    return payload


def refresh_access_token(company: CompanyProfile) -> str:
    refresh = (company.zoho_refresh_token or "").strip()
    if not refresh:
        raise RuntimeError("Zoho CRM is not connected for this company.")

    accounts_domain = (company.zoho_accounts_domain or "").strip() or accounts_base_url()
    try:
        resp = requests.post(
            f"{accounts_domain}/oauth/v2/token",
            data={
                "grant_type": "refresh_token",
                "client_id": settings.ZOHO_CLIENT_ID,
                "client_secret": settings.ZOHO_CLIENT_SECRET,
                "refresh_token": refresh,
            },
            timeout=30,
        )
    except requests.RequestException:
        logger.exception(
            "Zoho token refresh network error company_id=%s", company.pk
        )
        raise RuntimeError("Could not reach Zoho to refresh the connection.") from None
    payload = resp.json() if resp.content else {}
    if not resp.ok or "access_token" not in payload:
        raise RuntimeError(
            payload.get("error")
            or payload.get("error_description")
            or f"Zoho token refresh failed ({resp.status_code})"
        )

    expires_in = int(payload.get("expires_in") or 3600)
    company.zoho_access_token = payload["access_token"]
    company.zoho_access_token_expires_at = timezone.now() + timedelta(
        seconds=max(expires_in - 60, 60)
    )
    api_domain = (payload.get("api_domain") or "").strip()
    if api_domain:
        company.zoho_api_domain = api_domain
    company.save(
        update_fields=[
            "zoho_access_token",
            "zoho_access_token_expires_at",
            "zoho_api_domain",
            "updated_at",
        ]
    )
    return company.zoho_access_token


def get_access_token(company: CompanyProfile) -> str:
    token = (company.zoho_access_token or "").strip()
    expires = company.zoho_access_token_expires_at
    if token and expires and expires > timezone.now():
        return token
    return refresh_access_token(company)


def save_oauth_tokens(company: CompanyProfile, payload: dict) -> None:
    company.zoho_refresh_token = (payload.get("refresh_token") or company.zoho_refresh_token or "").strip()
    company.zoho_access_token = (payload.get("access_token") or "").strip()
    expires_in = int(payload.get("expires_in") or 3600)
    company.zoho_access_token_expires_at = timezone.now() + timedelta(
        seconds=max(expires_in - 60, 60)
    )
    company.zoho_api_domain = (payload.get("api_domain") or "").strip() or api_base_url()
    company.zoho_accounts_domain = accounts_base_url()
    company.zoho_connected_at = timezone.now()
    company.save(
        update_fields=[
            "zoho_refresh_token",
            "zoho_access_token",
            "zoho_access_token_expires_at",
            "zoho_api_domain",
            "zoho_accounts_domain",
            "zoho_connected_at",
            "updated_at",
        ]
    )


def disconnect_zoho(company: CompanyProfile) -> None:
    company.zoho_refresh_token = ""
    company.zoho_access_token = ""
    company.zoho_access_token_expires_at = None
    company.zoho_api_domain = ""
    company.zoho_accounts_domain = ""
    company.zoho_connected_email = ""
    company.zoho_connected_at = None
    company.save(
        update_fields=[
            "zoho_refresh_token",
            "zoho_access_token",
            "zoho_access_token_expires_at",
            "zoho_api_domain",
            "zoho_accounts_domain",
            "zoho_connected_email",
            "zoho_connected_at",
            "updated_at",
        ]
    )


def _crm_api_root(company: CompanyProfile) -> str:
    domain = (company.zoho_api_domain or "").strip().rstrip("/")
    if domain:
        return f"{domain}/crm/v2"
    return f"{api_base_url()}/crm/v2"


def _crm_headers(company: CompanyProfile) -> dict[str, str]:
    return {
        "Authorization": f"Zoho-oauthtoken {get_access_token(company)}",
        "Content-Type": "application/json",
    }


def fetch_connected_user_email(company: CompanyProfile) -> str:
    url = f"{_crm_api_root(company)}/users?type=CurrentUser"
    resp = requests.get(url, headers=_crm_headers(company), timeout=30)
    if not resp.ok:
        return ""
    users = (resp.json() or {}).get("users") or []
    if not users:
        return ""
    return (users[0].get("email") or "").strip()


def _escape_criteria_value(value: str) -> str:
    # Zoho CRM search criteria: escape ( ) \
    return (
        (value or "")
        .replace("\\", "\\\\")
        .replace("(", "\\(")
        .replace(")", "\\)")
    )


def _search_module(
    company: CompanyProfile,
    module: str,
    criteria: str,
) -> dict | None:
    url = f"{_crm_api_root(company)}/{module}/search"
    resp = requests.get(
        url,
        headers=_crm_headers(company),
        params={"criteria": criteria},
        timeout=30,
    )
    if resp.status_code == 204:
        return None
    if not resp.ok:
        logger.warning("Zoho search %s failed: %s", module, resp.text[:300])
        return None
    data = (resp.json() or {}).get("data") or []
    return data[0] if data else None


def _create_record(company: CompanyProfile, module: str, payload: dict) -> dict:
    url = f"{_crm_api_root(company)}/{module}"
    try:
        resp = requests.post(
            url,
            headers=_crm_headers(company),
            json={"data": [payload]},
            timeout=30,
        )
    except requests.RequestException:
        logger.exception(
            "Zoho %s create network error company_id=%s", module, company.pk
        )
        raise RuntimeError(f"Could not reach Zoho to create {module}.") from None
    body = resp.json() if resp.content else {}
    if not resp.ok:
        logger.warning(
            "Zoho %s create failed company_id=%s status=%s body=%s",
            module,
            company.pk,
            resp.status_code,
            (resp.text or "")[:300],
        )
        raise RuntimeError(
            f"Zoho {module} create failed: {body or resp.text[:300]}"
        )
    details = ((body.get("data") or [{}])[0].get("details") or {})
    if not details.get("id"):
        message = ((body.get("data") or [{}])[0].get("message") or "unknown error")
        raise RuntimeError(f"Zoho {module} create failed: {message}")
    return details


def find_or_create_account(
    company: CompanyProfile,
    *,
    account_name: str,
    email: str,
) -> str:
    name = (account_name or "").strip() or (email.split("@")[0] if email else "Client")
    email_clean = (email or "").strip()

    if email_clean:
        found = _search_module(
            company,
            "Accounts",
            f"(Email:equals:{_escape_criteria_value(email_clean)})",
        )
        if found and found.get("id"):
            return str(found["id"])

    found = _search_module(
        company,
        "Accounts",
        f"(Account_Name:equals:{_escape_criteria_value(name)})",
    )
    if found and found.get("id"):
        return str(found["id"])

    details = _create_record(
        company,
        "Accounts",
        {
            "Account_Name": name[:200],
            **({"Email": email_clean} if email_clean else {}),
        },
    )
    return str(details["id"])


def find_or_create_product(
    company: CompanyProfile,
    *,
    product_name: str,
    unit_price: Decimal | float | int,
) -> str:
    name = (product_name or "").strip() or "Subscription product"
    found = _search_module(
        company,
        "Products",
        f"(Product_Name:equals:{_escape_criteria_value(name)})",
    )
    if found and found.get("id"):
        return str(found["id"])

    details = _create_record(
        company,
        "Products",
        {
            "Product_Name": name[:200],
            "Unit_Price": float(unit_price or 0),
        },
    )
    return str(details["id"])


def _quote_ui_url(company: CompanyProfile, quote_id: str) -> str:
    dc = zoho_dc()
    if company.zoho_api_domain and "zohoapis." in company.zoho_api_domain:
        # api_domain like https://www.zohoapis.in
        host = company.zoho_api_domain.replace("https://www.zohoapis.", "").replace(
            "http://www.zohoapis.", ""
        )
        if host:
            dc = host.split("/")[0]
    return f"{crm_ui_base_url(dc)}/crm/tab/Quotes/{quote_id}"


def _quote_quantity_for_line(line: SubscriptionRenewalInviteLine) -> int | None:
    if line.renewal_count is not None:
        return int(line.renewal_count)
    if line.current_quantity is not None:
        return int(line.current_quantity)
    subscription = getattr(line, "subscription", None)
    if subscription is not None and getattr(subscription, "quantity", None) is not None:
        return int(subscription.quantity)
    return None


def create_quote_from_renewal_invite(
    company: CompanyProfile,
    invite: SubscriptionRenewalInvite,
) -> tuple[str, str, str]:
    """
    Create a Zoho CRM Quote from renewal response lines.
    Returns (quote_id, quote_number, quote_url).
    """
    if not company.zoho_connected:
        raise RuntimeError("Connect Zoho CRM in Company profile first.")
    if not zoho_configured():
        raise RuntimeError("Zoho app credentials are missing in server settings.")

    lines = list(
        invite.lines.select_related("subscription", "subscription__product").order_by("id")
    )
    quote_items = []
    for line in lines:
        qty = _quote_quantity_for_line(line)
        if qty is None:
            continue
        quote_items.append((line, qty))
    if not quote_items:
        raise RuntimeError(
            "No product quantity is available to include in the quotation."
        )

    account_name = quote_items[0][0].company_name or invite.client_email
    account_id = find_or_create_account(
        company,
        account_name=account_name,
        email=invite.client_email,
    )

    product_details = []
    description_lines = []
    for line, qty in quote_items:
        unit_price = (
            line.subscription.billing_amount
            if line.subscription_id
            else Decimal("0")
        )
        product_id = find_or_create_product(
            company,
            product_name=line.product_name,
            unit_price=unit_price,
        )
        product_details.append(
            {
                "product": {"id": product_id},
                "quantity": qty,
                "list_price": float(unit_price or 0),
                "unit_price": float(unit_price or 0),
                "Discount": 0,
                "tax": 0,
            }
        )
        if line.renewal_count is not None:
            description_lines.append(
                f"- {line.product_name}: renew qty {qty} "
                f"(current {line.current_quantity})"
            )
        else:
            description_lines.append(
                f"- {line.product_name}: qty {qty} (current, no renewal qty yet)"
            )

    subject = f"Renewal quotation — {account_name}"[:120]
    payload = {
        "Subject": subject,
        "Account_Name": {"id": account_id},
        "Quote_Stage": "Draft",
        "Description": "\n".join(description_lines),
        "Product_Details": product_details,
        "Valid_Till": (timezone.localdate() + timedelta(days=30)).isoformat(),
    }

    details = _create_record(company, "Quotes", payload)
    quote_id = str(details["id"])
    quote_number = str(details.get("Quoted_Items") or details.get("id") or quote_id)

    # Prefer Quote_Number if available via a follow-up GET.
    try:
        get_resp = requests.get(
            f"{_crm_api_root(company)}/Quotes/{quote_id}",
            headers=_crm_headers(company),
            params={"fields": "Quote_Number,Subject"},
            timeout=30,
        )
        if get_resp.ok:
            record = ((get_resp.json() or {}).get("data") or [{}])[0]
            if record.get("Quote_Number"):
                quote_number = str(record["Quote_Number"])
    except Exception:
        logger.exception("Could not fetch Zoho quote number for %s", quote_id)

    quote_url = _quote_ui_url(company, quote_id)
    return quote_id, quote_number, quote_url


def new_oauth_state() -> str:
    return secrets.token_urlsafe(24)
