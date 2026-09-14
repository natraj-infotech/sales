"""OEM tickets, Email Diary contacts, and outbound OEM mail."""

from __future__ import annotations

import html
import logging
import re
from email.utils import parseaddr

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.utils import timezone
from helpdesk.models import FollowUp

from business.models import (
    ClientSubscription,
    OemDiaryCompany,
    OemDiaryEmail,
    Product,
    SupportTicketMeta,
)
from business.outbound_mail import send_outbound

logger = logging.getLogger(__name__)

MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
MAX_ATTACHMENTS = 8

_FONT = "'Segoe UI', Arial, Helvetica, sans-serif"
_TEXT = "#1e5bb8"
_MUTED = "#3b82c4"
_BLUE = "#2b6fd6"
_HEADER_BG = "#2b6fd6"
_BORDER = "#bfdbfe"
_PAGE_BG = "#eff6ff"
_CARD_BG = "#FFFFFF"


def normalize_company_name(name: str) -> str:
    return " ".join((name or "").split())


def _valid_email(raw: str) -> str:
    addr = (parseaddr(raw or "")[1] or (raw or "")).strip()
    if not addr:
        raise ValidationError("Email is required.")
    try:
        validate_email(addr)
    except ValidationError as exc:
        raise ValidationError(f"Invalid email: {addr}") from exc
    return addr


def parse_email_list(raw: str) -> list[str]:
    parts = re.split(r"[,\s;]+", raw or "")
    emails: list[str] = []
    seen: set[str] = set()
    for part in parts:
        token = part.strip()
        if not token:
            continue
        addr = _valid_email(token)
        key = addr.lower()
        if key in seen:
            continue
        seen.add(key)
        emails.append(addr)
    return emails


def oem_sender(company) -> tuple[str, str]:
    sender = (getattr(company, "oem_email", None) or "").strip()
    password = (getattr(company, "oem_email_app_password", None) or "").strip()
    if not sender:
        raise ValidationError(
            "Configure OEM email in Company profile before sending OEM tickets."
        )
    return sender, password


def _subscription_payload(sub: ClientSubscription) -> dict:
    product = sub.product
    client_name = sub.client.name if sub.client_id else ""
    product_name = product.name if product else ""
    sid = sub.subscription_list_id
    return {
        "subscription_id": sid,
        "pk": sub.pk,
        "client_name": client_name,
        "product_name": product_name,
        "oem_company": (product.company_name if product else "") or "",
        "oem_email": (product.company_email if product else "") or "",
        "serial_number": sub.serial_number or "",
        "label": " — ".join(part for part in (sid, client_name, product_name) if part),
    }


def lookup_subscription(company, subscription_id: str) -> dict | None:
    code = (subscription_id or "").strip()
    if not code:
        return None
    qs = ClientSubscription.objects.filter(company=company).select_related(
        "client", "product"
    )
    sub = qs.filter(subscription_code__iexact=code).first()
    if sub is None and code.isdigit():
        sub = qs.filter(pk=int(code)).first()
    if sub is None:
        return None
    return _subscription_payload(sub)


def subscription_options(company, limit: int = 400) -> list[dict]:
    qs = (
        ClientSubscription.objects.filter(company=company)
        .select_related("client", "product")
        .order_by("-created_at", "-id")[:limit]
    )
    return [_subscription_payload(sub) for sub in qs]


def _product_contacts(company) -> dict[str, dict]:
    grouped: dict[str, dict] = {}
    seen_by_name: dict[str, set[str]] = {}
    rows = (
        Product.objects.filter(company=company)
        .exclude(company_name="")
        .values_list("company_name", "company_email")
    )
    for raw_name, raw_email in rows:
        name = normalize_company_name(raw_name)
        if not name:
            continue
        key = name.lower()
        grouped.setdefault(key, {"name": name, "emails": []})
        seen = seen_by_name.setdefault(key, set())
        email = (raw_email or "").strip()
        if not email:
            continue
        try:
            email = _valid_email(email)
        except ValidationError:
            continue
        if email.lower() in seen:
            continue
        seen.add(email.lower())
        grouped[key]["emails"].append({"email": email, "source": "product", "id": None})
    return grouped


def diary_rows(company) -> list[dict]:
    grouped = _product_contacts(company)
    diary_companies = (
        OemDiaryCompany.objects.filter(company=company)
        .prefetch_related("emails")
        .order_by("name", "id")
    )
    for row in diary_companies:
        name = normalize_company_name(row.name)
        if not name:
            continue
        key = name.lower()
        bucket = grouped.setdefault(key, {"name": name, "emails": []})
        seen = {item["email"].lower() for item in bucket["emails"]}
        for item in row.emails.all():
            email = (item.email or "").strip()
            if not email or email.lower() in seen:
                continue
            seen.add(email.lower())
            bucket["emails"].append(
                {"email": email, "source": "diary", "id": item.pk}
            )
    rows = list(grouped.values())
    rows.sort(key=lambda r: r["name"].lower())
    return rows


def contact_options(company, limit: int = 400) -> list[dict]:
    """Quick OEM dropdown: products, plus company name/email if stored on the product."""
    products = Product.objects.filter(company=company).order_by("name", "id")[:limit]
    options: list[dict] = []
    for product in products:
        company_name = normalize_company_name(product.company_name)
        email = (product.company_email or "").strip()
        if email:
            try:
                email = _valid_email(email)
            except ValidationError:
                email = ""
        parts = [product.name]
        if company_name:
            parts.append(company_name)
        if email:
            parts.append(email)
        options.append(
            {
                "id": product.pk,
                "product_name": product.name,
                "name": company_name,
                "email": email,
                "source": "product",
                "label": " — ".join(parts),
            }
        )
    return options


def _get_or_create_diary_company(company, name: str) -> OemDiaryCompany:
    cleaned = normalize_company_name(name)
    if not cleaned:
        raise ValidationError("Company name is required.")
    existing = OemDiaryCompany.objects.filter(
        company=company, name__iexact=cleaned
    ).first()
    if existing:
        return existing
    return OemDiaryCompany.objects.create(company=company, name=cleaned)


def add_diary_company(company, name: str, email: str) -> OemDiaryCompany:
    addr = _valid_email(email)
    diary = _get_or_create_diary_company(company, name)
    add_diary_email(company, diary.name, addr)
    return diary


def add_diary_email(company, name: str, email: str) -> OemDiaryEmail:
    addr = _valid_email(email)
    diary = _get_or_create_diary_company(company, name)
    existing = diary.emails.filter(email__iexact=addr).first()
    if existing:
        return existing
    for item in diary_rows(company):
        if item["name"].lower() != diary.name.lower():
            continue
        if any(e["email"].lower() == addr.lower() for e in item["emails"]):
            raise ValidationError("That email is already listed for this company.")
    return OemDiaryEmail.objects.create(diary_company=diary, email=addr)


def collect_attachments(uploaded_files) -> list[tuple[str, bytes, str]]:
    attachments: list[tuple[str, bytes, str]] = []
    files = list(uploaded_files or [])[:MAX_ATTACHMENTS]
    for uploaded in files:
        name = (getattr(uploaded, "name", None) or "attachment")[:200]
        content_type = getattr(uploaded, "content_type", None) or "application/octet-stream"
        data = uploaded.read()
        if len(data) > MAX_ATTACHMENT_BYTES:
            raise ValidationError(f"{name} is larger than 10 MB.")
        if not data:
            continue
        attachments.append((name, data, content_type))
    return attachments


def _company_logo(company):
    logo_inline = None
    logo_cid = ""
    try:
        from business.expiry_notifications import _company_logo_inline

        logo_inline = _company_logo_inline(company)
        if logo_inline:
            logo_cid = logo_inline[0]
    except Exception:
        logger.warning(
            "Could not load company logo for OEM email company_id=%s",
            getattr(company, "pk", None),
            exc_info=True,
        )
        logo_inline = None
        logo_cid = ""
    return logo_inline, logo_cid


def _logo_html(company, logo_cid: str) -> str:
    company_name = html.escape(company.company_name or "Company")
    if logo_cid:
        return (
            f'<img src="cid:{html.escape(logo_cid)}" alt="{company_name}" width="120" '
            'style="display:block;max-width:140px;max-height:48px;width:auto;height:auto;'
            'object-fit:contain;background-color:#FFFFFF;border-radius:4px;padding:2px;">'
        )
    initial = html.escape((company.company_name or "C")[:1].upper())
    return (
        '<div style="width:44px;height:44px;border-radius:4px;background-color:#FFFFFF;'
        f'color:{_HEADER_BG};font-family:{_FONT};font-size:18px;font-weight:700;'
        'line-height:44px;text-align:center;">'
        f"{initial}</div>"
    )


def _attachments_html(names: list[str]) -> str:
    if not names:
        return ""
    chips = "".join(
        (
            '<span style="display:inline-block;margin:0 8px 8px 0;padding:6px 12px;'
            f'border:1px solid {_BORDER};border-radius:999px;background:#eff6ff;'
            f'font-family:{_FONT};font-size:12px;font-weight:600;color:{_BLUE};">'
            f"{html.escape(name)}</span>"
        )
        for name in names
    )
    return f'<div style="margin:18px 0 8px 0;">{chips}</div>'


def build_oem_email_text(
    company,
    *,
    subject: str,
    body: str,
    attachment_names: list[str] | None = None,
) -> str:
    lines = [
        "Hello,",
        "",
        (body or "").strip(),
        "",
    ]
    names = [name for name in (attachment_names or []) if name]
    if names:
        lines.append("Attachments:")
        lines.extend(f"- {name}" for name in names)
        lines.append("")
    lines.extend(
        [
            "Best Regards,",
            company.company_name or "Support team",
        ]
    )
    return "\n".join(lines)


def build_oem_email_html(
    company,
    *,
    subject: str,
    body: str,
    attachment_names: list[str] | None = None,
    logo_cid: str = "",
) -> str:
    company_name = html.escape(company.company_name or "Support")
    heading = html.escape((subject or "OEM support").strip() or "OEM support")
    message = html.escape(body or "").replace("\n", "<br>\n")
    names = [name for name in (attachment_names or []) if name]
    return f"""
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin:0;padding:0;background-color:{_PAGE_BG};">
  <tr>
    <td align="center" style="padding:24px 12px;background-color:{_PAGE_BG};">
      <table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0" style="width:600px;max-width:600px;background-color:{_CARD_BG};border:1px solid {_BORDER};border-radius:8px;">
        <tr>
          <td bgcolor="{_HEADER_BG}" style="background-color:{_HEADER_BG};padding:14px 18px;border-radius:8px 8px 0 0;">
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
              <tr>
                <td align="left" valign="middle">
                  {_logo_html(company, logo_cid)}
                </td>
                <td align="right" valign="middle" style="font-family:{_FONT};font-size:18px;font-weight:700;color:#FFFFFF;padding-left:12px;">
                  {company_name}
                </td>
              </tr>
            </table>
          </td>
        </tr>
        <tr>
          <td style="padding:28px 28px 8px 28px;font-family:{_FONT};">
            <p style="margin:0 0 6px 0;font-size:12px;letter-spacing:0.4px;text-transform:uppercase;color:{_BLUE};font-weight:600;">
              OEM SUPPORT
            </p>
            <h1 style="margin:0 0 16px 0;font-size:22px;line-height:1.3;color:{_BLUE};font-weight:700;font-family:{_FONT};">
              {heading}
            </h1>
            <p style="margin:0 0 12px 0;font-size:15px;line-height:1.55;color:{_TEXT};font-family:{_FONT};">
              Hello,
            </p>
            <p style="margin:0 0 8px 0;font-size:15px;line-height:1.55;color:{_TEXT};font-family:{_FONT};">
              {message}
            </p>
            {_attachments_html(names)}
            <p style="margin:16px 0 0 0;font-size:15px;line-height:1.55;color:{_TEXT};">
              Best Regards,<br>
              <strong style="color:{_BLUE};">{company_name}</strong>
            </p>
            <p style="margin:20px 0 0 0;padding-top:16px;border-top:1px solid {_BORDER};font-size:12px;line-height:1.5;color:{_MUTED};">
              This message was sent from the OEM support desk.
            </p>
          </td>
        </tr>
      </table>
    </td>
  </tr>
</table>
""".strip()


def send_oem_email(
    *,
    company,
    to_email: str,
    subject: str,
    body: str,
    cc_emails: list[str] | None = None,
    attachments: list[tuple[str, bytes, str]] | None = None,
) -> None:
    sender, password = oem_sender(company)
    to_addr = _valid_email(to_email)
    files = attachments or []
    names = [name for name, _data, _ctype in files]
    logo_inline, logo_cid = _company_logo(company)
    ok, error = send_outbound(
        sender,
        password,
        [to_addr],
        subject,
        build_oem_email_text(
            company, subject=subject, body=body, attachment_names=names
        ),
        html_body=build_oem_email_html(
            company,
            subject=subject,
            body=body,
            attachment_names=names,
            logo_cid=logo_cid,
        ),
        cc_addresses=cc_emails or [],
        attachments=files,
        inline_images=[logo_inline] if logo_inline else None,
    )
    if not ok:
        logger.error(
            "OEM email send failed company_id=%s to=%s error=%s",
            getattr(company, "pk", None),
            to_addr,
            error,
        )
        raise ValidationError(error or "Could not send the OEM email.")
    logger.info(
        "OEM email sent company_id=%s to=%s",
        getattr(company, "pk", None),
        to_addr,
    )


@transaction.atomic
def create_oem_ticket_and_send(
    *,
    company,
    user,
    subject: str,
    description: str,
    to_email: str,
    oem_company_name: str = "",
    cc_raw: str = "",
    subscription_id: str = "",
    attachments=None,
    transferred_from=None,
    assignee=None,
):
    from business.helpdesk_bridge import create_oem_ticket

    to_addr = _valid_email(to_email)
    subject = (subject or "").strip()
    description = (description or "").strip()
    if not subject:
        raise ValidationError("Subject is required.")
    if not description:
        raise ValidationError("Message is required.")
    cc_emails = parse_email_list(cc_raw) if cc_raw else []
    files = collect_attachments(attachments)
    if transferred_from is not None:
        source_meta = SupportTicketMeta.objects.filter(ticket=transferred_from).first()
        if source_meta and source_meta.transferred_to_id:
            raise ValidationError("This ticket has already been transferred to OEM.")
        if source_meta and source_meta.is_oem:
            raise ValidationError("OEM tickets cannot be transferred again.")
    ticket = create_oem_ticket(
        company=company,
        user=user,
        subject=subject,
        description=description,
        oem_company_name=normalize_company_name(oem_company_name),
        oem_to_email=to_addr,
        oem_cc_emails=", ".join(cc_emails),
        subscription_id=(subscription_id or "").strip()[:64],
        assignee=assignee,
    )
    send_oem_email(
        company=company,
        to_email=to_addr,
        subject=subject,
        body=description,
        cc_emails=cc_emails,
        attachments=files,
    )
    names = ", ".join(name for name, _data, _ctype in files)
    extra = f" Attachments: {names}." if names else ""
    FollowUp.objects.create(
        ticket=ticket,
        date=timezone.now(),
        title="OEM email sent",
        comment=f"Sent to {to_addr}.{extra}",
        public=False,
        user=user,
    )
    if transferred_from is not None:
        meta = SupportTicketMeta.objects.filter(ticket=transferred_from).first()
        if meta is None:
            from business.helpdesk_bridge import ensure_meta

            meta = ensure_meta(transferred_from, created_by=user, is_internal=True)
        meta.transferred_to = ticket
        meta.save(update_fields=["transferred_to", "updated_at"])
        FollowUp.objects.create(
            ticket=transferred_from,
            date=timezone.now(),
            title="Transferred to OEM",
            comment=f"Transferred to TK-{ticket.pk:05d}.",
            public=False,
            user=user,
        )
    return ticket
