"""JSON-safe chart payloads for the sales and support dashboards."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from types import SimpleNamespace

from django.db.models import Sum
from django.db.models.functions import TruncMonth
from django.utils import timezone

from business.models import (
    ClientSubscription,
    SubscriptionRenewalInvite,
    SubscriptionRenewalInviteLine,
    SubscriptionRenewalInviteNewProduct,
)
from business.reminder_schedule import recommended_schedule, schedule_bounds


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


RENEWAL_REPORT_RANGES = (
    ("30", "Last 30 days"),
    ("90", "Last 90 days"),
    ("year", "This year"),
)
_PENDING_HORIZON_DAYS = 60
_TIMING_CHART_LIMIT = 8
_BLOCKING_DEALS = {
    ClientSubscription.DealStatus.WIN,
    ClientSubscription.DealStatus.LOST,
    ClientSubscription.DealStatus.CANCEL,
}


def _report_window(today: date, range_key: str) -> tuple[date, str]:
    key = (range_key or "90").strip().lower()
    if key == "30":
        return today - timedelta(days=29), "Last 30 days"
    if key == "year":
        return date(today.year, 1, 1), "This year"
    return today - timedelta(days=89), "Last 90 days"


def _fmt_dt(value) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        if timezone.is_aware(value):
            value = timezone.localtime(value)
        return value.strftime("%d %b %Y, %H:%M")
    if isinstance(value, date):
        return value.strftime("%d %b %Y")
    return ""


def _fmt_money(value) -> str:
    amount = Decimal(value or 0)
    return f"₹{amount:,.2f}"


def _fmt_days_left(days) -> str:
    if days is None:
        return "—"
    if days < 0:
        return f"Expired {abs(days)}d ago"
    if days == 0:
        return "Today"
    return f"{days}d"


def _deal_label(status: str) -> str:
    return {
        ClientSubscription.DealStatus.WIN: "Win",
        ClientSubscription.DealStatus.LOST: "Lost",
    }.get(status or "", "Pending")


def _early_window_days(client, default_phases) -> int:
    phases = None
    template = getattr(client, "reminder_template", None) if client else None
    if template and template.schedule:
        phases = template.schedule
    elif default_phases:
        phases = default_phases
    else:
        phases = recommended_schedule()
    _low, high = schedule_bounds(phases)
    return high if high > 0 else 30


def _invite_opened(invite) -> bool:
    if getattr(invite, "read_at", None):
        return True
    return any(getattr(row, "opened_at", None) for row in invite.recipients.all())


def _duration_or_dash(seconds) -> str:
    if seconds is None:
        return "—"
    return format_duration(seconds)


def _days_from_seconds(seconds) -> float:
    if not seconds:
        return 0.0
    return round(max(0.0, float(seconds)) / 86400, 1)


def renewal_report_charts(company, *, range_key="90", today=None, now=None) -> dict:
    """Client renewal report: remaining work, timing, new products, churn."""
    now = now or timezone.now()
    today = today or timezone.localtime(now).date()
    range_key = (range_key or "90").strip().lower()
    if range_key not in {"30", "90", "year"}:
        range_key = "90"
    window_start, range_label = _report_window(today, range_key)
    window_start_dt = _aware_start(window_start)

    default_template = company.reminder_templates.filter(is_default=True).first()
    default_phases = (
        default_template.schedule
        if default_template and default_template.schedule
        else recommended_schedule()
    )

    subscriptions = list(
        company.subscriptions.select_related(
            "client",
            "product",
            "client__reminder_template",
        ).prefetch_related("expiry_dispatches")
    )
    invites = list(
        SubscriptionRenewalInvite.objects.filter(
            company=company,
            email_sent_at__isnull=False,
        ).prefetch_related("lines", "new_products", "recipients")
    )

    sub_by_id = {sub.pk: sub for sub in subscriptions}
    invites_by_sub: dict[int, list] = defaultdict(list)
    for invite in invites:
        seen = set()
        for line in invite.lines.all():
            sub_id = line.subscription_id
            if sub_id in seen:
                continue
            seen.add(sub_id)
            invites_by_sub[sub_id].append(invite)
    for group in invites_by_sub.values():
        group.sort(key=lambda item: item.email_sent_at or item.created_at)

    pending_rows = []
    silent_rows = []
    lost_rows = []
    timing_rows = []
    send_delay_secs: list[float] = []
    deal_delay_secs: list[float] = []
    remaining_buckets = Counter()
    pending_renewals = 0
    billing_at_risk = Decimal("0")
    win_in_window = 0
    lost_in_window = 0
    qty_up = qty_same = qty_down = 0
    client_send: dict[str, list[float]] = defaultdict(list)
    client_deal: dict[str, list[float]] = defaultdict(list)
    trend_sent = Counter()
    trend_submitted = Counter()
    trend_win = Counter()
    trend_lost = Counter()

    funnel_sent: set[int] = set()
    funnel_opened: set[int] = set()
    funnel_submitted: set[int] = set()
    funnel_deal: set[int] = set()

    window_invites = [
        invite
        for invite in invites
        if invite.email_sent_at and invite.email_sent_at >= window_start_dt
    ]
    for invite in window_invites:
        month_key = _month_start(_local_date(invite.email_sent_at) or today)
        trend_sent[month_key] += 1
        if invite.submitted_at:
            submitted_month = _month_start(_local_date(invite.submitted_at) or today)
            trend_submitted[submitted_month] += 1
        counted_lines: set[int] = set()
        for line in invite.lines.all():
            sub = sub_by_id.get(line.subscription_id)
            if sub is None:
                continue
            funnel_sent.add(sub.pk)
            if _invite_opened(invite):
                funnel_opened.add(sub.pk)
            if invite.submitted_at:
                funnel_submitted.add(sub.pk)
            if sub.deal_status in _BLOCKING_DEALS:
                funnel_deal.add(sub.pk)
            if (
                invite.submitted_at
                and line.renewal_count is not None
                and line.pk not in counted_lines
            ):
                counted_lines.add(line.pk)
                current = int(line.current_quantity or 0)
                requested = int(line.renewal_count)
                if requested > current:
                    qty_up += 1
                elif requested < current:
                    qty_down += 1
                else:
                    qty_same += 1

    for sub in subscriptions:
        client_name = sub.client.name if sub.client_id else "—"
        product_name = sub.product.name if sub.product_id else "—"
        days_left = (sub.end_date - today).days if sub.end_date else None
        deal = sub.deal_status or ""
        decided_on = sub.deal_status_set_at
        decided_day = _local_date(decided_on)

        if decided_on and decided_on >= window_start_dt:
            month_key = _month_start(decided_day or today)
            if deal == ClientSubscription.DealStatus.WIN:
                win_in_window += 1
                trend_win[month_key] += 1
            elif deal == ClientSubscription.DealStatus.LOST:
                lost_in_window += 1
                trend_lost[month_key] += 1

        is_pending = (
            sub.status == ClientSubscription.Status.ACTIVE
            and sub.end_date is not None
            and deal not in _BLOCKING_DEALS
            and 0 <= (days_left or -1) <= _PENDING_HORIZON_DAYS
        )
        is_silent = (
            sub.end_date is not None
            and days_left is not None
            and days_left < 0
            and deal == ""
            and sub.status in (
                ClientSubscription.Status.ACTIVE,
                ClientSubscription.Status.ENDED,
            )
        )
        if is_pending:
            pending_renewals += 1
            billing_at_risk += Decimal(sub.billing_amount or 0)
            if days_left <= 7:
                remaining_buckets["0–7 days"] += 1
            elif days_left <= 15:
                remaining_buckets["8–15 days"] += 1
            elif days_left <= 30:
                remaining_buckets["16–30 days"] += 1
            else:
                remaining_buckets["31–60 days"] += 1
        if is_silent:
            remaining_buckets["Expired"] += 1

        cycle_invites = []
        first_sent = None
        last_sent = None
        opened = False
        submitted_at = None
        if sub.end_date:
            early_days = _early_window_days(sub.client, default_phases)
            cycle_start = sub.end_date - timedelta(days=early_days + 7)
            for invite in invites_by_sub.get(sub.pk, []):
                sent_day = _local_date(invite.email_sent_at)
                if sent_day and sent_day >= cycle_start:
                    cycle_invites.append(invite)
            for dispatch in sub.expiry_dispatches.all():
                if dispatch.sent_on and dispatch.sent_on >= cycle_start:
                    if first_sent is None or dispatch.sent_at < first_sent:
                        first_sent = dispatch.sent_at
            if cycle_invites:
                first_invite = min(cycle_invites, key=lambda item: item.email_sent_at)
                last_invite = max(cycle_invites, key=lambda item: item.email_sent_at)
                if first_sent is None or first_invite.email_sent_at < first_sent:
                    first_sent = first_invite.email_sent_at
                last_sent = last_invite.email_sent_at
                opened = any(_invite_opened(item) for item in cycle_invites)
                submitted_times = [
                    item.submitted_at for item in cycle_invites if item.submitted_at
                ]
                submitted_at = min(submitted_times) if submitted_times else None
                eligible_dt = _aware_start(sub.end_date - timedelta(days=early_days))
                send_delay = max(0.0, (first_sent - eligible_dt).total_seconds())
                in_timing_window = (
                    first_sent >= window_start_dt
                    or (decided_on is not None and decided_on >= window_start_dt)
                )
                deal_delay = None
                if decided_on:
                    deal_delay = max(0.0, (decided_on - first_sent).total_seconds())
                if in_timing_window:
                    send_delay_secs.append(send_delay)
                    client_send[client_name].append(send_delay)
                    if deal_delay is not None:
                        deal_delay_secs.append(deal_delay)
                        client_deal[client_name].append(deal_delay)
                    timing_rows.append(
                        {
                            "client": client_name,
                            "product": product_name,
                            "email_sent": _fmt_dt(first_sent),
                            "opened": "Yes" if opened else "No",
                            "submitted": _fmt_dt(submitted_at),
                            "deal_set": _fmt_dt(decided_on),
                            "send_delay": _duration_or_dash(send_delay),
                            "deal_delay": _duration_or_dash(deal_delay),
                            "send_delay_secs": send_delay,
                            "deal_delay_secs": deal_delay if deal_delay is not None else -1,
                            "deal": _deal_label(deal),
                            "deal_status": deal or "pending",
                        }
                    )

        row_base = {
            "client": client_name,
            "product": product_name,
            "end_date": _fmt_dt(sub.end_date),
            "days_left": _fmt_days_left(days_left),
            "days_left_sort": days_left if days_left is not None else 9999,
            "last_email": _fmt_dt(last_sent),
            "opened": "Yes" if opened else "No",
            "submitted": "Yes" if submitted_at else "No",
            "deal": _deal_label(deal),
            "deal_status": deal or "pending",
            "amount": _fmt_money(sub.billing_amount),
        }

        if is_pending or is_silent:
            pending_rows.append(row_base)
        if is_silent:
            silent_rows.append(
                {
                    **row_base,
                    "reason": "Silent — expired, no decision",
                    "form_filled": "Yes" if submitted_at else "No",
                }
            )
        if deal == ClientSubscription.DealStatus.LOST and (
            (decided_on and decided_on >= window_start_dt)
            or (sub.end_date and window_start <= sub.end_date <= today)
        ):
            lost_rows.append(
                {
                    **row_base,
                    "reason": "Lost",
                    "form_filled": "Yes" if submitted_at else "No",
                }
            )

    pending_rows.sort(key=lambda row: row["days_left_sort"])
    timing_rows.sort(
        key=lambda row: (
            -(row["deal_delay_secs"] if row["deal_delay_secs"] >= 0 else -1),
            -row["send_delay_secs"],
        )
    )
    did_not_renew_rows = silent_rows + lost_rows
    did_not_renew_rows.sort(key=lambda row: row["days_left_sort"])

    new_product_rows = []
    new_qty_by_product: Counter = Counter()
    new_product_clients: set[str] = set()
    new_product_qty = 0
    new_products = SubscriptionRenewalInviteNewProduct.objects.filter(
        invite__company=company,
        invite__submitted_at__gte=window_start_dt,
    ).select_related("invite", "product").prefetch_related("invite__lines")
    for item in new_products:
        qty = int(item.quantity or 0)
        name = item.product_name or (item.product.name if item.product_id else "Product")
        client_label = item.company_name or "—"
        existing = ", ".join(
            dict.fromkeys(
                line.product_name for line in item.invite.lines.all() if line.product_name
            )
        )
        new_qty_by_product[name] += qty
        new_product_clients.add(client_label)
        new_product_qty += qty
        new_product_rows.append(
            {
                "client": client_label,
                "existing": existing or "—",
                "product": name,
                "quantity": qty,
                "date": _fmt_dt(item.invite.submitted_at),
            }
        )
    new_product_rows.sort(key=lambda row: row["date"], reverse=True)
    top_new = new_qty_by_product.most_common(8)

    client_timing = []
    for name in set(client_send) | set(client_deal):
        avg_send = _avg(client_send.get(name) or [])
        avg_deal = _avg(client_deal.get(name) or [])
        client_timing.append(
            {
                "client": name,
                "send_days": _days_from_seconds(avg_send),
                "deal_days": _days_from_seconds(avg_deal),
                "deal_sort": avg_deal if avg_deal is not None else -1,
            }
        )
    client_timing.sort(key=lambda row: (row["deal_sort"] < 0, -(row["deal_sort"] or 0)))
    client_timing = client_timing[:_TIMING_CHART_LIMIT]

    months = [_month_start(today, months_back=i) for i in range(5, -1, -1)]
    remaining_labels = ["Expired", "0–7 days", "8–15 days", "16–30 days", "31–60 days"]
    decided_in_window = win_in_window + lost_in_window
    avg_send = _avg(send_delay_secs)
    avg_deal = _avg(deal_delay_secs)

    remaining_total = pending_renewals + len(silent_rows)
    return {
        "range_key": range_key,
        "range_label": range_label,
        "pending_renewals": pending_renewals,
        "silent_expired": len(silent_rows),
        "lost_count": lost_in_window,
        "did_not_renew": len(silent_rows) + lost_in_window,
        "win_count": win_in_window,
        "resub_rate": _pct(win_in_window, decided_in_window),
        "qty_up": qty_up,
        "qty_same": qty_same,
        "qty_down": qty_down,
        "new_product_clients": len(new_product_clients),
        "new_product_qty": new_product_qty,
        "billing_at_risk": _fmt_money(billing_at_risk),
        "avg_send_delay": _duration_or_dash(avg_send),
        "avg_deal_delay": _duration_or_dash(avg_deal),
        "remaining_total": remaining_total,
        "remaining_rows": pending_rows,
        "timing_rows": timing_rows,
        "new_product_rows": new_product_rows,
        "did_not_renew_rows": did_not_renew_rows,
        "charts": {
            "funnel_labels": ["Sent", "Opened", "Submitted", "Deal set"],
            "funnel_values": [
                len(funnel_sent),
                len(funnel_opened),
                len(funnel_submitted),
                len(funnel_deal),
            ],
            "deal_labels": ["Win", "Lost", "Pending"],
            "deal_values": [win_in_window, lost_in_window, pending_renewals],
            "deal_total": win_in_window + lost_in_window + pending_renewals,
            "remaining_labels": remaining_labels,
            "remaining_values": [int(remaining_buckets.get(label, 0)) for label in remaining_labels],
            "timing_labels": [row["client"] for row in client_timing],
            "timing_send_days": [row["send_days"] for row in client_timing],
            "timing_deal_days": [row["deal_days"] for row in client_timing],
            "new_product_labels": [name for name, _qty in top_new],
            "new_product_values": [int(qty) for _name, qty in top_new],
            "trend_labels": [_month_label(month) for month in months],
            "trend_sent": [int(trend_sent.get(month, 0)) for month in months],
            "trend_submitted": [int(trend_submitted.get(month, 0)) for month in months],
            "trend_win": [int(trend_win.get(month, 0)) for month in months],
            "trend_lost": [int(trend_lost.get(month, 0)) for month in months],
        },
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
