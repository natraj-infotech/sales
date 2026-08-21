"""Reminder cadence: presets, validation, due slots, and email copy."""

from __future__ import annotations

import json
import re
from copy import deepcopy
from datetime import datetime, time
from typing import Any

from django.core.exceptions import ValidationError
from django.utils import timezone

MODE_WEEKLY = "weekly"
MODE_ALTERNATE = "alternate"
MODE_DAILY = "daily"
MODE_TIMES_PER_DAY = "times_per_day"
MODE_TOTAL_IN_WINDOW = "total_in_window"

MODE_CHOICES = (
    (MODE_WEEKLY, "Weekly"),
    (MODE_ALTERNATE, "Alternate days"),
    (MODE_DAILY, "Daily — once"),
    (MODE_TIMES_PER_DAY, "Daily — multiple times"),
    (MODE_TOTAL_IN_WINDOW, "Total mails in this window"),
)
VALID_MODES = {key for key, _ in MODE_CHOICES}

PRESET_LIGHT = "light"
PRESET_RECOMMENDED = "recommended"
PRESET_CUSTOM = "custom"
VALID_PRESETS = {PRESET_LIGHT, PRESET_RECOMMENDED, PRESET_CUSTOM}

DEFAULT_EMAIL_SUBJECT = (
    "{{client_name}} — subscription expiring in {{days_left}} days"
)
DEFAULT_INITIAL_PARAGRAPH = (
    "Kindly renew it to ensure there is no interruption to your services."
)
DEFAULT_END_PARAGRAPH = (
    "We request you to share your confirmation at the earliest so that we can "
    "initiate the renewal process and ensure uninterrupted service."
)

MAX_WINDOW_DAYS = 90
MIN_OVERDUE_DAYS = -31
MAX_PHASES = 12
MAX_TIMES_PER_DAY = 8


def _phase(
    name: str,
    from_days: int,
    to_days: int,
    mode: str,
    times: list[str],
    count: int = 1,
) -> dict[str, Any]:
    return {
        "name": name,
        "from_days": int(from_days),
        "to_days": int(to_days),
        "mode": mode,
        "count": int(count),
        "times": list(times),
    }


def recommended_schedule() -> list[dict[str, Any]]:
    return [
        _phase("Early window", 30, 16, MODE_WEEKLY, ["09:00"]),
        _phase("Mid window", 15, 7, MODE_ALTERNATE, ["09:00"]),
        _phase("Daily push", 6, 4, MODE_DAILY, ["09:00"]),
        _phase(
            "Last 3 days",
            3,
            1,
            MODE_TIMES_PER_DAY,
            ["09:00", "14:00", "18:00"],
            3,
        ),
        _phase(
            "Expiry day",
            0,
            0,
            MODE_TIMES_PER_DAY,
            ["09:00", "14:00", "18:00"],
            3,
        ),
        _phase("Overdue", -1, -7, MODE_DAILY, ["09:00"]),
    ]


DEFAULT_REMINDER_CYCLE_NAME = "Default Renewal"


def default_reminder_cycle_schedule() -> list[dict[str, Any]]:
    """Locked schedule for the company default reminder cycle."""
    return clone_schedule(recommended_schedule())


def default_reminder_cycle_steps() -> list[dict[str, str]]:
    """Human-readable summary rows for the locked default cycle."""
    return [
        {
            "title": "30 Days Remaining",
            "detail": "Weekly reminders",
        },
        {
            "title": "15 to 7 Days Remaining",
            "detail": "Alternate day reminders",
        },
        {
            "title": "6 to 4 Days Remaining",
            "detail": "Daily reminders",
        },
        {
            "title": "Last 3 Days",
            "detail": "3 reminders/day (9 AM, 2 PM, 6 PM)",
        },
        {
            "title": "Expiry Day",
            "detail": "3 reminders/day (9 AM, 2 PM, 6 PM)",
        },
        {
            "title": "1–7 Days Overdue",
            "detail": "Daily overdue reminder",
        },
    ]


def light_schedule(days: list[int] | tuple[int, ...] | None = None) -> list[dict[str, Any]]:
    values = sorted({int(d) for d in (days or (30, 15, 7)) if int(d) > 0}, reverse=True)
    if not values:
        values = [30, 15, 7]
    return [
        _phase(f"{day} days before", day, day, MODE_TOTAL_IN_WINDOW, ["09:00"], 1)
        for day in values
    ]


def parse_day_list(raw: str) -> list[int]:
    days: list[int] = []
    for part in re.split(r"[,;\s]+", (raw or "").strip()):
        if not part:
            continue
        try:
            value = int(part)
        except ValueError:
            continue
        if value > 0 and value not in days:
            days.append(value)
    return sorted(days, reverse=True)


def default_company_schedule() -> list[dict[str, Any]]:
    return recommended_schedule()


def parse_hhmm(raw: str) -> time | None:
    text = (raw or "").strip()
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", text)
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2))
    if hour > 23 or minute > 59:
        return None
    return time(hour, minute)


def format_hhmm(value: time) -> str:
    return f"{value.hour:02d}:{value.minute:02d}"


def window_days(from_days: int, to_days: int) -> list[int]:
    """Days-remaining values from the start of the window toward its end."""
    start = int(from_days)
    end = int(to_days)
    if start >= end:
        return list(range(start, end - 1, -1))
    return list(range(start, end + 1))


def send_days_for_phase(phase: dict[str, Any]) -> list[int]:
    days = window_days(int(phase["from_days"]), int(phase["to_days"]))
    mode = phase.get("mode") or MODE_DAILY
    count = max(1, int(phase.get("count") or 1))
    if not days:
        return []
    if mode in {MODE_DAILY, MODE_TIMES_PER_DAY}:
        return days
    if mode == MODE_WEEKLY:
        return [day for index, day in enumerate(days) if index % 7 == 0]
    if mode == MODE_ALTERNATE:
        return [day for index, day in enumerate(days) if index % 2 == 0]
    if mode == MODE_TOTAL_IN_WINDOW:
        count = min(count, len(days))
        if count == 1:
            return [days[0]]
        chosen: list[int] = []
        seen: set[int] = set()
        for index in range(count):
            pos = round(index * (len(days) - 1) / (count - 1))
            day = days[pos]
            if day not in seen:
                seen.add(day)
                chosen.append(day)
        return chosen
    return days


def phase_times(phase: dict[str, Any]) -> list[time]:
    parsed: list[time] = []
    for raw in phase.get("times") or ["09:00"]:
        value = parse_hhmm(str(raw))
        if value and value not in parsed:
            parsed.append(value)
    mode = phase.get("mode") or MODE_DAILY
    if mode != MODE_TIMES_PER_DAY:
        parsed = parsed[:1]
    else:
        count = max(1, min(int(phase.get("count") or len(parsed) or 1), MAX_TIMES_PER_DAY))
        parsed = parsed[:count]
    return parsed or [time(9, 0)]


def matching_phase(
    phases: list[dict[str, Any]], days_remaining: int
) -> dict[str, Any] | None:
    for phase in phases:
        if days_remaining in window_days(int(phase["from_days"]), int(phase["to_days"])):
            return phase
    return None


def due_send_numbers(
    phase: dict[str, Any],
    days_remaining: int,
    now: datetime,
) -> list[int]:
    if days_remaining not in send_days_for_phase(phase):
        return []
    current = now.time().replace(second=0, microsecond=0)
    due: list[int] = []
    for index, slot in enumerate(phase_times(phase), start=1):
        if slot <= current:
            due.append(index)
    return due


def next_due_slot(
    phase: dict[str, Any],
    days_remaining: int,
    now: datetime,
    already_sent: set[int],
) -> int | None:
    pending = [
        number
        for number in due_send_numbers(phase, days_remaining, now)
        if number not in already_sent
    ]
    return pending[0] if pending else None


def schedule_bounds(phases: list[dict[str, Any]]) -> tuple[int, int]:
    if not phases:
        return 0, 30
    lows: list[int] = []
    highs: list[int] = []
    for phase in phases:
        start = int(phase["from_days"])
        end = int(phase["to_days"])
        lows.append(min(start, end))
        highs.append(max(start, end))
    return min(lows), max(highs)


def in_schedule_window(phases: list[dict[str, Any]], days_remaining: int) -> bool:
    low, high = schedule_bounds(phases)
    return low <= days_remaining <= high


def notify_days_csv(phases: list[dict[str, Any]]) -> str:
    days = sorted(
        {
            max(int(phase["from_days"]), int(phase["to_days"]))
            for phase in phases
            if max(int(phase["from_days"]), int(phase["to_days"])) > 0
        },
        reverse=True,
    )
    return ",".join(str(day) for day in days) or "30,15,7"


def _coerce_phases(raw: Any) -> list[Any]:
    if raw in (None, "", []):
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValidationError("Reminder schedule is not valid JSON.") from exc
    if not isinstance(raw, list):
        raise ValidationError("Reminder schedule must be a list of phases.")
    return raw


def normalize_schedule(raw: Any, *, required: bool = True) -> list[dict[str, Any]]:
    phases = _coerce_phases(raw)
    if not phases:
        if required:
            raise ValidationError("Add at least one reminder phase.")
        return []
    if len(phases) > MAX_PHASES:
        raise ValidationError(f"You can add at most {MAX_PHASES} reminder phases.")

    cleaned: list[dict[str, Any]] = []
    for index, item in enumerate(phases, start=1):
        if not isinstance(item, dict):
            raise ValidationError(f"Phase {index} is invalid.")
        try:
            from_days = int(item.get("from_days"))
            to_days = int(item.get("to_days"))
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"Phase {index} needs valid from/to days.") from exc
        if from_days > MAX_WINDOW_DAYS or to_days > MAX_WINDOW_DAYS:
            raise ValidationError(
                f"Phase {index}: days remaining cannot be more than {MAX_WINDOW_DAYS}."
            )
        if from_days < MIN_OVERDUE_DAYS or to_days < MIN_OVERDUE_DAYS:
            raise ValidationError(
                f"Phase {index}: overdue window cannot go beyond {abs(MIN_OVERDUE_DAYS)} days."
            )
        mode = (item.get("mode") or MODE_DAILY).strip()
        if mode not in VALID_MODES:
            raise ValidationError(f"Phase {index} has an unknown send mode.")
        try:
            count = max(1, int(item.get("count") or 1))
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"Phase {index} has an invalid mail count.") from exc
        if mode == MODE_TIMES_PER_DAY:
            count = min(count, MAX_TIMES_PER_DAY)
        name = (item.get("name") or f"Phase {index}").strip()[:80]
        times_raw = item.get("times") or ["09:00"]
        if isinstance(times_raw, str):
            times_raw = [part.strip() for part in times_raw.split(",") if part.strip()]
        times: list[str] = []
        for raw_time in times_raw:
            parsed = parse_hhmm(str(raw_time))
            if parsed is None:
                raise ValidationError(
                    f"Phase {index}: time '{raw_time}' must be HH:MM (e.g. 09:00)."
                )
            stamp = format_hhmm(parsed)
            if stamp not in times:
                times.append(stamp)
        if not times:
            times = ["09:00"]
        if mode == MODE_TIMES_PER_DAY and len(times) < count:
            count = len(times)
        cleaned.append(
            _phase(name, from_days, to_days, mode, times, count)
        )
    return cleaned


def preset_schedule(preset: str, *, notify_days: str = "") -> list[dict[str, Any]]:
    if preset == PRESET_LIGHT:
        return light_schedule(parse_day_list(notify_days) or [30, 15, 7])
    if preset == PRESET_RECOMMENDED:
        return recommended_schedule()
    return recommended_schedule()


def resolve_reminder_template(company, client=None):
    from business.models import ReminderTemplate
    from business.reminder_templates import ensure_default_reminder_template

    if client is not None:
        template = getattr(client, "reminder_template", None)
        if template is not None:
            return template
    return ensure_default_reminder_template(company)


def resolve_schedule(company, client=None) -> list[dict[str, Any]]:
    template = resolve_reminder_template(company, client)
    if template and template.is_default:
        return default_reminder_cycle_schedule()
    if template and template.schedule:
        try:
            return normalize_schedule(template.schedule)
        except ValidationError:
            pass
    return default_reminder_cycle_schedule()


def resolve_email_copy(company, client=None) -> dict[str, str]:
    subject = (getattr(company, "expiry_email_subject", "") or "").strip() or (
        DEFAULT_EMAIL_SUBJECT
    )
    initial = (
        getattr(company, "expiry_email_initial_paragraph", "") or ""
    ).strip() or DEFAULT_INITIAL_PARAGRAPH
    end = (
        getattr(company, "expiry_email_end_paragraph", "") or ""
    ).strip() or DEFAULT_END_PARAGRAPH
    return {"subject": subject, "initial": initial, "end": end}


def apply_merge_tags(text: str, context: dict[str, str]) -> str:
    out = text or ""
    for key, value in context.items():
        replacement = "" if value is None else str(value)
        out = out.replace("{{" + key + "}}", replacement)
        out = out.replace("{{ " + key + " }}", replacement)
    return out


def merge_context(
    *,
    company,
    client,
    items: list[tuple[Any, int, int | None]],
    renewal_url: str = "",
) -> dict[str, str]:
    first_sub = items[0][0]
    days = [day for _, _, day in items if day is not None]
    soonest = min(days) if days else None
    if soonest is None:
        days_left = "soon"
    elif soonest < 0:
        days_left = f"{abs(soonest)} overdue"
    else:
        days_left = str(soonest)
    products = [sub.product.name for sub, _, _ in items]
    expiry_date = ""
    if getattr(first_sub, "end_date", None):
        expiry_date = first_sub.end_date.strftime("%d-%m-%Y")
    return {
        "client_name": getattr(client, "name", "") or "",
        "company_name": getattr(company, "company_name", "") or "",
        "product_name": products[0] if len(products) == 1 else "multiple subscriptions",
        "expiry_date": expiry_date,
        "days_left": days_left,
        "renewal_link": renewal_url or "",
    }


def now_for_today(today) -> datetime:
    current = timezone.localtime()
    if current.date() == today:
        return current
    naive = datetime.combine(today, time(18, 30))
    tz = timezone.get_current_timezone()
    if timezone.is_naive(naive):
        return timezone.make_aware(naive, tz)
    return naive


def clone_schedule(phases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return deepcopy(phases)
