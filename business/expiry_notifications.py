"""Send subscription expiry emails to clients and sales team digests."""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from datetime import timedelta
from html import escape
from io import BytesIO

from django.utils import timezone
from openpyxl import Workbook

from accounts.models import CompanyMember, CompanyProfile
from business.models import (
    ClientSubscription,
    SubscriptionExpiryDispatch,
    SubscriptionExpiryNotice,
)
from business.outbound_mail import send_company_outbound

logger = logging.getLogger(__name__)

DEFAULT_NOTIFY_DAYS = (30, 15, 7)
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
    recipients: list[str] = []

    owner_email = (company.user.email or "").strip().lower()
    if owner_email:
        recipients.append(owner_email)

    company_email = (company.email or "").strip().lower()
    if company_email and company_email not in recipients:
        recipients.append(company_email)

    member_emails = (
        CompanyMember.objects.filter(company=company)
        .select_related("user")
        .order_by("user__email")
    )
    for membership in member_emails:
        email = (membership.user.email or "").strip().lower()
        if email and email not in recipients:
            recipients.append(email)

    return recipients


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


def _active_expiring_within(company: CompanyProfile, max_days: int, today):
    until = today + timedelta(days=max_days)
    return (
        company.subscriptions.filter(
            status=ClientSubscription.Status.ACTIVE,
            end_date__isnull=False,
            end_date__gte=today,
            end_date__lte=until,
        )
        .select_related("client", "product", "vendor")
        .order_by("end_date", "client__name", "product__name")
    )


def _selected_threshold_window(days_remaining: int, thresholds: list[int]) -> int | None:
    eligible = sorted(threshold for threshold in thresholds if days_remaining <= threshold)
    return eligible[0] if eligible else None


def _max_daily_sends(days_remaining: int) -> int:
    return 2 if days_remaining <= DOUBLE_SEND_DAYS_REMAINING else 1


def _format_subscription_line(sub: ClientSubscription) -> str:
    days = sub.days_until_expiry
    days_text = f"{days} day(s)" if days is not None else "—"
    vendor = sub.vendor.name if sub.vendor_id else "—"
    return (
        f"- {sub.client.name} | {sub.product.name} | ends {sub.end_date} "
        f"({days_text} remaining) | vendor: {vendor} | "
        f"subscription #{sub.subscription_list_id}"
    )


def _subscription_bill_type(sub: ClientSubscription) -> str:
    if sub.start_date and sub.end_date:
        term_days = (sub.end_date - sub.start_date).days
        if term_days >= 330:
            return "Annual"
        if term_days >= 25:
            return "Monthly"
    return "N/A"


def _client_contact_lines(company: CompanyProfile) -> list[str]:
    contact_bits = [
        bit
        for bit in (
            (company.phone or "").strip(),
            (company.email or "").strip(),
            (company.outgoing_email or "").strip(),
        )
        if bit
    ]
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
def _days_left_badge(days_remaining: int | None) -> tuple[str, str, str]:
    """Return (label, emoji, accent_hex) for days-left status."""
    if days_remaining is None:
        return "—", "⚪", "#6B7280"
    if days_remaining <= 7:
        return f"{days_remaining} days", "🔴", "#DC2626"
    if days_remaining <= 15:
        return f"{days_remaining} days", "🟠", "#D97706"
    return f"{days_remaining} days", "🟢", "#16A34A"

def _client_email_body(
    company: CompanyProfile,
    sections: list[tuple[str, list[tuple[ClientSubscription, int, int | None]]]],
) -> str:
    contact_bits = _client_contact_lines(company)
    all_items = [item for _, items in sections for item in items]
    threshold_days = max(notice_threshold for _, notice_threshold, _ in all_items)
    lines = [
        "Dear Sir/Madam,",
        "",
        f"Your subscription is expiring within the next {threshold_days} days.",
        "",
        "Kindly renew it to ensure there is no interruption to your services.",
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
            "We request you to share your confirmation at the earliest so that we can "
            "initiate the renewal process and ensure uninterrupted service.",
            "",
            "Please feel free to contact us if you require any further information.",
            "",
            "Thank you.",
            "",
            "Best Regards,",
            company.company_name,
        ]
    )
    lines.extend(contact_bits)
    return "\n".join(lines)

def _client_email_html(
    company: CompanyProfile,
    sections: list[tuple[str, list[tuple[ClientSubscription, int, int | None]]]],
) -> str:
    all_items = [item for _, items in sections for item in items]
    threshold_days = max(notice_threshold for _, notice_threshold, _ in all_items)

    font = "'Segoe UI', Arial, Helvetica, sans-serif"
    text = "#374151"
    muted = "#6B7280"
    blue = "#2563EB"
    border = "#E5E7EB"
    header_bg = "#2563EB"
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
                  <td style="padding:14px 12px;border:1px solid {border};color:{text};font-family:{font};font-size:14px;line-height:1.45;vertical-align:top;">
                    <strong style="color:{text};">{product}</strong><br>
                    <span style="color:{muted};font-size:12px;">Subscription ID: {subscription_id}</span><br>
                    <span style="color:{muted};font-size:12px;">Bill Type: {bill_type}</span>
                  </td>
                  <td style="padding:14px 12px;border:1px solid {border};color:{text};font-family:{font};font-size:14px;vertical-align:middle;" align="center">
                    {quantity}
                  </td>
                  <td style="padding:14px 12px;border:1px solid {border};color:{text};font-family:{font};font-size:14px;vertical-align:middle;">
                    {end_date}
                  </td>
                  <td style="padding:14px 12px;border:1px solid {border};font-family:{font};font-size:13px;vertical-align:middle;" align="center">
                    <span style="display:inline-block;padding:6px 10px;border:1px solid {accent};border-radius:4px;color:{accent};background-color:#FFFFFF;white-space:nowrap;">
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
        <strong>🏢 Company Name:</strong> {client_name}
      </p>
    </td>
  </tr>
  <tr>
    <td style="padding:0 18px 18px 18px;">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="border-collapse:collapse;width:100%;">
        <thead>
          <tr>
            <th align="left" style="padding:12px;background-color:{header_bg};border:1px solid {border};color:#FFFFFF;font-family:{font};font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:0.3px;">Product Description</th>
            <th align="center" style="padding:12px;background-color:{header_bg};border:1px solid {border};color:#FFFFFF;font-family:{font};font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:0.3px;">Qty</th>
            <th align="left" style="padding:12px;background-color:{header_bg};border:1px solid {border};color:#FFFFFF;font-family:{font};font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:0.3px;">End Date</th>
            <th align="center" style="padding:12px;background-color:{header_bg};border:1px solid {border};color:#FFFFFF;font-family:{font};font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:0.3px;">Days Left</th>
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
                header_bg=header_bg,
                muted=muted,
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

    reply_to = (company.outgoing_email or company.email or "").strip()
    cta_html = ""
    if reply_to:
        cta_html = """
              <table role="presentation" cellpadding="0" cellspacing="0" border="0" style="margin:8px 0 20px 0;">
                <tr>
                  <td align="center" bgcolor="{blue}" style="background-color:{blue};border-radius:4px;">
                    <a href="mailto:{reply_to}?subject={subject}"
                       style="display:inline-block;padding:12px 22px;font-family:{font};font-size:14px;font-weight:600;color:#FFFFFF;text-decoration:none;">
                      ✉️ Confirm renewal
                    </a>
                  </td>
                </tr>
              </table>
""".format(
            blue=blue,
            font=font,
            reply_to=escape(reply_to),
            subject=escape("Subscription renewal confirmation"),
        )

    return """
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin:0;padding:0;background-color:{page_bg};">
  <tr>
    <td align="center" style="padding:24px 12px;background-color:{page_bg};">
      <table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0" style="width:600px;max-width:600px;background-color:{card_bg};border:1px solid {border};border-radius:8px;">
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
              Kindly renew it to ensure there is no interruption to your services.
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
              We request you to share your confirmation at the earliest so that we can initiate the renewal process and ensure uninterrupted service.
            </p>
            {cta_html}
            <p style="margin:0 0 12px 0;font-size:15px;line-height:1.55;color:{text};">
              Please feel free to contact us if you require any further information.
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
        threshold_days=escape(str(threshold_days)),
        sections="\n".join(section_blocks),
        cta_html=cta_html,
        company_name=escape(company.company_name),
        contact_html=contact_html,
    )

def _team_digest_body(
    company: CompanyProfile, subs, today, *, max_days: int, attachment_name: str
) -> str:
    lines = [
        f"Daily expiring subscriptions digest for {company.company_name}",
        f"Date: {today.isoformat()}",
        "",
        f"{len(subs)} active subscription(s) expiring within the next {max_days} day(s).",
        f"See the attached Excel file ({attachment_name}) for the full list.",
        "",
        "Summary:",
        "",
    ]
    lines.extend(_format_subscription_line(sub) for sub in subs[:10])
    if len(subs) > 10:
        lines.append(f"... and {len(subs) - 10} more in the attached spreadsheet.")
    lines.extend(
        [
            "",
            "Manage subscriptions in Tracker365.",
        ]
    )
    return "\n".join(lines)


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
                sub.client.email or "",
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
    logs: list[str] = []
    sent_count = 0

    if not force and not company.expiry_client_emails_enabled:
        return 0, logs
    if not _company_can_send_mail(company):
        logs.append(
            f"{company.company_name}: client expiry emails skipped (outgoing SMTP not configured)."
        )
        return 0, logs

    thresholds = parse_notify_days(company.expiry_notify_at_days)
    cc_recipients = _client_reminder_cc_recipients(company)
    grouped: dict[
        str, list[tuple[ClientSubscription, int, int | None, int]]
    ] = defaultdict(list)

    if force:
        max_days = max(thresholds)
        for sub in _active_expiring_within(company, max_days, today):
            days_remaining = sub.days_until_expiry
            if days_remaining is None:
                continue
            notice_threshold = _selected_threshold_window(days_remaining, thresholds)
            if notice_threshold is None:
                continue
            client_email = (sub.client.email or "").strip()
            if not client_email:
                logs.append(
                    f"{company.company_name}: skipped subscription #{sub.pk} "
                    f"({days_remaining} days remaining) — client has no email."
                )
                continue
            dispatch_count = SubscriptionExpiryDispatch.objects.filter(
                subscription=sub,
                threshold_window=notice_threshold,
                sent_on=today,
            ).count()
            grouped[client_email].append(
                (sub, notice_threshold, days_remaining, dispatch_count + 1)
            )
    else:
        max_days = max(thresholds)
        for sub in _active_expiring_within(company, max_days, today):
            days_remaining = sub.days_until_expiry
            if days_remaining is None:
                continue
            notice_threshold = _selected_threshold_window(days_remaining, thresholds)
            if notice_threshold is None:
                continue
            client_email = (sub.client.email or "").strip()
            if not client_email:
                logs.append(
                    f"{company.company_name}: skipped subscription #{sub.pk} "
                    f"({days_remaining} days remaining) — client has no email."
                )
                continue
            dispatch_count = SubscriptionExpiryDispatch.objects.filter(
                subscription=sub,
                threshold_window=notice_threshold,
                sent_on=today,
            ).count()
            if dispatch_count >= _max_daily_sends(days_remaining):
                continue
            grouped[client_email].append(
                (sub, notice_threshold, days_remaining, dispatch_count + 1)
            )

    for client_email, items in grouped.items():
        by_client: dict[int, list[tuple[ClientSubscription, int, int | None, int]]] = {}
        for sub, notice_threshold, days_remaining, send_number in items:
            by_client.setdefault(sub.client_id, []).append(
                (sub, notice_threshold, days_remaining, send_number)
            )

        sections = [
            (
                client_items[0][0].client.name,
                [
                    (sub, threshold, days_remaining)
                    for sub, threshold, days_remaining, _ in client_items
                ],
            )
            for client_items in by_client.values()
        ]
        body_items = [item for _, client_items in sections for item in client_items]
        subject = _client_reminder_subject(body_items)
        body = _client_email_body(company, sections)
        html_body = _client_email_html(company, sections)

        ok, err = send_company_outbound(
            company,
            [client_email],
            subject,
            body,
            html_body=html_body,
            cc_addresses=cc_recipients,
        )
        if ok:
            sent_count += 1
            for sub, notice_threshold, days_remaining, send_number in items:
                SubscriptionExpiryDispatch.objects.get_or_create(
                    subscription=sub,
                    threshold_window=notice_threshold,
                    sent_on=today,
                    send_number=send_number,
                )
                SubscriptionExpiryNotice.objects.get_or_create(
                    subscription=sub,
                    days_before=notice_threshold,
                )
            doubled = any(
                (days_remaining is not None and days_remaining <= DOUBLE_SEND_DAYS_REMAINING)
                for _, _, days_remaining, _ in items
            )
            cadence_note = " (double-send window)" if doubled else ""
            logs.append(
                f"{company.company_name}: client reminder sent to {client_email} "
                f"({len(items)} subscription(s)){cadence_note}."
            )
        else:
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

    thresholds = parse_notify_days(company.expiry_notify_at_days)
    max_days = max(thresholds)
    subs = list(_active_expiring_within(company, max_days, today))
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
    ok, err = send_company_outbound(
        company,
        recipients,
        subject,
        body,
        attachments=[(attachment_name, excel_bytes, XLSX_MIME)],
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
