"""Daily email to the team summarising pending support tickets (Excel attached)."""

from __future__ import annotations

import re
from html import escape
from io import BytesIO

from django.urls import reverse
from django.utils import timezone
from openpyxl import Workbook

from accounts.models import CompanyMember, CompanyProfile
from business import helpdesk_bridge
from business.expiry_notifications import (
    XLSX_MIME,
    _company_can_send_mail,
    _company_logo_inline,
    _public_base_url,
    parse_extra_emails,
)
from business.outbound_mail import send_company_outbound

PENDING_STATUSES = ("open", "in_progress")

EXCEL_HEADERS = (
    "Ticket",
    "Type",
    "Subject",
    "Status",
    "Priority",
    "Department",
    "Assignee",
    "Requester",
    "Created",
    "Days open",
)


def support_digest_recipients(company: CompanyProfile) -> list[str]:
    recipients: list[str] = []
    owner_email = (company.user.email or "").strip().lower()
    if owner_email:
        recipients.append(owner_email)
    company_email = (company.email or "").strip().lower()
    if company_email and company_email not in recipients:
        recipients.append(company_email)
    members = (
        CompanyMember.objects.filter(company=company, support_ticket_access=True)
        .select_related("user")
        .order_by("user__email")
    )
    for membership in members:
        email = (membership.user.email or "").strip().lower()
        if email and email not in recipients:
            recipients.append(email)
    for email in parse_extra_emails(company.expiry_team_extra_emails):
        if email not in recipients:
            recipients.append(email)
    return recipients


def _assignee_label(ticket) -> str:
    user = ticket.assignee
    if not user:
        return "Unassigned"
    name = (user.get_full_name() or "").strip()
    return name or user.email or "Unassigned"


def _requester_label(ticket) -> str:
    return (getattr(ticket.created_by, "email", "") or ticket.source_sender or "").strip()


def _days_open(ticket, today) -> int:
    created = ticket.created_at
    if not created:
        return 0
    return max((today - timezone.localtime(created).date()).days, 0)


def _display_date(value) -> str:
    if not value:
        return "—"
    return timezone.localtime(value).strftime("%d-%m-%Y")


_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
# The app is opened on the office LAN. Loopback links in mail do not open there.
_LAN_DIGEST_BASE = "http://192.168.1.147:8000"


def digest_link_base() -> str:
    base = _public_base_url()
    host = base.split("://", 1)[-1].split("/")[0].split(":")[0].lower()
    if host in _LOOPBACK_HOSTS:
        return _LAN_DIGEST_BASE
    return base


def ticket_detail_url(ticket) -> str:
    """Public ticket page. Anyone with the digest link can open it without login."""
    from business.team_notifications import company_for_ticket
    from business.ticket_close_tokens import sign_ticket_view_token

    raw = getattr(ticket, "_ticket", None) or ticket
    company = company_for_ticket(raw)
    if company is None:
        path = reverse("support_ticket_detail", args=[ticket.pk])
    else:
        token = sign_ticket_view_token(ticket.pk, company.pk)
        path = reverse("support_ticket_email_view", kwargs={"token": token})
    return f"{digest_link_base()}{path}"


def collect_ticket_summary(company: CompanyProfile, today) -> dict:
    tickets = helpdesk_bridge.wrap_tickets(helpdesk_bridge.tickets_for_company(company))
    pending = [t for t in tickets if t.status in PENDING_STATUSES]
    pending.sort(key=lambda t: t.created_at or timezone.now())
    return {
        "pending": pending,
        "pending_count": len(pending),
        "open_count": sum(1 for t in tickets if t.status == "open"),
        "in_progress_count": sum(1 for t in tickets if t.status == "in_progress"),
        "under_observation_count": sum(1 for t in tickets if t.status == "resolved"),
        "today": today,
    }


def _ticket_row(ticket, today) -> list:
    return [
        ticket.reference,
        "OEM" if ticket.is_oem else "Internal",
        ticket.subject or "",
        ticket.get_status_display(),
        ticket.get_priority_display(),
        ticket.get_department_display(),
        _assignee_label(ticket),
        _requester_label(ticket),
        _display_date(ticket.created_at),
        _days_open(ticket, today),
    ]


def build_support_digest_excel_bytes(summary: dict) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Pending Tickets"
    ws.append(list(EXCEL_HEADERS))
    for ticket in summary["pending"]:
        ws.append(_ticket_row(ticket, summary["today"]))
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def support_digest_attachment_filename(company: CompanyProfile, today) -> str:
    safe_name = re.sub(r"[^\w\-]+", "_", (company.company_name or "").strip())[:40]
    if not safe_name:
        safe_name = f"company_{company.pk}"
    return f"pending_support_tickets_{safe_name}_{today.isoformat()}.xlsx"


def support_digest_body(company: CompanyProfile, summary: dict, attachment_name: str) -> str:
    today = summary["today"]
    lines = [
        f"Daily support ticket digest for {company.company_name}",
        f"Date: {today.strftime('%d-%m-%Y')}",
        "",
        f"Pending tickets: {summary['pending_count']}",
        f"Open: {summary['open_count']}",
        f"In progress: {summary['in_progress_count']}",
        f"Under observation: {summary['under_observation_count']}",
        "",
        f"See the attached Excel file ({attachment_name}) for the full list.",
        "",
    ]
    for ticket in summary["pending"]:
        lines.append(
            f"{ticket.reference} | {ticket.subject or '—'} | "
            f"{ticket.get_status_display()} | {ticket.get_priority_display()} | "
            f"{_assignee_label(ticket)} | {_display_date(ticket.created_at)} | "
            f"{_days_open(ticket, today)} day(s) open | {ticket_detail_url(ticket)}"
        )
    lines.extend(["", "Manage tickets in Tracker365."])
    return "\n".join(lines)


def support_digest_html(
    company: CompanyProfile,
    summary: dict,
    attachment_name: str,
    *,
    logo_cid: str = "",
) -> str:
    font = "'Segoe UI', Arial, Helvetica, sans-serif"
    text = "#374151"
    muted = "#6B7280"
    blue = "#2563EB"
    header_bg = "#3B5998"
    border = "#E5E7EB"
    page_bg = "#F3F4F6"
    card_bg = "#FFFFFF"
    today = summary["today"]

    th = (
        f"padding:10px 8px;background-color:{header_bg};border:1px solid {border};"
        f"color:#FFFFFF;font-family:{font};font-size:11px;font-weight:600;"
        "text-transform:uppercase;letter-spacing:0.3px;"
    )
    td = (
        f"padding:0;border:1px solid {border};color:{text};"
        f"font-family:{font};font-size:13px;line-height:1.4;"
        f"vertical-align:top;background-color:{card_bg};"
    )
    link = (
        f"display:block;padding:10px 8px;color:{text};text-decoration:none;"
        f"font-family:{font};font-size:13px;line-height:1.4;"
    )
    id_link = (
        f"display:block;padding:10px 8px;color:{blue};text-decoration:underline;"
        f"font-family:{font};font-size:13px;line-height:1.4;font-weight:700;"
    )

    def linked_cell(
        href: str,
        inner: str,
        *,
        align: str = "left",
        extra: str = "",
        anchor: str = "",
    ) -> str:
        style = anchor or link
        return (
            f'<td style="{td}{extra}" align="{align}">'
            f'<a href="{href}" style="{style}text-align:{align};">{inner}</a>'
            f"</td>"
        )

    stat_cards = [
        ("Pending", summary["pending_count"], "#DC2626"),
        ("Open", summary["open_count"], blue),
        ("In progress", summary["in_progress_count"], "#D97706"),
        ("Under observation", summary["under_observation_count"], "#16A34A"),
    ]
    stats_html = "".join(
        f'<td align="center" style="padding:14px 8px;border:1px solid {border};'
        f'background-color:#F9FAFB;font-family:{font};">'
        f'<div style="font-size:24px;font-weight:700;color:{color};">{value}</div>'
        f'<div style="margin-top:4px;font-size:11px;font-weight:600;color:{muted};'
        f'text-transform:uppercase;letter-spacing:0.3px;">{escape(label)}</div></td>'
        for label, value, color in stat_cards
    )

    rows = []
    for ticket in summary["pending"]:
        days = _days_open(ticket, today)
        accent = "#DC2626" if days > 7 else ("#D97706" if days > 2 else "#16A34A")
        href = escape(ticket_detail_url(ticket))
        kind = "OEM" if ticket.is_oem else "Internal"
        rows.append(
            "<tr>"
            + linked_cell(
                href,
                f"{escape(ticket.reference)}<br>"
                f'<span style="color:{muted};font-size:11px;font-weight:400;'
                f'text-decoration:none;">{kind}</span>',
                extra="white-space:nowrap;",
                anchor=id_link,
            )
            + linked_cell(href, escape(ticket.subject or "—"))
            + linked_cell(href, escape(ticket.get_status_display()), align="center")
            + linked_cell(href, escape(ticket.get_priority_display()), align="center")
            + linked_cell(href, escape(_assignee_label(ticket)))
            + linked_cell(
                href,
                escape(_display_date(ticket.created_at)),
                align="center",
                extra="white-space:nowrap;",
            )
            + linked_cell(
                href,
                f'<span style="display:inline-block;padding:4px 8px;border:1px solid {accent};'
                f'border-radius:999px;color:{accent};white-space:nowrap;font-size:12px;">'
                f"{days} day(s)</span>",
                align="center",
            )
            + "</tr>"
        )
    if rows:
        table_html = (
            '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
            'style="border-collapse:collapse;width:100%;"><thead><tr>'
            f'<th align="left" style="{th}">Ticket</th>'
            f'<th align="left" style="{th}">Subject</th>'
            f'<th align="center" style="{th}">Status</th>'
            f'<th align="center" style="{th}">Priority</th>'
            f'<th align="left" style="{th}">Assignee</th>'
            f'<th align="center" style="{th}">Created</th>'
            f'<th align="center" style="{th}">Open for</th>'
            f"</tr></thead><tbody>{''.join(rows)}</tbody></table>"
        )
    else:
        table_html = (
            f'<p style="margin:0;font-family:{font};font-size:14px;color:{muted};">'
            "No pending tickets.</p>"
        )

    company_name = escape(company.company_name or "")
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
            f'line-height:44px;text-align:center;">{initial}</div>'
        )

    return f"""
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin:0;padding:0;background-color:{page_bg};">
  <tr>
    <td align="center" style="padding:24px 12px;background-color:{page_bg};">
      <table role="presentation" width="720" cellpadding="0" cellspacing="0" border="0" style="width:720px;max-width:720px;background-color:{card_bg};border:1px solid {border};border-radius:8px;">
        <tr>
          <td bgcolor="{header_bg}" style="background-color:{header_bg};padding:14px 18px;border-radius:8px 8px 0 0;">
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
              <tr>
                <td align="left" valign="middle">{logo_html}</td>
                <td align="right" valign="middle" style="font-family:{font};font-size:18px;font-weight:700;color:#FFFFFF;padding-left:12px;">{company_name}</td>
              </tr>
            </table>
          </td>
        </tr>
        <tr>
          <td style="padding:28px 24px 8px 24px;font-family:{font};">
            <p style="margin:0 0 6px 0;font-size:12px;letter-spacing:0.4px;text-transform:uppercase;color:{blue};font-weight:600;">Team digest</p>
            <h1 style="margin:0 0 16px 0;font-size:22px;line-height:1.3;color:{blue};font-weight:700;font-family:{font};">Daily support ticket digest</h1>
            <p style="margin:0 0 16px 0;font-size:15px;line-height:1.55;color:{text};">Date: <strong>{escape(today.strftime('%d-%m-%Y'))}</strong></p>
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="border-collapse:collapse;width:100%;margin:0 0 18px 0;">
              <tr>{stats_html}</tr>
            </table>
            <p style="margin:0 0 18px 0;font-size:14px;line-height:1.55;color:{muted};">See the attached Excel file ({escape(attachment_name)}) for the full list.</p>
          </td>
        </tr>
        <tr>
          <td style="padding:0 24px 8px 24px;">{table_html}</td>
        </tr>
        <tr>
          <td style="padding:16px 24px 28px 24px;font-family:{font};">
            <p style="margin:0;font-size:14px;line-height:1.55;color:{text};">Manage tickets in Tracker365.</p>
          </td>
        </tr>
      </table>
    </td>
  </tr>
</table>
""".strip()


def send_support_ticket_digest(
    company: CompanyProfile,
    today=None,
    *,
    force: bool = False,
) -> tuple[int, list[str]]:
    today = today or timezone.localdate()
    logs: list[str] = []

    if not force and not company.expiry_team_digest_enabled:
        return 0, logs
    if not force and company.support_digest_last_sent == today:
        return 0, logs
    if not _company_can_send_mail(company):
        logs.append(
            f"{company.company_name}: support digest skipped (outgoing SMTP not configured)."
        )
        return 0, logs

    recipients = support_digest_recipients(company)
    if not recipients:
        logs.append(f"{company.company_name}: support digest skipped (no recipient emails).")
        return 0, logs

    summary = collect_ticket_summary(company, today)
    attachment_name = support_digest_attachment_filename(company, today)
    excel_bytes = build_support_digest_excel_bytes(summary)
    logo_inline = _company_logo_inline(company)
    subject = (
        f"Pending support tickets ({summary['pending_count']}) — "
        f"{company.company_name} — {today.isoformat()}"
    )
    ok, err = send_company_outbound(
        company,
        recipients,
        subject,
        support_digest_body(company, summary, attachment_name),
        html_body=support_digest_html(
            company,
            summary,
            attachment_name,
            logo_cid=logo_inline[0] if logo_inline else "",
        ),
        attachments=[(attachment_name, excel_bytes, XLSX_MIME)],
        inline_images=[logo_inline] if logo_inline else None,
    )
    if ok:
        company.support_digest_last_sent = today
        company.save(update_fields=["support_digest_last_sent"])
        logs.append(
            f"{company.company_name}: support digest sent to {len(recipients)} recipient(s) "
            f"({summary['pending_count']} pending ticket(s), Excel attached)."
        )
        return 1, logs

    logs.append(f"{company.company_name}: failed to send support digest.")
    if err:
        logs.append(err)
    return 0, logs


def send_support_digest_test(company: CompanyProfile, to_email: str) -> tuple[bool, str | None]:
    """Send today's digest to one address. Does not mark the daily digest as sent."""
    today = timezone.localdate()
    if not _company_can_send_mail(company):
        return False, "Outgoing email is not configured."
    recipient = (to_email or "").strip()
    if not recipient:
        return False, "Test recipient is missing."
    summary = collect_ticket_summary(company, today)
    attachment_name = support_digest_attachment_filename(company, today)
    excel_bytes = build_support_digest_excel_bytes(summary)
    logo_inline = _company_logo_inline(company)
    subject = (
        f"Pending support tickets ({summary['pending_count']}) — "
        f"{company.company_name} — {today.isoformat()}"
    )
    return send_company_outbound(
        company,
        [recipient],
        subject,
        support_digest_body(company, summary, attachment_name),
        html_body=support_digest_html(
            company,
            summary,
            attachment_name,
            logo_cid=logo_inline[0] if logo_inline else "",
        ),
        attachments=[(attachment_name, excel_bytes, XLSX_MIME)],
        inline_images=[logo_inline] if logo_inline else None,
    )
