"""SLA due-date calculation and breach helpers (24×7 calendar hours)."""

from __future__ import annotations

from datetime import timedelta

from django.utils import timezone

from accounts.models import CompanyProfile
from business.models import SupportSLASettings, SupportTicketMeta
from helpdesk.models import Ticket

_PRIORITY_HD_TO_UI = {
    1: "urgent",
    2: "high",
    3: "medium",
    4: "low",
    5: "low",
}


def get_or_create_sla_settings(company: CompanyProfile) -> SupportSLASettings:
    settings_obj, _ = SupportSLASettings.objects.get_or_create(company=company)
    return settings_obj


def _due_from(created_at, hours: int):
    if not created_at or not hours or hours <= 0:
        return None
    return created_at + timedelta(hours=int(hours))


def apply_sla_to_ticket(
    company: CompanyProfile,
    ticket: Ticket,
    *,
    priority: str | None = None,
    priority_source: str | None = None,
) -> SupportTicketMeta:
    """
    Set first_response_due_at / resolve_due_at from company SLA settings.

    Uses ticket.created as the clock start (24×7). Recalculates when priority changes.
    """
    from business.helpdesk_bridge import ensure_meta

    settings_obj = get_or_create_sla_settings(company)
    meta = ensure_meta(ticket)
    ui_priority = priority or _PRIORITY_HD_TO_UI.get(ticket.priority, "medium")
    created = ticket.created or timezone.now()

    response_due = None
    resolve_due = None
    if settings_obj.tracks_response():
        response_due = _due_from(created, settings_obj.response_hours_for(ui_priority))
    if settings_obj.tracks_resolve():
        resolve_due = _due_from(created, settings_obj.resolve_hours_for(ui_priority))

    dirty = ["updated_at"]
    if meta.first_response_due_at != response_due:
        meta.first_response_due_at = response_due
        dirty.append("first_response_due_at")
    if meta.resolve_due_at != resolve_due:
        meta.resolve_due_at = resolve_due
        dirty.append("resolve_due_at")
    if priority_source and meta.priority_source != priority_source:
        meta.priority_source = priority_source
        dirty.append("priority_source")
    meta.save(update_fields=dirty)
    # Keep OneToOne reverse cache on the ticket instance in sync.
    ticket.crm_meta = meta
    return meta


def mark_first_response(ticket: Ticket, when=None) -> bool:
    """Record first public staff/customer-facing response. Returns True if newly set."""
    from business.helpdesk_bridge import ensure_meta

    meta = ensure_meta(ticket)
    if meta.first_responded_at:
        return False
    meta.first_responded_at = when or timezone.now()
    meta.save(update_fields=["first_responded_at", "updated_at"])
    return True


def sla_status_for_meta(
    meta: SupportTicketMeta | None,
    *,
    settings_obj: SupportSLASettings | None,
    ticket_status: str,
) -> dict:
    """
    Compute display fields for list/detail.

    Returns keys: response_due, resolve_due, response_status, resolve_status,
    response_label, resolve_label, any_breached.
    """
    now = timezone.now()
    response_due = meta.first_response_due_at if meta else None
    resolve_due = meta.resolve_due_at if meta else None
    first_responded = meta.first_responded_at if meta else None
    completed = meta.completed_at if meta else None
    done = ticket_status in ("resolved", "closed")

    tracks_response = bool(settings_obj and settings_obj.tracks_response())
    tracks_resolve = bool(settings_obj and settings_obj.tracks_resolve())

    def _response_state():
        if not tracks_response or not response_due:
            return "off", "—"
        if first_responded:
            if first_responded <= response_due:
                return "met", "Met"
            return "breached", "Breached"
        if now > response_due:
            return "breached", "Overdue"
        return "ok", "On track"

    def _resolve_state():
        if not tracks_resolve or not resolve_due:
            return "off", "—"
        if done and completed:
            if completed <= resolve_due:
                return "met", "Met"
            return "breached", "Breached"
        if done:
            # completed_at missing — treat modified close as done without breach calc
            return "met", "Closed"
        if now > resolve_due:
            return "breached", "Overdue"
        return "ok", "On track"

    r_code, r_label = _response_state()
    s_code, s_label = _resolve_state()
    return {
        "response_due": response_due,
        "resolve_due": resolve_due,
        "response_status": r_code,
        "resolve_status": s_code,
        "response_label": r_label,
        "resolve_label": s_label,
        "any_breached": r_code == "breached" or s_code == "breached",
        "tracks_response": tracks_response,
        "tracks_resolve": tracks_resolve,
    }
