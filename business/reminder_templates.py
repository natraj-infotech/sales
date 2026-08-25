"""CRUD helpers for company reminder templates."""

from __future__ import annotations

import json

from django.core.exceptions import ValidationError
from django.db import transaction

from accounts.models import CompanyProfile
from business.models import Client, ReminderTemplate
from business.reminder_schedule import (
    DEFAULT_REMINDER_CYCLE_NAME,
    default_reminder_cycle_schedule,
    default_reminder_cycle_steps,
    normalize_schedule,
    notify_days_csv,
    recommended_schedule,
    schedule_bounds,
)


def repair_default_reminder_template(template: ReminderTemplate) -> ReminderTemplate:
    """Keep the default cycle name and schedule locked to the product default."""
    if not template.is_default:
        return template
    fixed_schedule = default_reminder_cycle_schedule()
    updates: list[str] = []
    if template.name != DEFAULT_REMINDER_CYCLE_NAME:
        template.name = DEFAULT_REMINDER_CYCLE_NAME
        updates.append("name")
    if template.schedule != fixed_schedule:
        template.schedule = fixed_schedule
        updates.append("schedule")
    if updates:
        updates.append("updated_at")
        template.save(update_fields=updates)
    return template


def ensure_default_reminder_template(company: CompanyProfile) -> ReminderTemplate:
    existing = (
        ReminderTemplate.objects.filter(company=company, is_default=True)
        .order_by("pk")
        .first()
    )
    if existing:
        return repair_default_reminder_template(existing)
    return ReminderTemplate.objects.create(
        company=company,
        name=DEFAULT_REMINDER_CYCLE_NAME,
        schedule=default_reminder_cycle_schedule(),
        is_default=True,
    )


def company_reminder_bounds(company) -> tuple[int, int]:
    from business.models import ReminderTemplate

    templates = ReminderTemplate.objects.filter(company=company)
    if not templates.exists():
        ensure_default_reminder_template(company)
        templates = ReminderTemplate.objects.filter(company=company)

    lows: list[int] = []
    highs: list[int] = []
    for template in templates:
        try:
            if template.is_default:
                phases = default_reminder_cycle_schedule()
            else:
                phases = normalize_schedule(template.schedule or [])
        except ValidationError:
            continue
        low, high = schedule_bounds(phases)
        lows.append(low)
        highs.append(high)
    if not lows:
        return schedule_bounds(default_reminder_cycle_schedule())
    return min(lows), max(highs)


def sync_company_notify_days(company: CompanyProfile) -> None:
    default = ensure_default_reminder_template(company)
    company.expiry_notify_at_days = notify_days_csv(default.schedule or [])
    company.save(update_fields=["expiry_notify_at_days", "updated_at"])


@transaction.atomic
def create_reminder_template(company: CompanyProfile, name: str) -> ReminderTemplate:
    ensure_default_reminder_template(company)
    clean_name = (name or "").strip() or "New template"
    return ReminderTemplate.objects.create(
        company=company,
        name=clean_name,
        schedule=default_reminder_cycle_schedule(),
        is_default=False,
    )


@transaction.atomic
def delete_reminder_template(template: ReminderTemplate) -> None:
    if template.is_default:
        raise ValidationError("The default reminder cycle cannot be deleted.")
    default = ensure_default_reminder_template(template.company)
    Client.objects.filter(reminder_template=template).update(
        reminder_template=default
    )
    template.delete()


def parse_template_schedule(raw: str | list) -> list[dict]:
    if isinstance(raw, list):
        return normalize_schedule(raw)
    return normalize_schedule(json.loads(raw or "[]"))


@transaction.atomic
def save_reminder_templates(
    company: CompanyProfile,
    *,
    template_payloads: list[dict],
    client_assignments: dict[int, list[int]] | None = None,
) -> None:
    ensure_default_reminder_template(company)
    templates = {
        template.pk: template
        for template in ReminderTemplate.objects.filter(company=company)
    }

    for payload in template_payloads:
        template_id = payload.get("id")
        if not template_id or template_id not in templates:
            continue
        template = templates[template_id]
        if template.is_default:
            repair_default_reminder_template(template)
            continue
        name = (payload.get("name") or "").strip()
        if name:
            template.name = name[:120]
        template.schedule = parse_template_schedule(payload.get("schedule") or "[]")
        template.save(update_fields=["name", "schedule", "updated_at"])

    if client_assignments is not None:
        claimed: set[int] = set()
        default_template = ensure_default_reminder_template(company)
        ordered_templates = sorted(
            templates.values(),
            key=lambda item: (item.is_default, item.name.lower()),
        )

        for template in ordered_templates:
            if template.is_default:
                continue
            template_id = template.pk
            client_ids = client_assignments.get(template_id, [])
            valid_ids = list(
                Client.objects.filter(company=company, pk__in=client_ids).values_list(
                    "pk", flat=True
                )
            )
            for client_id in valid_ids:
                if client_id in claimed:
                    continue
                claimed.add(client_id)
                Client.objects.filter(pk=client_id).update(reminder_template=template)

        default_ids = client_assignments.get(default_template.pk, [])
        if default_ids:
            valid_default_ids = list(
                Client.objects.filter(company=company, pk__in=default_ids).values_list(
                    "pk", flat=True
                )
            )
            for client_id in valid_default_ids:
                if client_id in claimed:
                    continue
                claimed.add(client_id)
                Client.objects.filter(pk=client_id).update(
                    reminder_template=default_template
                )

        Client.objects.filter(company=company).exclude(pk__in=claimed).update(
            reminder_template=default_template
        )

    sync_company_notify_days(company)


__all__ = [
    "DEFAULT_REMINDER_CYCLE_NAME",
    "company_reminder_bounds",
    "create_reminder_template",
    "default_reminder_cycle_steps",
    "delete_reminder_template",
    "ensure_default_reminder_template",
    "repair_default_reminder_template",
    "save_reminder_templates",
    "sync_company_notify_days",
]
