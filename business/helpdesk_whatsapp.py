"""Create django-helpdesk tickets from WhatsApp support intake."""

from __future__ import annotations

from django.utils import timezone
from helpdesk.models import FollowUp, Ticket
from helpdesk.signals import new_ticket_done

from accounts.models import CompanyProfile
from business.helpdesk_bridge import PRIORITY_UI_TO_HD, ensure_meta, get_company_queue
from business.helpdesk_inbox import _MAX_ATTACHMENT_BYTES, _store_message_files
from business.models import SupportTicketMeta


def format_whatsapp_phone(wa_id: str) -> str:
    """Display-friendly mobile from WhatsApp wa_id."""
    raw = (wa_id or "").strip()
    if not raw:
        return ""
    if raw.startswith("+"):
        return raw
    digits = "".join(c for c in raw if c.isdigit())
    if not digits:
        return raw
    return f"+{digits}"


def build_whatsapp_ticket_description(
    *,
    firm_name: str,
    phone_display: str,
    issue: str,
) -> str:
    firm = (firm_name or "").strip()
    phone = (phone_display or "").strip()
    body = (issue or "").strip()
    lines = []
    if firm:
        lines.append(f"Firm: {firm}")
    if phone:
        lines.append(f"Mobile: {phone}")
    lines.append("Channel: WhatsApp")
    if body:
        lines.extend(["", body])
    return "\n".join(lines).strip()


def create_helpdesk_ticket_from_whatsapp(
    *,
    company: CompanyProfile,
    firm_name: str,
    description: str,
    phone_wa_id: str,
    attachments: list[tuple[str, bytes, str]] | None = None,
    message_id: str = "",
) -> Ticket:
    from business.sla import apply_sla_to_ticket

    queue = get_company_queue(company)
    now = timezone.now()
    firm = (firm_name or "").strip()
    phone_display = format_whatsapp_phone(phone_wa_id)
    issue = (description or "").strip()
    title = f"Support — {firm}" if firm else "WhatsApp support"
    full_description = build_whatsapp_ticket_description(
        firm_name=firm,
        phone_display=phone_display,
        issue=issue,
    )

    ticket = Ticket(
        title=title[:200],
        queue=queue,
        created=now,
        modified=now,
        submitter_email=None,
        status=Ticket.OPEN_STATUS,
        description=full_description,
        priority=PRIORITY_UI_TO_HD.get("medium", 3),
    )
    ticket.save()
    ensure_meta(
        ticket,
        created_by=company.user,
        department="general",
        is_internal=False,
        source_channel=SupportTicketMeta.SourceChannel.WHATSAPP,
        customer_phone=phone_display[:64],
        customer_firm_name=firm[:255],
    )

    mid = (message_id or "").strip()
    opening = FollowUp.objects.create(
        ticket=ticket,
        date=now,
        title="WhatsApp Received",
        comment=full_description,
        public=True,
        user=None,
        new_status=Ticket.OPEN_STATUS,
        message_id=mid or None,
    )
    files: list[tuple[str, bytes, str]] = []
    for name, data, mime in attachments or []:
        if not data or len(data) > _MAX_ATTACHMENT_BYTES:
            continue
        files.append((name, data, mime))
    _store_message_files(opening, files)

    apply_sla_to_ticket(
        company,
        ticket,
        priority="medium",
        priority_source="default",
    )
    try:
        from business.support_team import auto_assign_ticket

        auto_assign_ticket(company, ticket)
    except Exception:
        import logging

        logging.getLogger(__name__).exception(
            "Auto-assign failed for WhatsApp ticket_id=%s", ticket.pk
        )
    try:
        from business.team_notifications import notify_new_ticket

        notify_new_ticket(ticket)
    except Exception:
        import logging

        logging.getLogger(__name__).exception(
            "Team notification failed for WhatsApp ticket_id=%s", ticket.pk
        )
    new_ticket_done.send(sender="helpdesk_whatsapp", ticket=ticket)
    return ticket
