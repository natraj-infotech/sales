"""Send subscription expiry emails to clients and sales team digests."""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from datetime import timedelta
from html import escape
from io import BytesIO

from django.conf import settings
from django.db.models import Max
from django.urls import reverse
from django.utils import timezone
from openpyxl import Workbook

from accounts.models import CompanyMember, CompanyProfile
from business.models import (
    ClientSubscription,
    SubscriptionExpiryDispatch,
    SubscriptionExpiryNotice,
    SubscriptionRenewalInvite,
    SubscriptionRenewalInviteLine,
)
from business.outbound_mail import send_company_outbound
from business.reminder_schedule import (
    DEFAULT_END_PARAGRAPH,
    DEFAULT_INITIAL_PARAGRAPH,
    apply_merge_tags,
    in_schedule_window,
    matching_phase,
    merge_context,
    next_due_slot,
    now_for_today,
    resolve_email_copy,
    resolve_schedule,
    schedule_bounds,
)
from business.reminder_templates import company_reminder_bounds
from business.renewal_read_tracking import send_renewal_email

logger = logging.getLogger(__name__)

DEFAULT_NOTIFY_DAYS = (30, 15, 7)
DEFAULT_EXPIRY_INITIAL_PARAGRAPH = DEFAULT_INITIAL_PARAGRAPH
DEFAULT_EXPIRY_END_PARAGRAPH = DEFAULT_END_PARAGRAPH
VALID_NOTIFY_DAYS = {7, 15, 30, 45, 90}
DIGEST_EXPORT_HEADERS: tuple[str, ...] = (
    "Subscription ID",
    "Client",
    "Client Email",
    "Category",
    "Product",
    "Vendor",
    "Qty",
    "Start Date",
    "End Date",
    "Days Remaining",
    "Price",
    "Serial Number",
    "Invoice Number",
    "Status",
)
XLSX_MIME = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
)
DOUBLE_SEND_DAYS_REMAINING = 10


def _public_base_url() -> str:
    base = (getattr(settings, "PUBLIC_BASE_URL", "") or "").strip().rstrip("/")
    if base:
        return base
    return "http://127.0.0.1:8000"


def renewal_invite_absolute_url(invite: SubscriptionRenewalInvite) -> str:
    path = reverse("renewal_confirm_public", kwargs={"invite_id": str(invite.pk)})
    return f"{_public_base_url()}{path}"


def _create_renewal_invite(
    company: CompanyProfile,
    client_email: str,
    items: list[tuple[ClientSubscription, int, int | None, int]],
) -> SubscriptionRenewalInvite:
    invite = SubscriptionRenewalInvite.objects.create(
        company=company,
        client_email=client_email,
    )
    SubscriptionRenewalInviteLine.objects.bulk_create(
        [
            SubscriptionRenewalInviteLine(
                invite=invite,
                subscription=sub,
                company_name=sub.client.name,
                product_name=sub.product.name,
                current_quantity=sub.quantity,
            )
            for sub, _, _, _ in items
        ]
    )
    return invite


def parse_notify_days(raw: str) -> list[int]:
    days: list[int] = []
    for part in re.split(r"[,;\s]+", (raw or "").strip()):
        if not part:
            continue
        try:
            value = int(part)
        except ValueError:
            continue
        if value in VALID_NOTIFY_DAYS and value not in days:
            days.append(value)
    return sorted(days, reverse=True) or list(DEFAULT_NOTIFY_DAYS)


def parse_extra_emails(raw: str) -> list[str]:
    emails: list[str] = []
    for part in re.split(r"[,;\s\n]+", (raw or "").strip()):
        addr = part.strip().lower()
        if addr and addr not in emails:
            emails.append(addr)
    return emails


def _company_can_send_mail(company: CompanyProfile) -> bool:
    return bool(
        (company.outgoing_email or "").strip()
        and (company.outgoing_email_app_password or "").strip()
    )


def _team_recipients(company: CompanyProfile) -> list[str]:
    recipients: list[str] = []
    owner_email = (company.user.email or "").strip().lower()
    if owner_email:
        recipients.append(owner_email)
    company_email = (company.email or "").strip().lower()
    if company_email and company_email not in recipients:
        recipients.append(company_email)

    member_emails = (
        CompanyMember.objects.filter(company=company, sales_access=True)
        .select_related("user")
        .order_by("user__email")
    )
    for membership in member_emails:
        email = (membership.user.email or "").strip().lower()
        if email and email not in recipients:
            recipients.append(email)

    for email in parse_extra_emails(company.expiry_team_extra_emails):
        if email not in recipients:
            recipients.append(email)
    return recipients


def _client_reminder_cc_recipients(company: CompanyProfile) -> list[str]:
    """CC company owner and sales team on client renewal reminders."""
    recipients: list[str] = []

    owner_email = (company.user.email or "").strip().lower()
    if owner_email:
        recipients.append(owner_email)

    member_emails = (
        CompanyMember.objects.filter(company=company, sales_access=True)
        .select_related("user")
        .order_by("user__email")
    )
    for membership in member_emails:
        email = (membership.user.email or "").strip().lower()
        if email and email not in recipients:
            recipients.append(email)

    return recipients


def _merge_cc_addresses(to_email: str, *groups: list[str]) -> list[str]:
    seen = {(to_email or "").strip().lower()}
    seen.discard("")
    merged: list[str] = []
    for group in groups:
        for addr in group:
            key = (addr or "").strip().lower()
            if key and key not in seen:
                seen.add(key)
                merged.append(key)
    return merged


def _active_expiring_on_day(company: CompanyProfile, days_before: int, today):
    target_end = today + timedelta(days=days_before)
    return (
        company.subscriptions.filter(
            status=ClientSubscription.Status.ACTIVE,
            end_date=target_end,
        )
        .select_related("client", "product", "vendor")
        .order_by("client__name", "product__name")
    )


def _active_expiring_within(
    company: CompanyProfile, max_days: int, today, *, min_days: int = 0
):
    until = today + timedelta(days=max_days)
    since = today + timedelta(days=min_days)
    return (
        company.subscriptions.filter(
            status=ClientSubscription.Status.ACTIVE,
            end_date__isnull=False,
            end_date__gte=since,
            end_date__lte=until,
        )
        .select_related("client", "product", "vendor")
        .order_by("end_date", "client__name", "product__name")
    )


def _active_expiring_for_client_reminders(
    company: CompanyProfile, max_days: int, today, *, min_days: int = 0
):
    """Expiring active subscriptions that are still open for client reminder emails."""
    return _active_expiring_within(
        company, max_days, today, min_days=min_days
    ).exclude(
        deal_status__in=[
            ClientSubscription.DealStatus.WIN,
            ClientSubscription.DealStatus.LOST,
            ClientSubscription.DealStatus.CANCEL,
        ]
    )


_BLOCKING_DEAL_STATUSES = {
    ClientSubscription.DealStatus.WIN,
    ClientSubscription.DealStatus.LOST,
    ClientSubscription.DealStatus.CANCEL,
}


def _days_remaining_label(days_remaining: int | None) -> str:
    if days_remaining is None:
        return "no end date"
    if days_remaining < 0:
        return f"{abs(days_remaining)} days overdue"
    if days_remaining == 0:
        return "expires today"
    return f"{days_remaining} days remaining"


def _form_stopped(sub: ClientSubscription) -> bool:
    return bool(
        sub.end_date
        and sub.expiry_reminder_stopped_for_end_date
        and sub.expiry_reminder_stopped_for_end_date == sub.end_date
    )


def _next_manual_send_number(sub: ClientSubscription, threshold: int, today) -> int:
    """Next slot number for a manual send, counted separately from automatic ones."""
    highest = SubscriptionExpiryDispatch.objects.filter(
        subscription=sub,
        threshold_window=threshold,
        sent_on=today,
        source=SubscriptionExpiryDispatch.Source.MANUAL,
    ).aggregate(highest=Max("send_number"))["highest"]
    return (highest or 0) + 1


def _manual_skip_reason(client, subs, phases, today) -> str | None:
    """Why a renewal-email-enabled client gets no manual send. None = can send."""
    if not client.contact_emails():
        return "client has no email"
    if not subs:
        low, high = schedule_bounds(phases)
        return (
            "no active subscription in the reminder window "
            f"({high} to {low} days remaining)"
        )
    open_subs = [
        sub
        for sub in subs
        if (sub.deal_status or "") not in _BLOCKING_DEAL_STATUSES
        and not _form_stopped(sub)
    ]
    if not open_subs:
        if all(_form_stopped(sub) for sub in subs):
            return "renewal form already submitted for the current expiry"
        statuses = sorted(
            {
                (sub.get_deal_status_display() or sub.deal_status)
                for sub in subs
                if sub.deal_status
            }
        )
        label = " / ".join(statuses) if statuses else "closed"
        return f"marked {label} — client reminders are stopped"
    in_window = []
    outside = []
    for sub in open_subs:
        days = (sub.end_date - today).days if sub.end_date else None
        if days is None:
            continue
        if in_schedule_window(phases, days):
            in_window.append(days)
        else:
            outside.append(days)
    if in_window:
        return None
    if outside:
        sample = outside[0]
        low, high = schedule_bounds(phases)
        return (
            f"{_days_remaining_label(sample)}; reminder window is "
            f"{high} to {low} days remaining"
        )
    return "no dated active subscription to remind"


def _subscription_bill_type(sub: ClientSubscription) -> str:
    if sub.start_date and sub.end_date:
        term_days = (sub.end_date - sub.start_date).days
        if term_days >= 330:
            return "Annual"
        if term_days >= 25:
            return "Monthly"
    return "N/A"


def _client_contact_lines(company: CompanyProfile) -> list[str]:
    contact_bits: list[str] = []
    contact = (getattr(company, "regards_contact", "") or "").strip()
    if contact:
        contact_bits.append(contact)
    contact_bits.extend(parse_extra_emails(getattr(company, "regards_email", "") or ""))
    return contact_bits


def _client_reminder_subject(items: list[tuple[ClientSubscription, int, int | None]]) -> str:
    if len(items) == 1:
        sub, _, days_remaining = items[0]
        remaining_text = (
            f"{days_remaining} days" if days_remaining is not None else "soon"
        )
        return f"Subscription Renewal Reminder - {sub.product.name} expiring in {remaining_text}"
    max_threshold = max(notice_threshold for _, notice_threshold, _ in items)
    return (
        "Subscription Renewal Reminder - "
        f"Services expiring within {max_threshold} days"
    )


def _expiry_email_paragraph(company: CompanyProfile, attr: str, default: str) -> str:
    custom = (getattr(company, attr, None) or "").strip()
    return custom or default


def _expiry_email_paragraph_html(text: str) -> str:
    return escape(text).replace("\n", "<br>\n              ")


def _days_left_badge(days_remaining: int | None) -> tuple[str, str, str]:
    """Return (label, emoji, accent_hex) for days-left status."""
    if days_remaining is None:
        return "—", "⚪", "#6B7280"
    if days_remaining < 0:
        return f"{abs(days_remaining)} overdue", "🔴", "#DC2626"
    if days_remaining == 0:
        return "Expires today", "🔴", "#DC2626"
    if days_remaining <= 7:
        return f"{days_remaining} days", "🔴", "#DC2626"
    if days_remaining <= 15:
        return f"{days_remaining} days", "🟠", "#D97706"
    return f"{days_remaining} days", "🟢", "#16A34A"

def _client_email_body(
    company: CompanyProfile,
    sections: list[tuple[str, list[tuple[ClientSubscription, int, int | None]]]],
    *,
    renewal_url: str = "",
    initial_paragraph: str | None = None,
    end_paragraph: str | None = None,
) -> str:
    contact_bits = _client_contact_lines(company)
    all_items = [item for _, items in sections for item in items]
    threshold_days = max(notice_threshold for _, notice_threshold, _ in all_items)
    if initial_paragraph is None:
        initial_paragraph = _expiry_email_paragraph(
            company, "expiry_email_initial_paragraph", DEFAULT_EXPIRY_INITIAL_PARAGRAPH
        )
    if end_paragraph is None:
        end_paragraph = _expiry_email_paragraph(
            company, "expiry_email_end_paragraph", DEFAULT_EXPIRY_END_PARAGRAPH
        )
    lines = [
        "Dear Sir/Madam,",
        "",
        f"Your subscription is expiring within the next {threshold_days} days.",
        "",
        initial_paragraph,
        "",
    ]
    for client_name, items in sections:
        lines.extend(
            [
                f"Company Name: {client_name}",
                "",
                "+----------------------------+----------+------------+-----------+",
                "| Product Description        | Quantity | End Date   | Days Left |",
                "+----------------------------+----------+------------+-----------+",
            ]
        )
        for sub, _, days_remaining in items:
            days_text = "—" if days_remaining is None else str(days_remaining)
            lines.extend(
                [
                    f"| {sub.product.name[:26]:<26} | {str(sub.quantity):<8} | {sub.end_date.strftime('%d-%m-%Y'):<10} | {days_text:<9} |",
                    f"| Subscription ID: {sub.subscription_list_id[:13]:<13} |          |            |           |",
                    f"| Bill Type: {_subscription_bill_type(sub):<16} |          |            |           |",
                    "+----------------------------+----------+------------+-----------+",
                ]
            )
        lines.append("")
    lines.extend(
        [
            end_paragraph,
            "",
        ]
    )
    if renewal_url:
        lines.extend(
            [
                f"CONFIRM SUBSCRIPTIONS QTY: {renewal_url}",
                "",
            ]
        )
    lines.extend(
        [
            "So we can submit you the best and competitive commercial offer for the confirmed subscriptions Quantities.",
            "",
            "Thank you.",
            "",
            "Best Regards,",
            company.company_name,
        ]
    )
    lines.extend(contact_bits)
    return "\n".join(lines)

COMPANY_LOGO_CID = "company-logo"


def _company_logo_inline(company: CompanyProfile) -> tuple[str, str, bytes, str] | None:
    """Return (content_id, filename, bytes, mimetype) for the company logo, if available."""
    logo = getattr(company, "company_logo", None)
    if not logo or not getattr(logo, "name", ""):
        return None
    from pathlib import Path

    path = Path(settings.MEDIA_ROOT) / logo.name
    if not path.is_file():
        try:
            logo.open("rb")
            content = logo.read()
        except Exception:
            logger.exception("Failed reading company logo for company %s", company.pk)
            return None
        finally:
            try:
                logo.close()
            except Exception:
                pass
    else:
        content = path.read_bytes()
    if not content:
        return None
    name = path.name if path.is_file() else logo.name.rsplit("/", 1)[-1]
    lower = name.lower()
    if lower.endswith(".jpg") or lower.endswith(".jpeg"):
        mimetype = "image/jpeg"
    elif lower.endswith(".webp"):
        mimetype = "image/webp"
    elif lower.endswith(".gif"):
        mimetype = "image/gif"
    else:
        mimetype = "image/png"
    return COMPANY_LOGO_CID, name or "logo.png", content, mimetype


def _client_email_html(
    company: CompanyProfile,
    sections: list[tuple[str, list[tuple[ClientSubscription, int, int | None]]]],
    *,
    renewal_url: str = "",
    logo_cid: str = "",
    initial_paragraph: str | None = None,
    end_paragraph: str | None = None,
) -> str:
    all_items = [item for _, items in sections for item in items]
    threshold_days = max(notice_threshold for _, notice_threshold, _ in all_items)

    font = "'Segoe UI', Arial, Helvetica, sans-serif"
    text = "#374151"
    muted = "#6B7280"
    blue = "#2563EB"
    header_bg = "#3B5998"
    table_header_bg = header_bg
    border = "#E5E7EB"
    page_bg = "#F3F4F6"
    card_bg = "#FFFFFF"

    section_blocks: list[str] = []
    for client_name, items in sections:
        rows: list[str] = []
        for sub, _, days_remaining in items:
            label, emoji, accent = _days_left_badge(days_remaining)
            rows.append(
                """
                <tr>
                  <td style="padding:14px 12px;border:1px solid {border};color:{text};font-family:{font};font-size:14px;line-height:1.45;vertical-align:top;background-color:{card_bg};">
                    <strong style="color:{text};">{product}</strong><br>
                    <span style="color:{muted};font-size:12px;">Subscription ID: {subscription_id}</span><br>
                    <span style="color:{muted};font-size:12px;">Bill Type: {bill_type}</span>
                  </td>
                  <td style="padding:14px 12px;border:1px solid {border};color:{text};font-family:{font};font-size:14px;vertical-align:middle;background-color:{card_bg};" align="center">
                    {quantity}
                  </td>
                  <td style="padding:14px 12px;border:1px solid {border};color:{text};font-family:{font};font-size:14px;vertical-align:middle;background-color:{card_bg};" align="center">
                    {end_date}
                  </td>
                  <td style="padding:14px 12px;border:1px solid {border};font-family:{font};font-size:13px;vertical-align:middle;background-color:{card_bg};" align="center">
                    <span style="display:inline-block;padding:6px 10px;border:1px solid {accent};border-radius:999px;color:{accent};background-color:#FFFFFF;white-space:nowrap;">
                      {emoji} {label}
                    </span>
                  </td>
                </tr>
                """.format(
                    border=border,
                    text=text,
                    font=font,
                    muted=muted,
                    accent=accent,
                    card_bg=card_bg,
                    product=escape(sub.product.name),
                    subscription_id=escape(sub.subscription_list_id),
                    bill_type=escape(_subscription_bill_type(sub)),
                    quantity=escape(str(sub.quantity)),
                    end_date=escape(sub.end_date.strftime("%d-%m-%Y")),
                    emoji=emoji,
                    label=escape(label),
                ).strip()
            )

        section_blocks.append(
            """
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin:0 0 20px 0;background-color:{card_bg};border:1px solid {border};border-radius:6px;">
  <tr>
    <td style="padding:16px 18px 10px 18px;font-family:{font};">
      <p style="margin:0;font-size:15px;line-height:1.4;color:{blue};">
        <strong style="color:{muted};">🏢 Company Name:</strong>
        <strong style="color:{blue};">{client_name}</strong>
      </p>
    </td>
  </tr>
  <tr>
    <td style="padding:0 18px 18px 18px;">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="border-collapse:collapse;width:100%;">
        <thead>
          <tr>
            <th align="left" style="padding:12px;background-color:{table_header_bg};border:1px solid {border};color:#FFFFFF;font-family:{font};font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:0.3px;">Product Description</th>
            <th align="center" style="padding:12px;background-color:{table_header_bg};border:1px solid {border};color:#FFFFFF;font-family:{font};font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:0.3px;">Qty</th>
            <th align="center" style="padding:12px;background-color:{table_header_bg};border:1px solid {border};color:#FFFFFF;font-family:{font};font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:0.3px;">End Date</th>
            <th align="center" style="padding:12px;background-color:{table_header_bg};border:1px solid {border};color:#FFFFFF;font-family:{font};font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:0.3px;">Days Left</th>
          </tr>
        </thead>
        <tbody>
          {rows}
        </tbody>
      </table>
    </td>
  </tr>
</table>
""".format(
                card_bg=card_bg,
                border=border,
                font=font,
                blue=blue,
                muted=muted,
                table_header_bg=table_header_bg,
                client_name=escape(client_name),
                rows="\n".join(rows),
            ).strip()
        )

    contact_bits = _client_contact_lines(company)
    contact_html = ""
    if contact_bits:
        contact_html = (
            "<br>\n              "
            + "<br>\n              ".join(escape(bit) for bit in contact_bits)
        )

    cta_html = ""
    if renewal_url:
        cta_html = """
              <table role="presentation" cellpadding="0" cellspacing="0" border="0" style="margin:8px 0 20px 0;">
                <tr>
                  <td align="center" bgcolor="{header_bg}" style="background-color:{header_bg};border-radius:4px;">
                    <a href="{renewal_url}"
                       style="display:inline-block;padding:12px 22px;font-family:{font};font-size:14px;font-weight:600;color:#FFFFFF;text-decoration:none;">
                      CONFIRM SUBSCRIPTIONS QTY
                    </a>
                  </td>
                </tr>
              </table>
""".format(
            header_bg=header_bg,
            font=font,
            renewal_url=escape(renewal_url),
        )

    company_name = escape(company.company_name)
    if logo_cid:
        logo_html = (
            f'<img src="cid:{escape(logo_cid)}" alt="{company_name}" width="120" '
            'style="display:block;max-width:140px;max-height:48px;width:auto;height:auto;'
            'object-fit:contain;background-color:#FFFFFF;border-radius:4px;padding:2px;">'
        )
    else:
        initial = escape((company.company_name or "C")[:1].upper())
        logo_html = (
            '<div style="width:44px;height:44px;border-radius:4px;background-color:#FFFFFF;'
            f'color:{header_bg};font-family:{font};font-size:18px;font-weight:700;'
            'line-height:44px;text-align:center;">'
            f"{initial}</div>"
        )

    return """
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin:0;padding:0;background-color:{page_bg};">
  <tr>
    <td align="center" style="padding:24px 12px;background-color:{page_bg};">
      <table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0" style="width:600px;max-width:600px;background-color:{card_bg};border:1px solid {border};border-radius:8px;">
        <tr>
          <td bgcolor="{header_bg}" style="background-color:{header_bg};padding:14px 18px;border-radius:8px 8px 0 0;">
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
              <tr>
                <td align="left" valign="middle">
                  {logo_html}
                </td>
                <td align="right" valign="middle" style="font-family:{font};font-size:18px;font-weight:700;color:#FFFFFF;padding-left:12px;">
                  {company_name}
                </td>
              </tr>
            </table>
          </td>
        </tr>
        <tr>
          <td style="padding:28px 28px 8px 28px;font-family:{font};">
            <p style="margin:0 0 6px 0;font-size:12px;letter-spacing:0.4px;text-transform:uppercase;color:{blue};font-weight:600;">
              🔔 Renewal reminder
            </p>
            <h1 style="margin:0 0 16px 0;font-size:22px;line-height:1.3;color:{blue};font-weight:700;font-family:{font};">
              Subscription expiry notice
            </h1>
            <p style="margin:0 0 12px 0;font-size:15px;line-height:1.55;color:{text};font-family:{font};">
              Dear Sir/Madam,
            </p>
            <p style="margin:0 0 12px 0;font-size:15px;line-height:1.55;color:{text};font-family:{font};">
              Your subscription is expiring within the next <strong style="color:{blue};">{threshold_days} days</strong>.
            </p>
            <p style="margin:0 0 22px 0;font-size:15px;line-height:1.55;color:{text};font-family:{font};">
              {initial_paragraph}
            </p>
          </td>
        </tr>
        <tr>
          <td style="padding:0 28px;">
            {sections}
          </td>
        </tr>
        <tr>
          <td style="padding:8px 28px 28px 28px;font-family:{font};">
            <p style="margin:0 0 12px 0;font-size:15px;line-height:1.55;color:{text};">
              {end_paragraph}
            </p>
            {cta_html}
            <p style="margin:0 0 12px 0;font-size:15px;line-height:1.55;color:{text};">
              So we can submit you the best and competitive commercial offer for the confirmed subscriptions Quantities.
            </p>
            <p style="margin:0 0 4px 0;font-size:15px;line-height:1.55;color:{text};">
              Thank you.
            </p>
            <p style="margin:16px 0 0 0;font-size:15px;line-height:1.55;color:{text};">
              Best Regards,<br>
              <strong style="color:{blue};">{company_name}</strong>{contact_html}
            </p>
            <p style="margin:20px 0 0 0;padding-top:16px;border-top:1px solid {border};font-size:12px;line-height:1.5;color:{muted};">
              🟢 &gt;15 days &nbsp;&nbsp; 🟠 8–15 days &nbsp;&nbsp; 🔴 ≤7 days
            </p>
          </td>
        </tr>
      </table>
    </td>
  </tr>
</table>
""".strip().format(
        page_bg=page_bg,
        card_bg=card_bg,
        border=border,
        font=font,
        blue=blue,
        text=text,
        muted=muted,
        header_bg=header_bg,
        logo_html=logo_html,
        company_name=company_name,
        threshold_days=escape(str(threshold_days)),
        initial_paragraph=_expiry_email_paragraph_html(
            initial_paragraph
            if initial_paragraph is not None
            else _expiry_email_paragraph(
                company,
                "expiry_email_initial_paragraph",
                DEFAULT_EXPIRY_INITIAL_PARAGRAPH,
            )
        ),
        end_paragraph=_expiry_email_paragraph_html(
            end_paragraph
            if end_paragraph is not None
            else _expiry_email_paragraph(
                company,
                "expiry_email_end_paragraph",
                DEFAULT_EXPIRY_END_PARAGRAPH,
            )
        ),
        sections="\n".join(section_blocks),
        cta_html=cta_html,
        contact_html=contact_html,
    )


RENEWAL_CONFIRMATION_SUBJECT = "Subscription Renewal Confirmation"
RENEWAL_CONFIRMATION_OFFER_LINE = (
    "We will shortly submit you the best and competitive commercial offer for the same."
)


def _renewal_confirmation_cc_recipients(
    invite: SubscriptionRenewalInvite, to_email: str
) -> list[str]:
    seen = {(to_email or "").strip().lower()}
    seen.discard("")
    extra: list[str] = []
    lines = invite.lines.select_related("subscription__client").all()
    for line in lines:
        client = getattr(line.subscription, "client", None)
        if client is None:
            continue
        for addr in client.contact_emails():
            key = (addr or "").strip().lower()
            if key and key not in seen:
                seen.add(key)
                extra.append(addr.strip())
    return extra


def _renewal_confirmation_body(company: CompanyProfile, lines) -> str:
    contact_bits = _client_contact_lines(company)
    rows = [
        "Dear Sir/Madam,",
        "",
        RENEWAL_CONFIRMATION_OFFER_LINE,
        "",
        "+--------------------------------+------------------------------+----------+----------+",
        "| Company name                   | Product                      | Current  | Renewal  |",
        "+--------------------------------+------------------------------+----------+----------+",
    ]
    for line in lines:
        company_name = (line.company_name or "")[:30]
        product_name = (line.product_name or "")[:28]
        current_qty = str(line.current_quantity)
        renewal_qty = "—" if line.renewal_count is None else str(line.renewal_count)
        rows.append(
            f"| {company_name:<30} | {product_name:<28} | {current_qty:<8} | {renewal_qty:<8} |"
        )
    rows.extend(
        [
            "+--------------------------------+------------------------------+----------+----------+",
            "",
            "Thank you.",
            "",
            "Best Regards,",
            company.company_name,
        ]
    )
    rows.extend(contact_bits)
    return "\n".join(rows)


def _renewal_confirmation_html(
    company: CompanyProfile,
    lines,
    *,
    logo_cid: str = "",
) -> str:
    font = "'Segoe UI', Arial, Helvetica, sans-serif"
    text = "#374151"
    muted = "#6B7280"
    blue = "#2563EB"
    header_bg = "#3B5998"
    table_header_bg = header_bg
    border = "#E5E7EB"
    page_bg = "#F3F4F6"
    card_bg = "#FFFFFF"

    table_rows: list[str] = []
    for line in lines:
        renewal_qty = "—" if line.renewal_count is None else str(line.renewal_count)
        table_rows.append(
            """
                <tr>
                  <td style="padding:14px 12px;border:1px solid {border};color:{text};font-family:{font};font-size:14px;vertical-align:top;background-color:{card_bg};">
                    {company_name}
                  </td>
                  <td style="padding:14px 12px;border:1px solid {border};color:{text};font-family:{font};font-size:14px;vertical-align:top;background-color:{card_bg};">
                    {product_name}
                  </td>
                  <td style="padding:14px 12px;border:1px solid {border};color:{text};font-family:{font};font-size:14px;vertical-align:middle;background-color:{card_bg};" align="center">
                    {current_qty}
                  </td>
                  <td style="padding:14px 12px;border:1px solid {border};color:{text};font-family:{font};font-size:14px;font-weight:600;vertical-align:middle;background-color:{card_bg};" align="center">
                    {renewal_qty}
                  </td>
                </tr>
            """.format(
                border=border,
                text=text,
                font=font,
                card_bg=card_bg,
                company_name=escape(line.company_name or ""),
                product_name=escape(line.product_name or ""),
                current_qty=escape(str(line.current_quantity)),
                renewal_qty=escape(renewal_qty),
            ).strip()
        )

    contact_bits = _client_contact_lines(company)
    contact_html = ""
    if contact_bits:
        contact_html = (
            "<br>\n              "
            + "<br>\n              ".join(escape(bit) for bit in contact_bits)
        )

    company_name = escape(company.company_name)
    if logo_cid:
        logo_html = (
            f'<img src="cid:{escape(logo_cid)}" alt="{company_name}" width="120" '
            'style="display:block;max-width:140px;max-height:48px;width:auto;height:auto;'
            'object-fit:contain;background-color:#FFFFFF;border-radius:4px;padding:2px;">'
        )
    else:
        initial = escape((company.company_name or "C")[:1].upper())
        logo_html = (
            '<div style="width:44px;height:44px;border-radius:4px;background-color:#FFFFFF;'
            f'color:{header_bg};font-family:{font};font-size:18px;font-weight:700;'
            'line-height:44px;text-align:center;">'
            f"{initial}</div>"
        )

    return """
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin:0;padding:0;background-color:{page_bg};">
  <tr>
    <td align="center" style="padding:24px 12px;background-color:{page_bg};">
      <table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0" style="width:600px;max-width:600px;background-color:{card_bg};border:1px solid {border};border-radius:8px;">
        <tr>
          <td bgcolor="{header_bg}" style="background-color:{header_bg};padding:14px 18px;border-radius:8px 8px 0 0;">
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
              <tr>
                <td align="left" valign="middle">
                  {logo_html}
                </td>
                <td align="right" valign="middle" style="font-family:{font};font-size:18px;font-weight:700;color:#FFFFFF;padding-left:12px;">
                  {company_name}
                </td>
              </tr>
            </table>
          </td>
        </tr>
        <tr>
          <td style="padding:28px 28px 8px 28px;font-family:{font};">
            <p style="margin:0 0 6px 0;font-size:12px;letter-spacing:0.4px;text-transform:uppercase;color:{blue};font-weight:600;">
              Renewal confirmation
            </p>
            <p style="margin:0 0 12px 0;font-size:15px;line-height:1.55;color:{text};font-family:{font};">
              Dear Sir/Madam,
            </p>
            <p style="margin:0 0 22px 0;font-size:15px;line-height:1.55;color:{text};font-family:{font};">
              {offer_line}
            </p>
          </td>
        </tr>
        <tr>
          <td style="padding:0 28px 8px 28px;">
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="border-collapse:collapse;width:100%;margin:0 0 12px 0;">
              <thead>
                <tr>
                  <th align="left" style="padding:12px;background-color:{table_header_bg};border:1px solid {border};color:#FFFFFF;font-family:{font};font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:0.3px;">Company name</th>
                  <th align="left" style="padding:12px;background-color:{table_header_bg};border:1px solid {border};color:#FFFFFF;font-family:{font};font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:0.3px;">Product</th>
                  <th align="center" style="padding:12px;background-color:{table_header_bg};border:1px solid {border};color:#FFFFFF;font-family:{font};font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:0.3px;">Current qty</th>
                  <th align="center" style="padding:12px;background-color:{table_header_bg};border:1px solid {border};color:#FFFFFF;font-family:{font};font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:0.3px;">Renewal qty</th>
                </tr>
              </thead>
              <tbody>
                {table_rows}
              </tbody>
            </table>
          </td>
        </tr>
        <tr>
          <td style="padding:8px 28px 28px 28px;font-family:{font};">
            <p style="margin:0 0 4px 0;font-size:15px;line-height:1.55;color:{text};">
              Thank you.
            </p>
            <p style="margin:16px 0 0 0;font-size:15px;line-height:1.55;color:{text};">
              Best Regards,<br>
              <strong style="color:{blue};">{company_name}</strong>{contact_html}
            </p>
          </td>
        </tr>
      </table>
    </td>
  </tr>
</table>
""".strip().format(
        page_bg=page_bg,
        card_bg=card_bg,
        border=border,
        font=font,
        blue=blue,
        text=text,
        header_bg=header_bg,
        table_header_bg=table_header_bg,
        logo_html=logo_html,
        company_name=company_name,
        offer_line=escape(RENEWAL_CONFIRMATION_OFFER_LINE),
        table_rows="\n".join(table_rows),
        contact_html=contact_html,
    )


def send_renewal_confirmation_email(invite: SubscriptionRenewalInvite, lines=None) -> bool:
    """Email the client a copy of the quantities they just confirmed."""
    try:
        company = invite.company
        to_email = (invite.client_email or "").strip()
        if not to_email:
            logger.warning(
                "Skipped renewal confirmation email for invite %s: missing client email.",
                invite.pk,
            )
            return False
        if not _company_can_send_mail(company):
            logger.warning(
                "Skipped renewal confirmation email for invite %s: outgoing mail not configured.",
                invite.pk,
            )
            return False

        if lines is None:
            lines = list(invite.lines.order_by("id"))

        body = _renewal_confirmation_body(company, lines)
        logo_inline = _company_logo_inline(company)
        html_body = _renewal_confirmation_html(
            company,
            lines,
            logo_cid=logo_inline[0] if logo_inline else "",
        )
        cc_addresses = _renewal_confirmation_cc_recipients(invite, to_email)
        ok, err, _internet_message_id = send_renewal_email(
            company,
            invite,
            [to_email],
            RENEWAL_CONFIRMATION_SUBJECT,
            body,
            html_body,
            cc_addresses=cc_addresses,
            inline_images=[logo_inline] if logo_inline else None,
        )
    except Exception:
        logger.exception(
            "Failed sending renewal confirmation email for invite %s",
            invite.pk,
        )
        return False

    if not ok:
        logger.warning(
            "Renewal confirmation email failed for invite %s to %s: %s",
            invite.pk,
            to_email,
            err or "unknown error",
        )
        return False
    return True


def _digest_display_date(value) -> str:
    if not value:
        return "—"
    if hasattr(value, "strftime"):
        return value.strftime("%d-%m-%Y")
    return str(value)


def _team_digest_body(
    company: CompanyProfile, subs, today, *, max_days: int, attachment_name: str
) -> str:
    lines = [
        f"Daily expiring subscriptions digest for {company.company_name}",
        f"Date: {_digest_display_date(today)}",
        "",
        f"{len(subs)} active subscription(s) expiring within the next {max_days} day(s).",
        f"See the attached Excel file ({attachment_name}) for the full list.",
        "",
        "+---------------------------+---------------------------+------+------------+-----------+----------------+",
        "| Client                    | Product Description       | Qty  | End Date   | Days Left | Vendor         |",
        "+---------------------------+---------------------------+------+------------+-----------+----------------+",
    ]
    for sub in subs:
        client = (sub.client.name or "—")[:25]
        product = (sub.product.name or "—")[:25]
        qty = str(sub.quantity)
        end_date = _digest_display_date(sub.end_date)
        days = "—" if sub.days_until_expiry is None else str(sub.days_until_expiry)
        vendor = ((sub.vendor.name if sub.vendor_id else "—") or "—")[:14]
        sub_id = (sub.subscription_list_id or "—")[:21]
        bill_type = _subscription_bill_type(sub)[:19]
        lines.extend(
            [
                f"| {client:<25} | {product:<25} | {qty:<4} | {end_date:<10} | {days:<9} | {vendor:<14} |",
                f"| ID: {sub_id:<21} | Bill: {bill_type:<19} |      |            |           |                |",
                "+---------------------------+---------------------------+------+------------+-----------+----------------+",
            ]
        )
    lines.extend(
        [
            "",
            "Manage subscriptions in Tracker365.",
            "Green >15 days | Orange 8-15 days | Red <=7 days",
        ]
    )
    return "\n".join(lines)


def _team_digest_html(
    company: CompanyProfile,
    subs,
    today,
    *,
    max_days: int,
    attachment_name: str,
    logo_cid: str = "",
) -> str:
    font = "'Segoe UI', Arial, Helvetica, sans-serif"
    text = "#374151"
    muted = "#6B7280"
    blue = "#2563EB"
    header_bg = "#3B5998"
    table_header_bg = header_bg
    border = "#E5E7EB"
    page_bg = "#F3F4F6"
    card_bg = "#FFFFFF"

    th = (
        "padding:10px 8px;background-color:{bg};border:1px solid {border};"
        "color:#FFFFFF;font-family:{font};font-size:11px;font-weight:600;"
        "text-transform:uppercase;letter-spacing:0.3px;"
    ).format(bg=table_header_bg, border=border, font=font)
    td = (
        "padding:12px 8px;border:1px solid {border};color:{text};"
        "font-family:{font};font-size:13px;line-height:1.4;"
        "vertical-align:top;background-color:{card_bg};"
    ).format(border=border, text=text, font=font, card_bg=card_bg)

    rows: list[str] = []
    for sub in subs:
        label, emoji, accent = _days_left_badge(sub.days_until_expiry)
        vendor = sub.vendor.name if sub.vendor_id else "—"
        rows.append(
            """
            <tr>
              <td style="{td}">{client}</td>
              <td style="{td}">
                <strong style="color:{text};">{product}</strong><br>
                <span style="color:{muted};font-size:11px;">Subscription ID: {subscription_id}</span><br>
                <span style="color:{muted};font-size:11px;">Bill Type: {bill_type}</span>
              </td>
              <td style="{td}" align="center">{quantity}</td>
              <td style="{td}" align="center">{end_date}</td>
              <td style="{td}" align="center">
                <span style="display:inline-block;padding:5px 8px;border:1px solid {accent};border-radius:999px;color:{accent};background-color:#FFFFFF;white-space:nowrap;font-size:12px;">
                  {emoji} {label}
                </span>
              </td>
              <td style="{td}">{vendor}</td>
            </tr>
            """.format(
                td=td,
                text=text,
                muted=muted,
                accent=accent,
                client=escape(sub.client.name or "—"),
                product=escape(sub.product.name or "—"),
                subscription_id=escape(sub.subscription_list_id or "—"),
                bill_type=escape(_subscription_bill_type(sub)),
                quantity=escape(str(sub.quantity)),
                end_date=escape(_digest_display_date(sub.end_date)),
                emoji=emoji,
                label=escape(label),
                vendor=escape(vendor),
            ).strip()
        )

    company_name = escape(company.company_name)
    if logo_cid:
        logo_html = (
            f'<img src="cid:{escape(logo_cid)}" alt="{company_name}" width="120" '
            'style="display:block;max-width:140px;max-height:48px;width:auto;height:auto;'
            'object-fit:contain;background-color:#FFFFFF;border-radius:4px;padding:2px;">'
        )
    else:
        initial = escape((company.company_name or "C")[:1].upper())
        logo_html = (
            '<div style="width:44px;height:44px;border-radius:4px;background-color:#FFFFFF;'
            f'color:{header_bg};font-family:{font};font-size:18px;font-weight:700;'
            'line-height:44px;text-align:center;">'
            f"{initial}</div>"
        )

    return """
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin:0;padding:0;background-color:{page_bg};">
  <tr>
    <td align="center" style="padding:24px 12px;background-color:{page_bg};">
      <table role="presentation" width="720" cellpadding="0" cellspacing="0" border="0" style="width:720px;max-width:720px;background-color:{card_bg};border:1px solid {border};border-radius:8px;">
        <tr>
          <td bgcolor="{header_bg}" style="background-color:{header_bg};padding:14px 18px;border-radius:8px 8px 0 0;">
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
              <tr>
                <td align="left" valign="middle">
                  {logo_html}
                </td>
                <td align="right" valign="middle" style="font-family:{font};font-size:18px;font-weight:700;color:#FFFFFF;padding-left:12px;">
                  {company_name}
                </td>
              </tr>
            </table>
          </td>
        </tr>
        <tr>
          <td style="padding:28px 24px 8px 24px;font-family:{font};">
            <p style="margin:0 0 6px 0;font-size:12px;letter-spacing:0.4px;text-transform:uppercase;color:{blue};font-weight:600;">
              Team digest
            </p>
            <h1 style="margin:0 0 16px 0;font-size:22px;line-height:1.3;color:{blue};font-weight:700;font-family:{font};">
              Daily expiring subscriptions digest
            </h1>
            <p style="margin:0 0 8px 0;font-size:15px;line-height:1.55;color:{text};font-family:{font};">
              Date: <strong>{today}</strong>
            </p>
            <p style="margin:0 0 8px 0;font-size:15px;line-height:1.55;color:{text};font-family:{font};">
              <strong style="color:{blue};">{count}</strong> active subscription(s) expiring within the next <strong style="color:{blue};">{max_days} day(s)</strong>.
            </p>
            <p style="margin:0 0 22px 0;font-size:14px;line-height:1.55;color:{muted};font-family:{font};">
              See the attached Excel file ({attachment_name}) for the full list.
            </p>
          </td>
        </tr>
        <tr>
          <td style="padding:0 24px 8px 24px;">
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="border-collapse:collapse;width:100%;">
              <thead>
                <tr>
                  <th align="left" style="{th}">Client</th>
                  <th align="left" style="{th}">Product Description</th>
                  <th align="center" style="{th}">Qty</th>
                  <th align="center" style="{th}">End Date</th>
                  <th align="center" style="{th}">Days Left</th>
                  <th align="left" style="{th}">Vendor</th>
                </tr>
              </thead>
              <tbody>
                {rows}
              </tbody>
            </table>
          </td>
        </tr>
        <tr>
          <td style="padding:16px 24px 28px 24px;font-family:{font};">
            <p style="margin:0 0 4px 0;font-size:14px;line-height:1.55;color:{text};">
              Manage subscriptions in Tracker365.
            </p>
            <p style="margin:16px 0 0 0;padding-top:16px;border-top:1px solid {border};font-size:12px;line-height:1.5;color:{muted};">
              🟢 &gt;15 days &nbsp;&nbsp; 🟠 8–15 days &nbsp;&nbsp; 🔴 ≤7 days
            </p>
          </td>
        </tr>
      </table>
    </td>
  </tr>
</table>
""".strip().format(
        page_bg=page_bg,
        card_bg=card_bg,
        border=border,
        font=font,
        blue=blue,
        text=text,
        muted=muted,
        header_bg=header_bg,
        logo_html=logo_html,
        company_name=company_name,
        today=escape(_digest_display_date(today)),
        count=escape(str(len(subs))),
        max_days=escape(str(max_days)),
        attachment_name=escape(attachment_name),
        th=th,
        rows="\n".join(rows),
    )


def _digest_attachment_filename(company: CompanyProfile, today) -> str:
    safe_name = re.sub(r"[^\w\-]+", "_", (company.company_name or "").strip())[:40]
    if not safe_name:
        safe_name = f"company_{company.pk}"
    return f"expiring_subscriptions_{safe_name}_{today.isoformat()}.xlsx"


def build_expiry_digest_excel_bytes(
    subs: list[ClientSubscription],
) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Expiring Subscriptions"
    ws.append(list(DIGEST_EXPORT_HEADERS))
    for sub in subs:
        days = sub.days_until_expiry
        ws.append(
            [
                sub.subscription_list_id,
                sub.client.name,
                "; ".join(sub.client.contact_emails()),
                sub.product.category or "",
                sub.product.name,
                sub.vendor.name if sub.vendor_id else "",
                sub.quantity,
                sub.start_date.isoformat() if sub.start_date else "",
                sub.end_date.isoformat() if sub.end_date else "",
                days if days is not None else "",
                float(sub.billing_amount),
                sub.serial_number,
                sub.invoice_number,
                sub.get_status_display(),
            ]
        )
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def send_client_expiry_emails(
    company: CompanyProfile,
    today=None,
    *,
    force: bool = False,
) -> tuple[int, list[str]]:
    today = today or timezone.localdate()
    now = now_for_today(today)
    logs: list[str] = []
    sent_count = 0

    if not force and not company.expiry_client_emails_enabled:
        return 0, logs
    if not _company_can_send_mail(company):
        logs.append(
            f"{company.company_name}: client expiry emails skipped (outgoing SMTP not configured)."
        )
        return 0, logs

    min_days, max_days = company_reminder_bounds(company)
    min_days = min(min_days, -31)
    max_days = max(max_days, 90)
    cc_recipients = _client_reminder_cc_recipients(company)
    dispatch_source = (
        SubscriptionExpiryDispatch.Source.MANUAL
        if force
        else SubscriptionExpiryDispatch.Source.AUTOMATIC
    )
    grouped: dict[
        int, list[tuple[ClientSubscription, int, int | None, int]]
    ] = defaultdict(list)

    if force:
        on_clients = list(
            company.clients.filter(renewal_emails_enabled=True)
            .prefetch_related("subscriptions__product", "subscriptions__vendor")
            .order_by("name")
        )
        if not on_clients:
            logs.append(
                f"{company.company_name}: skipped client reminders — "
                "no clients have Renewal email turned on."
            )
        skipped_no_email: set[int] = set()
        for client in on_clients:
            phases = resolve_schedule(company, client)
            window_low, window_high = schedule_bounds(phases)
            subs = [
                sub
                for sub in client.subscriptions.all()
                if sub.status == ClientSubscription.Status.ACTIVE
                and sub.end_date is not None
                and window_low <= (sub.end_date - today).days <= window_high
            ]
            # Include out-of-window / blocked subs so skip reasons are accurate.
            related_subs = [
                sub
                for sub in client.subscriptions.all()
                if sub.status == ClientSubscription.Status.ACTIVE
                and sub.end_date is not None
            ]
            skip = _manual_skip_reason(client, related_subs, phases, today)
            if skip:
                logs.append(f"{company.company_name}: skipped {client.name} — {skip}.")
                continue
            for sub in subs:
                if (sub.deal_status or "") in _BLOCKING_DEAL_STATUSES:
                    continue
                if _form_stopped(sub):
                    continue
                days_remaining = (sub.end_date - today).days
                if not client.contact_emails():
                    if client.pk not in skipped_no_email:
                        skipped_no_email.add(client.pk)
                        logs.append(
                            f"{company.company_name}: skipped {client.name} "
                            f"({_days_remaining_label(days_remaining)}) — client has no email."
                        )
                    continue
                phase = matching_phase(phases, days_remaining)
                notice_threshold = (
                    int(phase["from_days"]) if phase else days_remaining
                )
                grouped[client.pk].append(
                    (
                        sub,
                        notice_threshold,
                        days_remaining,
                        _next_manual_send_number(sub, notice_threshold, today),
                    )
                )
            if client.pk not in grouped:
                logs.append(
                    f"{company.company_name}: skipped {client.name} — "
                    "no subscription is due for a reminder right now."
                )
    else:
        qs = _active_expiring_for_client_reminders(
            company, max_days, today, min_days=min_days
        )
        skipped_no_email = set()
        for sub in qs:
            client = sub.client
            if not getattr(client, "renewal_emails_enabled", True):
                continue
            if _form_stopped(sub):
                continue
            days_remaining = (sub.end_date - today).days if sub.end_date else None
            if days_remaining is None:
                continue
            if not client.contact_emails():
                if client.pk not in skipped_no_email:
                    skipped_no_email.add(client.pk)
                    logs.append(
                        f"{company.company_name}: skipped {client.name} "
                        f"({days_remaining} days remaining) — client has no email."
                    )
                continue
            phases = resolve_schedule(company, client)
            phase = matching_phase(phases, days_remaining)
            if phase is None:
                continue
            notice_threshold = int(phase["from_days"])
            # Manual sends are ignored here so a manual reminder never eats one of
            # today's scheduled slots.
            already_sent = set(
                SubscriptionExpiryDispatch.objects.filter(
                    subscription=sub,
                    threshold_window=notice_threshold,
                    sent_on=today,
                    source=SubscriptionExpiryDispatch.Source.AUTOMATIC,
                ).values_list("send_number", flat=True)
            )
            slot = next_due_slot(phase, days_remaining, now, already_sent)
            if slot is None:
                continue
            grouped[client.pk].append(
                (sub, notice_threshold, days_remaining, slot)
            )

    for _client_id, items in grouped.items():
        client = items[0][0].client
        contact_emails = client.contact_emails()
        client_email = contact_emails[0]
        extra_client_cc = contact_emails[1:]
        sections = [
            (
                client.name,
                [
                    (sub, threshold, days_remaining)
                    for sub, threshold, days_remaining, _ in items
                ],
            )
        ]
        body_items = [item for _, client_items in sections for item in client_items]
        copy = resolve_email_copy(company, client)
        invite = _create_renewal_invite(company, client_email, items)
        renewal_url = renewal_invite_absolute_url(invite)
        context = merge_context(
            company=company,
            client=client,
            items=body_items,
            renewal_url=renewal_url,
        )
        subject = apply_merge_tags(copy["subject"], context).strip() or (
            _client_reminder_subject(body_items)
        )
        initial_paragraph = apply_merge_tags(copy["initial"], context)
        end_paragraph = apply_merge_tags(copy["end"], context)
        body = _client_email_body(
            company,
            sections,
            renewal_url=renewal_url,
            initial_paragraph=initial_paragraph,
            end_paragraph=end_paragraph,
        )
        logo_inline = _company_logo_inline(company)
        html_body = _client_email_html(
            company,
            sections,
            renewal_url=renewal_url,
            logo_cid=logo_inline[0] if logo_inline else "",
            initial_paragraph=initial_paragraph,
            end_paragraph=end_paragraph,
        )
        inline_images = [logo_inline] if logo_inline else None

        cc_for_send = _merge_cc_addresses(
            client_email, extra_client_cc, cc_recipients
        )

        ok, err, internet_message_id = send_renewal_email(
            company,
            invite,
            [client_email],
            subject,
            body,
            html_body,
            cc_addresses=cc_for_send,
            inline_images=inline_images,
        )
        if ok:
            sent_count += 1
            invite.email_subject = subject
            invite.email_sent_at = timezone.now()
            invite.internet_message_id = internet_message_id or ""
            invite.save(
                update_fields=[
                    "email_subject",
                    "email_sent_at",
                    "internet_message_id",
                ]
            )
            for sub, notice_threshold, days_remaining, send_number in items:
                SubscriptionExpiryDispatch.objects.get_or_create(
                    subscription=sub,
                    threshold_window=notice_threshold,
                    sent_on=today,
                    send_number=send_number,
                    source=dispatch_source,
                )
                if notice_threshold >= 0:
                    SubscriptionExpiryNotice.objects.get_or_create(
                        subscription=sub,
                        days_before=notice_threshold,
                    )
            logs.append(
                f"{company.company_name}: client reminder sent to {client_email} "
                f"({len(items)} subscription(s))."
            )
        else:
            invite.delete()
            logs.append(
                f"{company.company_name}: failed to send client reminder to {client_email}."
            )
            if err:
                logs.append(err)

    return sent_count, logs


def send_team_expiry_digest(
    company: CompanyProfile,
    today=None,
    *,
    force: bool = False,
) -> tuple[int, list[str]]:
    today = today or timezone.localdate()
    logs: list[str] = []

    if not force and not company.expiry_team_digest_enabled:
        return 0, logs
    if not force and company.expiry_team_digest_last_sent == today:
        return 0, logs
    if not _company_can_send_mail(company):
        logs.append(
            f"{company.company_name}: team digest skipped (outgoing SMTP not configured)."
        )
        return 0, logs

    recipients = _team_recipients(company)
    if not recipients:
        logs.append(
            f"{company.company_name}: team digest skipped (no recipient emails)."
        )
        return 0, logs

    phases = resolve_schedule(company)
    min_days, max_days = company_reminder_bounds(company)
    subs = list(
        _active_expiring_within(company, max_days, today, min_days=min_days)
    )
    if not subs:
        company.expiry_team_digest_last_sent = today
        company.save(update_fields=["expiry_team_digest_last_sent"])
        logs.append(f"{company.company_name}: team digest skipped (nothing expiring soon).")
        return 0, logs

    subject = f"Expiring subscriptions — {company.company_name} — {today.isoformat()}"
    attachment_name = _digest_attachment_filename(company, today)
    excel_bytes = build_expiry_digest_excel_bytes(subs)
    body = _team_digest_body(
        company,
        subs,
        today,
        max_days=max_days,
        attachment_name=attachment_name,
    )
    logo_inline = _company_logo_inline(company)
    html_body = _team_digest_html(
        company,
        subs,
        today,
        max_days=max_days,
        attachment_name=attachment_name,
        logo_cid=logo_inline[0] if logo_inline else "",
    )
    ok, err = send_company_outbound(
        company,
        recipients,
        subject,
        body,
        html_body=html_body,
        attachments=[(attachment_name, excel_bytes, XLSX_MIME)],
        inline_images=[logo_inline] if logo_inline else None,
    )
    if ok:
        company.expiry_team_digest_last_sent = today
        company.save(update_fields=["expiry_team_digest_last_sent"])
        logs.append(
            f"{company.company_name}: team digest sent to {len(recipients)} recipient(s) "
            f"({len(subs)} subscription(s), Excel attached)."
        )
        return 1, logs

    logs.append(f"{company.company_name}: failed to send team digest.")
    if err:
        logs.append(err)
    return 0, logs


def send_expiry_notifications_for_company(
    company: CompanyProfile,
    today=None,
) -> dict[str, int | list[str]]:
    client_sent, client_logs = send_client_expiry_emails(company, today=today)
    team_sent, team_logs = send_team_expiry_digest(company, today=today)
    return {
        "client_emails_sent": client_sent,
        "team_digests_sent": team_sent,
        "logs": client_logs + team_logs,
    }


def send_expiry_notifications_for_all_companies(today=None) -> dict[str, int | list[str]]:
    client_total = 0
    team_total = 0
    logs: list[str] = []
    qs = CompanyProfile.objects.select_related("user").order_by("pk")
    for company in qs:
        if not (
            company.expiry_client_emails_enabled
            or company.expiry_team_digest_enabled
        ):
            continue
        result = send_expiry_notifications_for_company(company, today=today)
        client_total += int(result["client_emails_sent"])
        team_total += int(result["team_digests_sent"])
        logs.extend(result["logs"])
    return {
        "client_emails_sent": client_total,
        "team_digests_sent": team_total,
        "logs": logs[-50:],
    }
