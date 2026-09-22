"""Renewal quotation SLA: 24×7 or working-hours clock from quantity to Zoho send."""

from __future__ import annotations

from datetime import datetime, timedelta

from django.utils import timezone
from django.utils.dateparse import parse_datetime

from accounts.models import CompanyProfile
from business.models import RenewalSLASettings, SubscriptionRenewalInvite

_SENT_STAGES = frozenset(
    {
        "delivered",
        "sent",
        "confirmed",
        "closed won",
        "approved",
        "accepted",
    }
)
_NOT_SENT_STAGES = frozenset(
    {
        "draft",
        "negotiation",
        "on hold",
        "cancelled",
        "canceled",
        "closed lost",
        "rejected",
        "lost",
    }
)


def get_or_create_renewal_sla_settings(company: CompanyProfile) -> RenewalSLASettings:
    settings_obj, _ = RenewalSLASettings.objects.get_or_create(company=company)
    return settings_obj


def quote_stage_name(value) -> str:
    if isinstance(value, dict):
        return str(value.get("name") or value.get("display_value") or "").strip()
    return str(value or "").strip()


def quote_stage_is_sent(stage: str) -> bool:
    key = " ".join(quote_stage_name(stage).lower().replace("_", " ").split())
    if not key or key in _NOT_SENT_STAGES:
        return False
    if key in _SENT_STAGES:
        return True
    return "sent" in key or "deliver" in key


def format_hour_span(hours: float | None) -> str:
    if hours is None:
        return "—"
    total_minutes = int(round(max(float(hours), 0) * 60))
    h, m = divmod(total_minutes, 60)
    if h and m:
        return f"{h}h {m}m"
    if h:
        return f"{h}h"
    return f"{m}m"


def _aware(dt):
    if dt is None:
        return None
    if timezone.is_naive(dt):
        return timezone.make_aware(dt, timezone.get_current_timezone())
    return dt


def _local(dt):
    aware = _aware(dt)
    if aware is None:
        return None
    return timezone.localtime(aware)


def _combine(day, clock, tz):
    naive = datetime.combine(day, clock)
    if timezone.is_naive(naive):
        return timezone.make_aware(naive, tz)
    return naive.replace(tzinfo=tz)


def elapsed_seconds(start, end, settings_obj: RenewalSLASettings) -> float:
    start = _local(start)
    end = _local(end)
    if start is None or end is None or end <= start:
        return 0.0
    if not settings_obj.uses_working_hours():
        return (end - start).total_seconds()

    tz = start.tzinfo or timezone.get_current_timezone()
    days = settings_obj.working_weekdays()
    work_start = settings_obj.work_start
    work_end = settings_obj.work_end
    total = 0.0
    cursor = start.date()
    last = end.date()
    while cursor <= last:
        if cursor.weekday() in days:
            window_start = _combine(cursor, work_start, tz)
            window_end = _combine(cursor, work_end, tz)
            overlap_start = max(start, window_start)
            overlap_end = min(end, window_end)
            if overlap_end > overlap_start:
                total += (overlap_end - overlap_start).total_seconds()
        cursor += timedelta(days=1)
    return total


def add_clock_hours(start, hours: float, settings_obj: RenewalSLASettings):
    start = _local(start)
    if start is None:
        return None
    hours = float(hours or 0)
    if hours <= 0:
        return start
    if not settings_obj.uses_working_hours():
        return start + timedelta(hours=hours)

    tz = start.tzinfo or timezone.get_current_timezone()
    days = settings_obj.working_weekdays()
    remaining = hours * 3600
    cursor = start
    safety = 0
    while remaining > 0.5 and safety < 400:
        safety += 1
        if cursor.weekday() in days:
            window_start = _combine(cursor.date(), settings_obj.work_start, tz)
            window_end = _combine(cursor.date(), settings_obj.work_end, tz)
            begin = max(cursor, window_start)
            if begin < window_end:
                available = (window_end - begin).total_seconds()
                if remaining <= available:
                    return begin + timedelta(seconds=remaining)
                remaining -= available
        cursor = _combine(
            cursor.date() + timedelta(days=1), settings_obj.work_start, tz
        )
    return cursor


def parse_zoho_datetime(value):
    raw = str(value or "").strip()
    if not raw:
        return None
    parsed = parse_datetime(raw.replace("Z", "+00:00"))
    return _aware(parsed)


def sla_status_for_invite(
    invite: SubscriptionRenewalInvite,
    settings_obj: RenewalSLASettings | None,
    *,
    now=None,
) -> dict:
    settings_obj = settings_obj or RenewalSLASettings()
    now = _aware(now) or timezone.now()
    quantity_at = _aware(invite.submitted_at)
    quote_number_at = _aware(invite.quotation_sent_at)
    quote_sent_at = _aware(invite.zoho_quote_sent_at)
    stage = quote_stage_name(invite.zoho_quote_stage)
    sent = bool(quote_sent_at) or quote_stage_is_sent(stage)
    if sent and quote_sent_at is None:
        quote_sent_at = quote_number_at or now

    if quote_number_at and not stage:
        stage = "Draft"
    if sent:
        zoho_status = "sent"
        zoho_status_label = "Sent"
    elif quote_number_at or stage:
        zoho_status = "draft_pending"
        zoho_status_label = "Draft pending"
    else:
        zoho_status = "none"
        zoho_status_label = "—"

    target = int(settings_obj.target_hours or 0)
    clock_mode = settings_obj.clock_mode
    empty = {
        "status": "off",
        "label": "—",
        "zoho_status": zoho_status,
        "zoho_status_label": zoho_status_label,
        "zoho_stage": stage,
        "target_hours": target,
        "elapsed_hours": None,
        "elapsed_label": "—",
        "remaining_hours": None,
        "remaining_label": "—",
        "qty_to_quote_hours": None,
        "qty_to_quote_label": "—",
        "quote_to_send_hours": None,
        "quote_to_send_label": "—",
        "clock_mode": clock_mode,
        "quantity_at": quantity_at,
        "quote_number_at": quote_number_at,
        "quote_sent_at": quote_sent_at,
        "due_at": None,
    }
    if not quantity_at or target <= 0:
        return empty

    due_at = add_clock_hours(quantity_at, target, settings_obj)
    stop = quote_sent_at or now
    elapsed = elapsed_seconds(quantity_at, stop, settings_obj) / 3600.0
    remaining = max(target - elapsed, 0) if not sent else 0.0
    qty_to_quote = (
        elapsed_seconds(quantity_at, quote_number_at, settings_obj) / 3600.0
        if quote_number_at
        else None
    )
    quote_to_send = None
    if quote_number_at and (quote_sent_at or not sent):
        quote_to_send = (
            elapsed_seconds(quote_number_at, quote_sent_at or now, settings_obj)
            / 3600.0
        )

    if sent:
        status = "met" if elapsed <= target + 1e-6 else "breached"
        label = "Met" if status == "met" else "Breached"
    elif now > due_at:
        status = "overdue"
        label = "Overdue"
    else:
        status = "on_track"
        label = "On track"

    return {
        "status": status,
        "label": label,
        "zoho_status": zoho_status,
        "zoho_status_label": zoho_status_label,
        "zoho_stage": stage,
        "target_hours": target,
        "elapsed_hours": round(elapsed, 2),
        "elapsed_label": format_hour_span(elapsed),
        "remaining_hours": round(remaining, 2),
        "remaining_label": format_hour_span(remaining),
        "qty_to_quote_hours": None if qty_to_quote is None else round(qty_to_quote, 2),
        "qty_to_quote_label": format_hour_span(qty_to_quote),
        "quote_to_send_hours": None if quote_to_send is None else round(quote_to_send, 2),
        "quote_to_send_label": format_hour_span(quote_to_send),
        "clock_mode": clock_mode,
        "quantity_at": quantity_at,
        "quote_number_at": quote_number_at,
        "quote_sent_at": quote_sent_at,
        "due_at": due_at,
    }


def apply_zoho_quote_status(
    invite: SubscriptionRenewalInvite,
    *,
    stage: str,
    quote_number: str = "",
    modified_at=None,
) -> bool:
    """Update stage / sent-at from a Zoho quote record. Returns True if saved."""
    stage = quote_stage_name(stage)
    dirty: list[str] = []
    if stage and invite.zoho_quote_stage != stage:
        invite.zoho_quote_stage = stage[:64]
        dirty.append("zoho_quote_stage")
    number = (quote_number or "").strip()
    if number and invite.zoho_quote_number != number:
        invite.zoho_quote_number = number[:128]
        dirty.append("zoho_quote_number")
    if not invite.zoho_quote_sent_at and quote_stage_is_sent(stage):
        sent_at = parse_zoho_datetime(modified_at) or timezone.now()
        created = _aware(invite.quotation_sent_at)
        submitted = _aware(invite.submitted_at)
        floor = created or submitted
        if floor and sent_at < floor:
            sent_at = floor
        invite.zoho_quote_sent_at = sent_at
        dirty.append("zoho_quote_sent_at")
    if not dirty:
        return False
    invite.save(update_fields=dirty)
    return True


def sync_zoho_quote_sent_status(company: CompanyProfile, invites) -> int:
    """Pull Quote_Stage for drafts that are not marked sent yet. Returns updates."""
    pending = [
        invite
        for invite in invites
        if (invite.zoho_quote_id or "").strip() and not invite.zoho_quote_sent_at
    ]
    if not pending or not getattr(company, "zoho_connected", False):
        return 0
    from business.zoho_crm import fetch_quote_statuses, zoho_configured

    if not zoho_configured():
        return 0
    try:
        statuses = fetch_quote_statuses(
            company, [invite.zoho_quote_id for invite in pending]
        )
    except Exception:
        return 0
    updated = 0
    for invite in pending:
        record = statuses.get(str(invite.zoho_quote_id))
        if not record:
            continue
        if apply_zoho_quote_status(
            invite,
            stage=record.get("stage") or "",
            quote_number=record.get("quote_number") or "",
            modified_at=record.get("modified_time"),
        ):
            updated += 1
    return updated
