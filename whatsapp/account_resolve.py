"""Resolve WhatsApp credentials for webhooks (DB account or env token)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from django.conf import settings

from .models import WhatsAppAccount

if TYPE_CHECKING:
    from accounts.models import CompanyProfile

@dataclass
class EnvWhatsAppAccount:
    """Outbound credentials from .env when no WhatsAppAccount row exists."""

    phone_number_id: str
    company: CompanyProfile | None = None
    pk = None
    is_active = True
    label: str = "env-default"
    access_token: str = ""

    def resolved_access_token(self) -> str:
        return (
            (self.access_token or "").strip()
            or (getattr(settings, "WHATSAPP_ACCESS_TOKEN", "") or "").strip()
        )


WhatsAppAccountLike = WhatsAppAccount | EnvWhatsAppAccount


def resolve_lead_company(account: WhatsAppAccount | EnvWhatsAppAccount | None):
    """
    Optional company for a lead: from the WhatsAppAccount row, or WHATSAPP_DEFAULT_COMPANY_ID.
    Returns None when unassigned (leads may still be created without a company).
    """
    if account is None:
        return None
    co = getattr(account, "company", None)
    if co is not None:
        return co
    raw = (getattr(settings, "WHATSAPP_DEFAULT_COMPANY_ID", "") or "").strip()
    if not raw:
        return None
    try:
        from accounts.models import CompanyProfile

        return CompanyProfile.objects.filter(pk=int(raw)).first()
    except (TypeError, ValueError):
        return None


def resolve_webhook_account(
    phone_number_id: str | None,
) -> WhatsAppAccount | EnvWhatsAppAccount | None:
    """
    Prefer an active DB WhatsAppAccount; otherwise use WHATSAPP_ACCESS_TOKEN for
    the inbound phone_number_id (or WHATSAPP_DEFAULT_PHONE_NUMBER_ID).
    """
    pid = (phone_number_id or "").strip()
    if not pid:
        pid = (getattr(settings, "WHATSAPP_DEFAULT_PHONE_NUMBER_ID", "") or "").strip()
    if not pid:
        return None

    db = (
        WhatsAppAccount.objects.select_related("company")
        .filter(phone_number_id=pid, is_active=True)
        .first()
    )
    if db is not None:
        return db

    token = (getattr(settings, "WHATSAPP_ACCESS_TOKEN", "") or "").strip()
    if not token:
        return None

    return EnvWhatsAppAccount(phone_number_id=pid)


def account_log_fields(account: WhatsAppAccountLike | None) -> tuple:
    """(company, db WhatsAppAccount) for WhatsAppMessageLog rows."""
    if account is None:
        return None, None
    company = resolve_lead_company(account)
    db_account = account if isinstance(account, WhatsAppAccount) else None
    return company, db_account
