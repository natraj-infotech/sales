"""Absolute URLs for public lead enquiry forms (WhatsApp CTA, etc.)."""

from __future__ import annotations

from django.conf import settings
from django.urls import reverse

from accounts.models import CompanyProfile

from .lead_public_tokens import sign_lead_public_token


def public_base_url() -> str:
    base = (getattr(settings, "PUBLIC_BASE_URL", "") or "").strip().rstrip("/")
    if base:
        return base
    return "http://127.0.0.1:8000"


def build_lead_public_enquiry_url(
    *,
    company: CompanyProfile | None,
    whatsapp_wa_id: str = "",
    contact_name: str = "",
) -> str:
    company_id = company.pk if company is not None else None
    token = sign_lead_public_token(
        company_id=company_id,
        whatsapp_wa_id=whatsapp_wa_id,
        contact_name=contact_name,
    )
    path = reverse("lead_public_enquiry", kwargs={"token": token})
    return f"{public_base_url()}{path}"
