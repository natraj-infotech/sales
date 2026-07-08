"""Send subscription expiry emails to clients and sales team digests."""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from datetime import timedelta
from io import BytesIO

from django.utils import timezone
from openpyxl import Workbook

from accounts.models import CompanyMember, CompanyProfile
from business.models import ClientSubscription, SubscriptionExpiryNotice
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


def _format_subscription_line(sub: ClientSubscription) -> str:
    days = sub.days_until_expiry
    days_text = f"{days} day(s)" if days is not None else "—"
    vendor = sub.vendor.name if sub.vendor_id else "—"
    return (
        f"- {sub.client.name} | {sub.product.name} | ends {sub.end_date} "
        f"({days_text} remaining) | vendor: {vendor} | "
        f"subscription #{sub.subscription_list_id}"
    )


def _client_email_body(company: CompanyProfile, sub: ClientSubscription, days_before: int) -> str:
    contact_bits = [
        bit
        for bit in (
            (company.phone or "").strip(),
            (company.email or "").strip(),
            (company.outgoing_email or "").strip(),
        )
        if bit
    ]
    contact_line = "\n".join(contact_bits) if contact_bits else "Please reply to this email."
    return (
        f"Dear {sub.client.name},\n\n"
        f"This is a reminder that your subscription for {sub.product.name} "
        f"with {company.company_name} will expire on {sub.end_date} "
        f"({days_before} day(s) remaining).\n\n"
        f"Subscription reference: {sub.subscription_list_id}\n"
        f"Please contact us to renew and avoid interruption.\n\n"
        f"{company.company_name}\n"
        f"{contact_line}\n"
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
    grouped: dict[str, list[tuple[ClientSubscription, int]]] = defaultdict(list)

    for days_before in thresholds:
        for sub in _active_expiring_on_day(company, days_before, today):
            if SubscriptionExpiryNotice.objects.filter(
                subscription=sub,
                days_before=days_before,
            ).exists():
                continue
            client_email = (sub.client.email or "").strip()
            if not client_email:
                logs.append(
                    f"{company.company_name}: skipped subscription #{sub.pk} "
                    f"({days_before} days) — client has no email."
                )
                continue
            grouped[client_email].append((sub, days_before))

    for client_email, items in grouped.items():
        if len(items) == 1:
            sub, days_before = items[0]
            subject = (
                f"Subscription expiring in {days_before} days — {sub.product.name}"
            )
            body = _client_email_body(company, sub, days_before)
        else:
            subject = f"Subscriptions expiring soon — {company.company_name}"
            body_lines = [
                f"Dear {items[0][0].client.name},",
                "",
                "The following subscriptions will expire soon:",
                "",
            ]
            for sub, days_before in items:
                body_lines.append(
                    f"- {sub.product.name} — expires {sub.end_date} "
                    f"({days_before} day(s) remaining) — ref {sub.subscription_list_id}"
                )
            body_lines.extend(
                [
                    "",
                    "Please contact us to renew.",
                    "",
                    company.company_name,
                ]
            )
            body = "\n".join(body_lines)

        ok, err = send_company_outbound(company, [client_email], subject, body)
        if ok:
            sent_count += 1
            for sub, days_before in items:
                SubscriptionExpiryNotice.objects.get_or_create(
                    subscription=sub,
                    days_before=days_before,
                )
            logs.append(
                f"{company.company_name}: client reminder sent to {client_email} "
                f"({len(items)} subscription(s))."
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
