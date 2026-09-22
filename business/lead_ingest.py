"""Helpers to create Lead records from WhatsApp, email, or manual entry."""

from __future__ import annotations

import logging
from typing import Any, Iterable

from django.db import IntegrityError, transaction
from django.db.models import Q

from accounts.models import CompanyProfile

from .models import Lead, Product

logger = logging.getLogger(__name__)


def _company_scope(company: CompanyProfile | None) -> Q:
    if company is None:
        return Q(company__isnull=True)
    return Q(company=company)


def _product_qs(company: CompanyProfile | None, product_ids: Iterable[int] | None):
    if not product_ids or company is None:
        return Product.objects.none()
    ids = [int(pk) for pk in product_ids if pk is not None]
    if not ids:
        return Product.objects.none()
    return Product.objects.filter(company=company, pk__in=ids)


@transaction.atomic
def create_lead(
    *,
    company: CompanyProfile | None,
    source: str,
    contact_name: str,
    company_name: str = "",
    email: str = "",
    phone: str = "",
    requirement: str = "",
    notes: str = "",
    product_ids: Iterable[int] | None = None,
    whatsapp_wa_id: str = "",
    whatsapp_message_id: str = "",
    email_message_id: str = "",
    raw_payload: dict[str, Any] | None = None,
    created_by=None,
) -> tuple[Lead, bool]:
    """
    Create a Lead or return the existing one when a source message id matches.

    Returns (lead, created).
    """
    contact_name = (contact_name or "").strip()
    if not contact_name:
        raise ValueError("contact_name is required")

    source = (source or Lead.Source.MANUAL).strip().lower()
    if source not in Lead.Source.values:
        raise ValueError(f"Invalid lead source: {source}")

    whatsapp_message_id = (whatsapp_message_id or "").strip()
    email_message_id = (email_message_id or "").strip()
    scope = _company_scope(company)

    if whatsapp_message_id:
        existing = Lead.objects.filter(scope, whatsapp_message_id=whatsapp_message_id).first()
        if existing:
            return existing, False

    if email_message_id:
        existing = Lead.objects.filter(scope, email_message_id=email_message_id).first()
        if existing:
            return existing, False

    try:
        lead = Lead.objects.create(
            company=company,
            created_by=created_by,
            source=source,
            contact_name=contact_name,
            company_name=(company_name or "").strip(),
            email=(email or "").strip(),
            phone=(phone or "").strip(),
            requirement=(requirement or "").strip(),
            notes=(notes or "").strip(),
            whatsapp_wa_id=(whatsapp_wa_id or "").strip(),
            whatsapp_message_id=whatsapp_message_id,
            email_message_id=email_message_id,
            raw_payload=raw_payload or {},
        )
    except IntegrityError:
        if whatsapp_message_id:
            existing = Lead.objects.filter(
                scope, whatsapp_message_id=whatsapp_message_id
            ).first()
            if existing:
                return existing, False
        if email_message_id:
            existing = Lead.objects.filter(scope, email_message_id=email_message_id).first()
            if existing:
                return existing, False
        raise

    products = list(_product_qs(company, product_ids))
    if products:
        lead.products.set(products)

    logger.info(
        "Lead created id=%s source=%s company_id=%s",
        lead.pk,
        lead.source,
        company.pk if company is not None else None,
    )
    return lead, True


def create_lead_from_whatsapp(
    *,
    company: CompanyProfile | None,
    contact_name: str,
    phone: str = "",
    company_name: str = "",
    email: str = "",
    requirement: str = "",
    product_ids: Iterable[int] | None = None,
    whatsapp_wa_id: str = "",
    whatsapp_message_id: str = "",
    raw_payload: dict[str, Any] | None = None,
) -> tuple[Lead, bool]:
    return create_lead(
        company=company,
        source=Lead.Source.WHATSAPP,
        contact_name=contact_name,
        company_name=company_name,
        email=email,
        phone=phone or whatsapp_wa_id,
        requirement=requirement,
        product_ids=product_ids,
        whatsapp_wa_id=whatsapp_wa_id,
        whatsapp_message_id=whatsapp_message_id,
        raw_payload=raw_payload,
    )


def create_lead_from_email(
    *,
    company: CompanyProfile | None,
    contact_name: str,
    email: str = "",
    phone: str = "",
    company_name: str = "",
    requirement: str = "",
    product_ids: Iterable[int] | None = None,
    email_message_id: str = "",
    raw_payload: dict[str, Any] | None = None,
) -> tuple[Lead, bool]:
    """Create a Lead from an inbound sales email (call from mailbox sync later)."""
    return create_lead(
        company=company,
        source=Lead.Source.EMAIL,
        contact_name=contact_name or (email or "Email lead"),
        company_name=company_name,
        email=email,
        phone=phone,
        requirement=requirement,
        product_ids=product_ids,
        email_message_id=email_message_id,
        raw_payload=raw_payload,
    )


def get_or_create_lead_by_whatsapp_id(
    *,
    company: CompanyProfile | None,
    whatsapp_wa_id: str,
    contact_name: str = "",
    phone: str = "",
    raw_payload: dict[str, Any] | None = None,
) -> tuple[Lead, bool]:
    """
    Find the latest Lead for this WhatsApp user, or create a minimal WhatsApp lead.
    Does not set whatsapp_message_id (conversation open is not a Flow submission).
    """
    wa_id = (whatsapp_wa_id or "").strip()
    if not wa_id:
        raise ValueError("whatsapp_wa_id is required")

    scope = _company_scope(company)
    existing = (
        Lead.objects.filter(scope, whatsapp_wa_id=wa_id)
        .order_by("-last_inbound_at", "-id")
        .first()
    )
    if existing:
        return existing, False

    name = (contact_name or "").strip() or wa_id
    return create_lead_from_whatsapp(
        company=company,
        contact_name=name,
        phone=(phone or wa_id).strip(),
        whatsapp_wa_id=wa_id,
        raw_payload=raw_payload,
    )
