"""Branded support-ticket notification emails (assign / requester / closed).

Uses django-helpdesk Ticket fields (submitter_email, assigned_to, status) for
recipients and ticket data. Outbound mail stays branded via company SMTP/logo
(helpdesk default templates are intentionally not used for branding).
"""

from __future__ import annotations

import logging
from html import escape

from django.core.exceptions import ObjectDoesNotExist

from accounts.models import CompanyProfile
from business.helpdesk_bridge import (
    DEPARTMENT_LABELS,
    PRIORITY_HD_TO_UI,
    PRIORITY_LABELS,
    STATUS_HD_TO_UI,
    STATUS_CHOICES,
)
from business.outbound_mail import send_outbound

logger = logging.getLogger(__name__)

_FONT = "'Segoe UI', Arial, Helvetica, sans-serif"
# Match Tracker365 / Ticket360 blue theme (not gray body text).
_TEXT = "#1e5bb8"
_MUTED = "#3b82c4"
_BLUE = "#2b6fd6"
_HEADER_BG = "#2b6fd6"
_BORDER = "#bfdbfe"
_PAGE_BG = "#eff6ff"
_CARD_BG = "#FFFFFF"

_CLOSED_MESSAGE = (
    "Hope your issue is resolved. Your ticket is closed now. "
    "Feel free to approach us for further issues."
)


def _smtp_credentials(company: CompanyProfile) -> tuple[str, str] | None:
    support = (company.support_email or "").strip()
    support_pw = (company.support_email_app_password or "").strip()
    if support and support_pw:
        return support, support_pw
    outgoing = (company.outgoing_email or "").strip()
    outgoing_pw = (company.outgoing_email_app_password or "").strip()
    if outgoing and outgoing_pw:
        return outgoing, outgoing_pw
    return None


def _ticket_ref(ticket) -> str:
    return f"TK-{ticket.pk:05d}"


def _status_label(ticket) -> str:
    ui = STATUS_HD_TO_UI.get(ticket.status, "open")
    return dict(STATUS_CHOICES).get(ui, ui.title())


def _priority_label(ticket) -> str:
    ui = PRIORITY_HD_TO_UI.get(ticket.priority, "medium")
    return PRIORITY_LABELS.get(ui, ui.title())


def _department_label(ticket) -> str:
    try:
        meta = ticket.crm_meta
    except ObjectDoesNotExist:
        meta = None
    dept = (meta.department if meta else "general") or "general"
    return DEPARTMENT_LABELS.get(dept, dept.title())


def _submitter_label(ticket) -> str:
    try:
        meta = ticket.crm_meta
    except ObjectDoesNotExist:
        meta = None
    if meta and meta.is_internal:
        if meta.created_by_id and meta.created_by:
            return meta.created_by.email or "Internal"
        return "Internal"
    return (ticket.submitter_email or "").strip() or "—"


def requester_email(ticket) -> str:
    """
    Email of the person who raised the ticket.

    Prefer django-helpdesk submitter_email (email-created tickets); fall back to
    CRM meta created_by for internal/manual tickets.
    """
    email = (getattr(ticket, "submitter_email", None) or "").strip()
    if email:
        return email
    try:
        meta = ticket.crm_meta
    except ObjectDoesNotExist:
        meta = None
    if meta and meta.created_by_id and meta.created_by:
        return (meta.created_by.email or "").strip()
    return ""


def _assignee_label(ticket) -> str:
    user = getattr(ticket, "assigned_to", None)
    if not user:
        return "—"
    return getattr(user, "email", None) or getattr(user, "username", None) or "—"


def _format_dt(value) -> str:
    from django.utils import timezone

    if not value:
        return "—"
    if timezone.is_aware(value):
        value = timezone.localtime(value)
    return value.strftime("%d-%m-%Y %H:%M")


def _logo_html(company: CompanyProfile, logo_cid: str = "") -> str:
    company_name = escape(company.company_name or "Company")
    if logo_cid:
        return (
            f'<img src="cid:{escape(logo_cid)}" alt="{company_name}" width="120" '
            'style="display:block;max-width:140px;max-height:48px;width:auto;height:auto;'
            'object-fit:contain;background-color:#FFFFFF;border-radius:4px;padding:2px;">'
        )
    initial = escape((company.company_name or "C")[:1].upper())
    return (
        '<div style="width:44px;height:44px;border-radius:4px;background-color:#FFFFFF;'
        f'color:{_HEADER_BG};font-family:{_FONT};font-size:18px;font-weight:700;'
        'line-height:44px;text-align:center;">'
        f"{initial}</div>"
    )


def _company_logo(company: CompanyProfile):
    logo_inline = None
    logo_cid = ""
    try:
        from business.expiry_notifications import _company_logo_inline

        logo_inline = _company_logo_inline(company)
        if logo_inline:
            logo_cid = logo_inline[0]
    except Exception:
        logger.exception("Could not load company logo for ticket email")
    return logo_inline, logo_cid


def _truncate_description(ticket) -> str:
    from business.email_body_clean import clean_inbound_email_body

    description = clean_inbound_email_body(
        ticket.description or "", empty_fallback=""
    ).strip()
    if len(description) > 400:
        return description[:397] + "..."
    return description


def _detail_rows_for_assignee(ticket, *, assigned_by) -> list[tuple[str, str]]:
    return [
        ("Ticket ID", _ticket_ref(ticket)),
        ("Subject", ticket.title or "—"),
        ("Priority", _priority_label(ticket)),
        ("Status", _status_label(ticket)),
        ("Department", _department_label(ticket)),
        ("Opened", _format_dt(ticket.created)),
        ("Raised by", _submitter_label(ticket)),
        ("Assigned by", (getattr(assigned_by, "email", None) or "—")),
        ("Assigned to", "YOU"),
        ("Description", _truncate_description(ticket) or "—"),
    ]


def _detail_rows_for_requester(ticket, *, assigned_by=None) -> list[tuple[str, str]]:
    rows = [
        ("Ticket ID", _ticket_ref(ticket)),
        ("Subject", ticket.title or "—"),
        ("Priority", _priority_label(ticket)),
        ("Status", _status_label(ticket)),
        ("Department", _department_label(ticket)),
        ("Opened", _format_dt(ticket.created)),
        ("Raised by", _submitter_label(ticket)),
        ("Assigned to", _assignee_label(ticket)),
    ]
    if assigned_by is not None:
        rows.insert(
            -1,
            ("Assigned by", (getattr(assigned_by, "email", None) or "—")),
        )
    rows.append(("Description", _truncate_description(ticket) or "—"))
    return rows


def _rows_html(rows: list[tuple[str, str]]) -> str:
    parts = []
    for label, value in rows:
        parts.append(
            f"""
            <tr>
              <td style="padding:12px;border:1px solid {_BORDER};background-color:#eff6ff;color:{_MUTED};font-family:{_FONT};font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:0.3px;width:34%;vertical-align:top;">
                {escape(label)}
              </td>
              <td style="padding:12px;border:1px solid {_BORDER};color:{_TEXT};font-family:{_FONT};font-size:14px;line-height:1.45;vertical-align:top;background-color:{_CARD_BG};">
                {escape(str(value)).replace(chr(10), "<br>")}
              </td>
            </tr>
            """.strip()
        )
    return "".join(parts)


def _text_table(rows: list[tuple[str, str]]) -> list[str]:
    lines = [
        "+------------------+------------------------------------------+",
        "| Field            | Value                                    |",
        "+------------------+------------------------------------------+",
    ]
    for label, value in rows:
        flat = " ".join(str(value).split())
        lines.append(f"| {label:<16} | {flat[:40]:<40} |")
    lines.append("+------------------+------------------------------------------+")
    return lines


def _branded_shell(
    *,
    company: CompanyProfile,
    logo_cid: str,
    eyebrow: str,
    heading: str,
    intro_html: str,
    rows: list[tuple[str, str]],
    body_html: str,
    footer: str,
) -> str:
    company_name = escape(company.company_name or "Support")
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
              {escape(eyebrow)}
            </p>
            <h1 style="margin:0 0 16px 0;font-size:22px;line-height:1.3;color:{_BLUE};font-weight:700;font-family:{_FONT};">
              {escape(heading)}
            </h1>
            {intro_html}
          </td>
        </tr>
        <tr>
          <td style="padding:0 28px 8px 28px;">
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="border-collapse:collapse;width:100%;margin:0 0 8px 0;">
              <thead>
                <tr>
                  <th align="left" style="padding:12px;background-color:{_HEADER_BG};border:1px solid {_BORDER};color:#FFFFFF;font-family:{_FONT};font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:0.3px;">Field</th>
                  <th align="left" style="padding:12px;background-color:{_HEADER_BG};border:1px solid {_BORDER};color:#FFFFFF;font-family:{_FONT};font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:0.3px;">Value</th>
                </tr>
              </thead>
              <tbody>
                {_rows_html(rows)}
              </tbody>
            </table>
          </td>
        </tr>
        <tr>
          <td style="padding:8px 28px 28px 28px;font-family:{_FONT};">
            {body_html}
            <p style="margin:16px 0 0 0;font-size:15px;line-height:1.55;color:{_TEXT};">
              Best Regards,<br>
              <strong style="color:{_BLUE};">{company_name}</strong>
            </p>
            <p style="margin:20px 0 0 0;padding-top:16px;border-top:1px solid {_BORDER};font-size:12px;line-height:1.5;color:{_MUTED};">
              {escape(footer)}
            </p>
          </td>
        </tr>
      </table>
    </td>
  </tr>
</table>
""".strip()


def build_assignment_email_text(
    company: CompanyProfile,
    ticket,
    *,
    assigned_by,
    ticket_url: str = "",
) -> str:
    lines = [
        "Hello,",
        "",
        f"Ticket {_ticket_ref(ticket)} has been assigned to you.",
        "",
        f"Company: {company.company_name}",
        "",
    ]
    lines.extend(_text_table(_detail_rows_for_assignee(ticket, assigned_by=assigned_by)))
    lines.extend(
        [
            "",
            "Ticket is opened.",
            "",
            "Best regards,",
            company.company_name or "Support team",
        ]
    )
    return "\n".join(lines)


def build_assignment_email_html(
    company: CompanyProfile,
    ticket,
    *,
    assigned_by,
    ticket_url: str = "",
    logo_cid: str = "",
) -> str:
    ref = escape(_ticket_ref(ticket))
    subject = escape(ticket.title or "")
    intro = f"""
            <p style="margin:0 0 12px 0;font-size:15px;line-height:1.55;color:{_TEXT};font-family:{_FONT};">
              Hello,
            </p>
            <p style="margin:0 0 22px 0;font-size:15px;line-height:1.55;color:{_TEXT};font-family:{_FONT};">
              Ticket <strong style="color:{_BLUE};">{ref}</strong>
              (<em>{subject}</em>) has been assigned to you.
            </p>
    """
    body = f"""
            <p style="margin:8px 0 16px 0;font-size:15px;line-height:1.55;color:{_TEXT};">
              Ticket is opened.
            </p>
            <p style="margin:0 0 4px 0;font-size:15px;line-height:1.55;color:{_TEXT};">
              Please review and update the ticket at the earliest.
            </p>
    """
    return _branded_shell(
        company=company,
        logo_cid=logo_cid,
        eyebrow="Support ticket assignment",
        heading="Ticket assigned to you",
        intro_html=intro,
        rows=_detail_rows_for_assignee(ticket, assigned_by=assigned_by),
        body_html=body,
        footer="This notification was sent because a support ticket was assigned to your account.",
    )


def build_requester_assignment_email_text(
    company: CompanyProfile,
    ticket,
    *,
    assigned_by,
) -> str:
    assignee = _assignee_label(ticket)
    lines = [
        "Hello,",
        "",
        f"Your ticket {_ticket_ref(ticket)} has been assigned to {assignee}.",
        "",
        f"Company: {company.company_name}",
        "",
    ]
    lines.extend(
        _text_table(_detail_rows_for_requester(ticket, assigned_by=assigned_by))
    )
    lines.extend(
        [
            "",
            "We will update you as work progresses.",
            "",
            "Best regards,",
            company.company_name or "Support team",
        ]
    )
    return "\n".join(lines)


def build_requester_assignment_email_html(
    company: CompanyProfile,
    ticket,
    *,
    assigned_by,
    logo_cid: str = "",
) -> str:
    ref = escape(_ticket_ref(ticket))
    subject = escape(ticket.title or "")
    assignee = escape(_assignee_label(ticket))
    intro = f"""
            <p style="margin:0 0 12px 0;font-size:15px;line-height:1.55;color:{_TEXT};font-family:{_FONT};">
              Hello,
            </p>
            <p style="margin:0 0 22px 0;font-size:15px;line-height:1.55;color:{_TEXT};font-family:{_FONT};">
              Your ticket <strong style="color:{_BLUE};">{ref}</strong>
              (<em>{subject}</em>) has been assigned to
              <strong>{assignee}</strong>.
            </p>
    """
    body = f"""
            <p style="margin:8px 0 4px 0;font-size:15px;line-height:1.55;color:{_TEXT};">
              We will update you as work progresses.
            </p>
    """
    return _branded_shell(
        company=company,
        logo_cid=logo_cid,
        eyebrow="Support ticket update",
        heading="Your ticket has been assigned",
        intro_html=intro,
        rows=_detail_rows_for_requester(ticket, assigned_by=assigned_by),
        body_html=body,
        footer="This notification was sent because your support ticket was assigned.",
    )


def build_closed_email_text(company: CompanyProfile, ticket) -> str:
    lines = [
        "Hello,",
        "",
        f"Your ticket {_ticket_ref(ticket)} has been closed.",
        "",
        _CLOSED_MESSAGE,
        "",
        f"Company: {company.company_name}",
        "",
    ]
    lines.extend(_text_table(_detail_rows_for_requester(ticket)))
    lines.extend(
        [
            "",
            "Best regards,",
            company.company_name or "Support team",
        ]
    )
    return "\n".join(lines)


def build_closed_email_html(
    company: CompanyProfile,
    ticket,
    *,
    logo_cid: str = "",
) -> str:
    ref = escape(_ticket_ref(ticket))
    subject = escape(ticket.title or "")
    intro = f"""
            <p style="margin:0 0 12px 0;font-size:15px;line-height:1.55;color:{_TEXT};font-family:{_FONT};">
              Hello,
            </p>
            <p style="margin:0 0 22px 0;font-size:15px;line-height:1.55;color:{_TEXT};font-family:{_FONT};">
              Your ticket <strong style="color:{_BLUE};">{ref}</strong>
              (<em>{subject}</em>) has been closed.
            </p>
    """
    body = f"""
            <p style="margin:8px 0 4px 0;font-size:15px;line-height:1.55;color:{_TEXT};">
              {escape(_CLOSED_MESSAGE)}
            </p>
    """
    return _branded_shell(
        company=company,
        logo_cid=logo_cid,
        eyebrow="Support ticket closed",
        heading="Your ticket is closed",
        intro_html=intro,
        rows=_detail_rows_for_requester(ticket),
        body_html=body,
        footer="This notification was sent because your support ticket was closed.",
    )


def _send_branded(
    *,
    company: CompanyProfile,
    recipient: str,
    subject: str,
    text_body: str,
    html_body: str,
    logo_inline,
) -> tuple[str, str | None]:
    creds = _smtp_credentials(company)
    if not creds:
        return (
            "skipped_no_smtp",
            "Configure support/outgoing email and app password in Company profile.",
        )
    from_email, password = creds
    ok, err = send_outbound(
        from_email=from_email,
        app_password=password,
        to_addresses=[recipient],
        subject=subject[:200],
        body=text_body,
        html_body=html_body,
        inline_images=[logo_inline] if logo_inline else None,
    )
    if ok:
        return "sent", None
    return "failed", err or "SMTP send failed"


def send_assignment_email(
    *,
    company: CompanyProfile,
    ticket,
    assignee,
    assigned_by=None,
) -> tuple[str, str | None]:
    """
    Send branded assignment mail to the engineer (assignee).

    Returns (status, error_message) where status is one of:
    sent | failed | skipped_self | skipped_no_email | skipped_no_smtp | skipped_unassign
    """
    if assignee is None:
        return "skipped_unassign", None

    recipient = (getattr(assignee, "email", None) or "").strip()
    if not recipient:
        return "skipped_no_email", "Assignee has no email address."

    assigner_email = (getattr(assigned_by, "email", None) or "").strip().lower()
    if assigner_email and recipient.lower() == assigner_email:
        return "skipped_self", None

    logo_inline, logo_cid = _company_logo(company)
    subject = (
        f"[{_ticket_ref(ticket)}] Ticket assigned to you — "
        f"{ticket.title or 'Support ticket'}"
    )
    return _send_branded(
        company=company,
        recipient=recipient,
        subject=subject,
        text_body=build_assignment_email_text(
            company, ticket, assigned_by=assigned_by
        ),
        html_body=build_assignment_email_html(
            company,
            ticket,
            assigned_by=assigned_by,
            logo_cid=logo_cid,
        ),
        logo_inline=logo_inline,
    )


def send_requester_assignment_email(
    *,
    company: CompanyProfile,
    ticket,
    assignee,
    assigned_by=None,
) -> tuple[str, str | None]:
    """Notify the ticket requester/creator that an engineer was assigned."""
    if assignee is None:
        return "skipped_unassign", None

    recipient = requester_email(ticket)
    if not recipient:
        return "skipped_no_email", "Ticket requester has no email address."

    assignee_email = (getattr(assignee, "email", None) or "").strip().lower()
    if assignee_email and recipient.lower() == assignee_email:
        # Same person already gets the engineer assignment mail.
        return "skipped_self", None

    logo_inline, logo_cid = _company_logo(company)
    subject = (
        f"[{_ticket_ref(ticket)}] Your ticket has been assigned — "
        f"{ticket.title or 'Support ticket'}"
    )
    return _send_branded(
        company=company,
        recipient=recipient,
        subject=subject,
        text_body=build_requester_assignment_email_text(
            company, ticket, assigned_by=assigned_by
        ),
        html_body=build_requester_assignment_email_html(
            company,
            ticket,
            assigned_by=assigned_by,
            logo_cid=logo_cid,
        ),
        logo_inline=logo_inline,
    )


def send_ticket_closed_email(
    *,
    company: CompanyProfile,
    ticket,
) -> tuple[str, str | None]:
    """Notify the ticket requester/creator that the ticket was closed."""
    recipient = requester_email(ticket)
    if not recipient:
        return "skipped_no_email", "Ticket requester has no email address."

    logo_inline, logo_cid = _company_logo(company)
    subject = (
        f"[{_ticket_ref(ticket)}] Your ticket is closed — "
        f"{ticket.title or 'Support ticket'}"
    )
    return _send_branded(
        company=company,
        recipient=recipient,
        subject=subject,
        text_body=build_closed_email_text(company, ticket),
        html_body=build_closed_email_html(
            company,
            ticket,
            logo_cid=logo_cid,
        ),
        logo_inline=logo_inline,
    )


def _bg_send(label: str, fn, **kwargs) -> None:
    """Fire-and-forget mail so assign/close UI is not blocked on SMTP/Graph."""
    import threading

    from django.db import close_old_connections

    def _run():
        close_old_connections()
        try:
            status, err = fn(**kwargs)
            if status == "failed":
                logger.warning("%s failed: %s", label, err)
            else:
                logger.info("%s status=%s", label, status)
        except Exception:
            logger.exception("%s crashed", label)
        finally:
            close_old_connections()

    threading.Thread(target=_run, name=label, daemon=True).start()


def queue_assignment_emails(
    *,
    company: CompanyProfile,
    ticket,
    assignee,
    assigned_by=None,
) -> None:
    """Queue assignee + requester assignment mails without blocking the request."""
    from django.contrib.auth import get_user_model
    from helpdesk.models import Ticket

    company_id = company.pk
    ticket_id = ticket.pk
    assignee_id = getattr(assignee, "pk", None)
    assigned_by_id = getattr(assigned_by, "pk", None)
    User = get_user_model()

    def _send_assignee():
        company_obj = CompanyProfile.objects.get(pk=company_id)
        ticket_obj = Ticket.objects.select_related("assigned_to", "queue").get(
            pk=ticket_id
        )
        assignee_obj = (
            User.objects.filter(pk=assignee_id).first() if assignee_id else None
        )
        by_obj = (
            User.objects.filter(pk=assigned_by_id).first() if assigned_by_id else None
        )
        return send_assignment_email(
            company=company_obj,
            ticket=ticket_obj,
            assignee=assignee_obj,
            assigned_by=by_obj,
        )

    def _send_requester():
        company_obj = CompanyProfile.objects.get(pk=company_id)
        ticket_obj = Ticket.objects.select_related("assigned_to", "queue").get(
            pk=ticket_id
        )
        assignee_obj = (
            User.objects.filter(pk=assignee_id).first() if assignee_id else None
        )
        by_obj = (
            User.objects.filter(pk=assigned_by_id).first() if assigned_by_id else None
        )
        return send_requester_assignment_email(
            company=company_obj,
            ticket=ticket_obj,
            assignee=assignee_obj,
            assigned_by=by_obj,
        )

    _bg_send("assignment-email", _send_assignee)
    if assignee_id is not None:
        _bg_send("requester-assignment-email", _send_requester)


def queue_ticket_closed_email(*, company: CompanyProfile, ticket) -> None:
    """Queue ticket-closed mail without blocking the request."""
    from helpdesk.models import Ticket

    company_id = company.pk
    ticket_id = ticket.pk

    def _send_closed():
        company_obj = CompanyProfile.objects.get(pk=company_id)
        ticket_obj = Ticket.objects.select_related("assigned_to", "queue").get(
            pk=ticket_id
        )
        return send_ticket_closed_email(company=company_obj, ticket=ticket_obj)

    _bg_send("ticket-closed-email", _send_closed)
