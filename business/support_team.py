"""Support team membership and auto-assignment of new tickets."""

from __future__ import annotations

import logging

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction
from django.db.models import Count, Q

from accounts.models import CompanyProfile
from accounts.permissions import company_users
from business.models import SupportTeam

logger = logging.getLogger(__name__)

AUTO_ASSIGN_COMMENT = "Auto-assigned (support team, fewest open tickets)."


def get_support_team(company: CompanyProfile) -> SupportTeam:
    team, _created = SupportTeam.objects.get_or_create(company=company)
    return team


def team_members(company: CompanyProfile):
    """Support team members who still belong to the company."""
    User = get_user_model()
    team = SupportTeam.objects.filter(company=company).first()
    if team is None:
        return User.objects.none()
    return team.members.filter(id__in=company.company_user_ids()).order_by("email")


def assignable_users(company: CompanyProfile, *, include_ids=()):
    """
    Users offered in the manual assignee dropdown.

    Support team members once the team has people, otherwise every company user.
    ``include_ids`` keeps current assignees selectable so saving a row never
    silently unassigns someone outside the team.
    """
    User = get_user_model()
    members = team_members(company)
    if not members.exists():
        return company_users(company)
    extra = {pk for pk in include_ids if pk}
    return User.objects.filter(
        Q(id__in=members.values("id")) | Q(id__in=extra)
    ).order_by("email")


def save_team(
    company: CompanyProfile, *, member_ids, auto_assign_enabled: bool
) -> SupportTeam:
    team = get_support_team(company)
    allowed = company.company_user_ids()
    clean_ids = {int(pk) for pk in member_ids if str(pk).isdigit()} & allowed
    team.members.set(clean_ids)
    team.auto_assign_enabled = bool(auto_assign_enabled)
    if team.last_assigned_id and team.last_assigned_id not in clean_ids:
        team.last_assigned = None
    team.save()
    return team


def set_auto_assign(company: CompanyProfile, enabled: bool) -> SupportTeam:
    team = get_support_team(company)
    team.auto_assign_enabled = bool(enabled)
    team.save(update_fields=["auto_assign_enabled", "updated_at"])
    return team


def team_picker_context(company: CompanyProfile) -> dict:
    """People split into company team / support team for the support team modal."""
    team = get_support_team(company)
    users = list(company_users(company))
    member_ids = set(team_members(company).values_list("id", flat=True))
    counts = open_ticket_counts(company, [u.pk for u in users])
    people = [
        {
            "id": u.pk,
            "email": u.email or u.get_username(),
            "name": u.get_full_name().strip(),
            "open_tickets": counts.get(u.pk, 0),
        }
        for u in users
    ]
    return {
        "team": team,
        "team_available": [p for p in people if p["id"] not in member_ids],
        "team_members": [p for p in people if p["id"] in member_ids],
    }


def open_ticket_counts(company: CompanyProfile, user_ids) -> dict[int, int]:
    from helpdesk.models import Ticket

    from business.helpdesk_bridge import company_queue_slug_prefix

    rows = (
        Ticket.objects.filter(
            queue__slug__startswith=company_queue_slug_prefix(company),
            assigned_to_id__in=user_ids,
            status__in=(Ticket.OPEN_STATUS, Ticket.REOPENED_STATUS),
        )
        .values("assigned_to_id")
        .annotate(n=Count("id"))
    )
    return {r["assigned_to_id"]: r["n"] for r in rows}


def pick_engineer(team: SupportTeam):
    """Least open tickets wins; ties rotate after the last engineer picked."""
    members = list(
        team.members.filter(id__in=team.company.company_user_ids()).order_by("id")
    )
    if not members:
        return None
    counts = open_ticket_counts(team.company, [u.pk for u in members])
    fewest = min(counts.get(u.pk, 0) for u in members)
    tied = [u for u in members if counts.get(u.pk, 0) == fewest]
    last_id = team.last_assigned_id or 0
    for user in tied:
        if user.pk > last_id:
            return user
    return tied[0]


def _is_oem(ticket) -> bool:
    try:
        return bool(ticket.crm_meta.is_oem)
    except ObjectDoesNotExist:
        return False


def auto_assign_ticket(company: CompanyProfile, ticket):
    """
    Assign a new ticket to a support engineer if auto-assign is on.

    Returns the engineer, or None when auto-assign is off, the team is empty,
    the ticket is an OEM ticket, or it already has an assignee.
    """
    from business.helpdesk_bridge import set_ticket_assignee

    if ticket.assigned_to_id or _is_oem(ticket):
        return None
    with transaction.atomic():
        team = (
            SupportTeam.objects.select_for_update()
            .select_related("company")
            .filter(company=company, auto_assign_enabled=True)
            .first()
        )
        if team is None:
            return None
        engineer = pick_engineer(team)
        if engineer is None:
            return None
        set_ticket_assignee(
            ticket, engineer, user=AnonymousUser(), comment=AUTO_ASSIGN_COMMENT
        )
        team.last_assigned = engineer
        team.save(update_fields=["last_assigned", "updated_at"])

    try:
        from business.team_notifications import notify_ticket_assigned

        notify_ticket_assigned(ticket, engineer)
    except Exception:
        logger.exception(
            "Team notification failed for auto-assigned ticket_id=%s", ticket.pk
        )
    try:
        from business.helpdesk_assign_mail import queue_assignment_emails

        queue_assignment_emails(company=company, ticket=ticket, assignee=engineer)
    except Exception:
        logger.exception(
            "Assignment email failed for auto-assigned ticket_id=%s", ticket.pk
        )
    return engineer
