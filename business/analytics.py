"""JSON-safe chart payloads for the sales and support dashboards."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta
from types import SimpleNamespace

from django.db.models import Sum
from django.db.models.functions import TruncMonth
from django.utils import timezone

from business.models import (
    ClientSubscription,
    SubscriptionRenewalInviteLine,
    SubscriptionRenewalInviteNewProduct,
)


def format_duration(seconds) -> str:
    """Show elapsed time in minutes or hours (never a bare dash)."""
    if seconds is None:
        return "0m"
    total_minutes = int(round(max(0, float(seconds)) / 60))
    if total_minutes < 1:
        return "0m"
    if total_minutes < 60:
        return f"{total_minutes}m"
    hours = total_minutes // 60
    mins = total_minutes % 60
    if hours < 48:
        return f"{hours}h {mins}m" if mins else f"{hours}h"
    days = hours // 24
    rem_h = hours % 24
    return f"{days}d {rem_h}h" if rem_h else f"{days}d"


def _local_date(value) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if timezone.is_aware(value):
            return timezone.localtime(value).date()
        return value.date()
    if isinstance(value, date):
        return value
    return None


def _month_start(d: date, months_back: int = 0) -> date:
    y, m = d.year, d.month - months_back
    while m < 1:
        m += 12
        y -= 1
    return date(y, m, 1)


def _month_label(d: date) -> str:
    return d.strftime("%b")


def _day_label(d: date) -> str:
    return d.strftime("%d %b")


def _aware_start(d: date):
    dt = datetime.combine(d, time.min)
    current_tz = timezone.get_current_timezone()
    if timezone.is_naive(dt):
        return timezone.make_aware(dt, current_tz)
    return dt


def _pct(part: int, whole: int) -> int:
    if whole <= 0:
        return 0
    return int(round(100 * part / whole))


def _avg(values: list[float]) -> float | None:
    if not values:
        return None
    return sum(values) / len(values)


def sales_dashboard_charts(company, *, expiring_qs=None, today=None) -> dict:
    today = today or timezone.localdate()
    tz = timezone.get_current_timezone()
    subs = company.subscriptions
    win = subs.filter(deal_status=ClientSubscription.DealStatus.WIN).count()
    lost = subs.filter(deal_status=ClientSubscription.DealStatus.LOST).count()
    pending_deals = (
        SubscriptionRenewalInviteLine.objects.filter(
            invite__company=company,
            invite__email_sent_at__isnull=False,
            subscription__company=company,
            subscription__deal_status="",
        )
        .values("subscription_id")
        .distinct()
        .count()
    )
    decided = win + lost
    win_rate = _pct(win, decided)

    if expiring_qs is None:
        pending_renewals = 0
    else:
        pending_renewals = expiring_qs.exclude(
            deal_status__in=[
                ClientSubscription.DealStatus.WIN,
                ClientSubscription.DealStatus.LOST,
            ]
        ).count()

    month_start = _month_start(today)
    month_start_dt = _aware_start(month_start)
    created_qty = (
        subs.filter(created_at__gte=month_start_dt).aggregate(total=Sum("quantity"))[
            "total"
        ]
        or 0
    )
    new_prod_qty = (
        SubscriptionRenewalInviteNewProduct.objects.filter(
            invite__company=company,
            invite__submitted_at__gte=month_start_dt,
        ).aggregate(total=Sum("quantity"))["total"]
        or 0
    )
    new_qty_month = int(created_qty) + int(new_prod_qty)

    pipeline_days = [today + timedelta(days=i) for i in range(14)]
    pipeline_until = today + timedelta(days=13)
    expiry_counts = Counter(
        subs.filter(
            status=ClientSubscription.Status.ACTIVE,
            end_date__gte=today,
            end_date__lte=pipeline_until,
        ).values_list("end_date", flat=True)
    )
    pipeline_values = [int(expiry_counts.get(day, 0)) for day in pipeline_days]

    months = [_month_start(today, months_back=i) for i in range(5, -1, -1)]
    series_start = _aware_start(months[0])
    qty_by_month: dict[date, int] = defaultdict(int)
    created_rows = (
        subs.filter(created_at__gte=series_start)
        .annotate(month=TruncMonth("created_at", tzinfo=tz))
        .values("month")
        .annotate(qty=Sum("quantity"))
    )
    for row in created_rows:
        key = _local_date(row["month"])
        if key:
            qty_by_month[_month_start(key)] += int(row["qty"] or 0)
    new_prod_rows = (
        SubscriptionRenewalInviteNewProduct.objects.filter(
            invite__company=company,
            invite__submitted_at__gte=series_start,
        )
        .annotate(month=TruncMonth("invite__submitted_at", tzinfo=tz))
        .values("month")
        .annotate(qty=Sum("quantity"))
    )
    for row in new_prod_rows:
        key = _local_date(row["month"])
        if key:
            qty_by_month[_month_start(key)] += int(row["qty"] or 0)

    deal_total = win + lost + pending_deals
    return {
        "win": win,
        "lost": lost,
        "pending_deals": pending_deals,
        "deal_total": deal_total,
        "win_rate": win_rate,
        "lost_rate": _pct(lost, decided),
        "pending_renewals": pending_renewals,
        "new_qty_month": new_qty_month,
        "pipeline_labels": [_day_label(d) for d in pipeline_days],
        "pipeline_values": pipeline_values,
        "qty_month_labels": [_month_label(d) for d in months],
        "qty_month_values": [qty_by_month.get(m, 0) for m in months],
    }


def _ticket_on_time(ticket) -> bool | None:
    if getattr(ticket, "status", "") not in ("resolved", "closed"):
        return None
    sla = getattr(ticket, "sla", None) or {}
    statuses = []
    if sla.get("tracks_resolve"):
        statuses.append(sla.get("resolve_status"))
    if sla.get("tracks_response"):
        statuses.append(sla.get("response_status"))
    if not statuses:
        if sla.get("any_breached"):
            return False
        return None
    if any(code == "breached" for code in statuses):
        return False
    if any(code == "met" for code in statuses):
        return True
    return None


def ticket_charts_from_views(tickets, *, now=None) -> dict:
    now = now or timezone.now()
    today = timezone.localtime(now).date() if timezone.is_aware(now) else now.date()
    window_start = now - timedelta(days=30)
    waiting_cutoff = now - timedelta(hours=8)
    volume_days = [today - timedelta(days=13 - i) for i in range(14)]

    status_counts = Counter()
    volume = Counter()
    sla_daily_met = Counter()
    sla_daily_total = Counter()
    week_solved = [0, 0, 0]
    week_open = [0, 0, 0]
    this_monday = today - timedelta(days=today.weekday())
    week_starts = [
        this_monday - timedelta(weeks=2),
        this_monday - timedelta(weeks=1),
        this_monday,
    ]

    resolve_secs: list[float] = []
    resolve_all: list[float] = []
    response_secs: list[float] = []
    response_all: list[float] = []
    on_time = 0
    on_time_total = 0
    sla_breached = 0
    high_unassigned = 0
    waiting_8h = 0
    unassigned_open = 0
    observation = 0
    closed = 0
    pending = 0
    solved = 0

    for ticket in tickets:
        status = getattr(ticket, "status", "") or "open"
        status_counts[status] += 1
        created = getattr(ticket, "created_at", None)
        created_day = _local_date(created)
        if created_day in volume:
            volume[created_day] += 1
        elif created_day is not None:
            volume[created_day] += 1

        sla = getattr(ticket, "sla", None) or {}
        if sla.get("any_breached"):
            sla_breached += 1

        assignee_id = getattr(ticket, "assignee_id", None)
        priority = getattr(ticket, "priority", "") or ""
        if (
            priority in ("high", "urgent")
            and not assignee_id
            and status != "closed"
        ):
            high_unassigned += 1
        if status == "open" and not assignee_id:
            unassigned_open += 1
        if status in ("open", "in_progress") and created and created <= waiting_cutoff:
            waiting_8h += 1
        if status == "resolved":
            observation += 1
        if status == "closed":
            closed += 1
        if status in ("open", "in_progress"):
            pending += 1
        if status in ("resolved", "closed"):
            solved += 1

        done_at = getattr(ticket, "completed_at", None)
        if not done_at and status in ("resolved", "closed"):
            done_at = getattr(ticket, "updated_at", None)
        if status in ("resolved", "closed") and created and done_at:
            elapsed = max(0, (done_at - created).total_seconds())
            resolve_all.append(elapsed)
            if done_at >= window_start:
                resolve_secs.append(elapsed)

        first_resp = getattr(ticket, "first_responded_at", None)
        if created and first_resp:
            resp_elapsed = max(0, (first_resp - created).total_seconds())
            response_all.append(resp_elapsed)
            if first_resp >= window_start:
                response_secs.append(resp_elapsed)

        timed = _ticket_on_time(ticket)
        if timed is not None:
            on_time_total += 1
            if timed:
                on_time += 1
            done_day = _local_date(done_at) or created_day
            if done_day and (today - timedelta(days=13)) <= done_day <= today:
                sla_daily_total[done_day] += 1
                if timed:
                    sla_daily_met[done_day] += 1

        if created_day:
            for idx, start in enumerate(week_starts):
                end = start + timedelta(days=7)
                if start <= created_day < end:
                    if status in ("resolved", "closed"):
                        week_solved[idx] += 1
                    else:
                        week_open[idx] += 1
                    break

    total = len(tickets)
    sla_met_pct = _pct(on_time, on_time_total)
    avg_resolve = _avg(resolve_secs) if resolve_secs else _avg(resolve_all)
    avg_response = _avg(response_secs) if response_secs else _avg(response_all)
    return {
        "total": total,
        "closed": closed,
        "pending": pending,
        "observation": observation,
        "solved": solved,
        "open_count": pending,
        "avg_resolution": format_duration(avg_resolve),
        "first_response": format_duration(avg_response),
        "solved_on_time": on_time,
        "solved_with_sla": on_time_total,
        "solved_on_time_pct": sla_met_pct,
        "sla_breached": sla_breached,
        "sla_met_pct": sla_met_pct,
        "sla_missed_pct": _pct(on_time_total - on_time, on_time_total),
        "status_mix": {
            "open": int(status_counts.get("open", 0)),
            "in_progress": int(status_counts.get("in_progress", 0)),
            "resolved": int(status_counts.get("resolved", 0)),
            "closed": int(status_counts.get("closed", 0)),
        },
        "volume_labels": [_day_label(d) for d in volume_days],
        "volume_values": [int(volume.get(d, 0)) for d in volume_days],
        "queue": {
            "high_unassigned": high_unassigned,
            "waiting_8h": waiting_8h,
            "unassigned_open": unassigned_open,
            "sla_breached": sla_breached,
            "observation": observation,
        },
        "week_labels": ["2 wks ago", "Last week", "This week"],
        "week_solved": week_solved,
        "week_open": week_open,
        "sla_daily_labels": [_day_label(d) for d in volume_days],
        "sla_daily_values": [
            _pct(sla_daily_met.get(d, 0), sla_daily_total.get(d, 0))
            if sla_daily_total.get(d, 0)
            else None
            for d in volume_days
        ],
    }


def empty_ticket_view(**kwargs):
    """Test helper matching TicketView attribute names."""
    defaults = {
        "status": "open",
        "sla": {},
        "created_at": None,
        "completed_at": None,
        "first_responded_at": None,
        "assignee_id": None,
        "priority": "medium",
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)
