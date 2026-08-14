"""Subject-keyword → priority matching for support tickets."""

from __future__ import annotations

from accounts.models import CompanyProfile
from business.models import SupportTicket, TicketPriorityRule

DEFAULT_PRIORITY_RULES: list[tuple[str, str, int]] = [
    ("urgent", "urgent, asap, critical, outage, down", 0),
    ("high", "high, important, billing error, payment failed", 1),
    ("low", "question, info, enquiry, inquiry, fyi", 2),
]


def ensure_default_priority_rules(company: CompanyProfile) -> None:
    """Seed starter keyword rules once per company (only if none exist)."""
    if TicketPriorityRule.objects.filter(company=company).exists():
        return
    TicketPriorityRule.objects.bulk_create(
        [
            TicketPriorityRule(
                company=company,
                priority=priority,
                keywords=keywords,
                sort_order=order,
                is_active=True,
            )
            for priority, keywords, order in DEFAULT_PRIORITY_RULES
        ]
    )


def match_priority_from_subject(
    company: CompanyProfile,
    subject: str,
    *,
    default: str = SupportTicket.Priority.MEDIUM,
) -> tuple[str, bool]:
    """
    Return (priority, matched).

    First active rule whose keyword appears in the subject wins (by sort_order).
    """
    ensure_default_priority_rules(company)
    text = (subject or "").lower()
    if not text:
        return default, False

    rules = (
        TicketPriorityRule.objects.filter(company=company, is_active=True)
        .order_by("sort_order", "id")
    )
    for rule in rules:
        for keyword in rule.keyword_list():
            if keyword and keyword in text:
                return rule.priority, True
    return default, False


def rules_payload_for_ui(company: CompanyProfile) -> list[dict]:
    """JSON-friendly rules for client-side priority suggestion on the create form."""
    ensure_default_priority_rules(company)
    rows = []
    for rule in TicketPriorityRule.objects.filter(
        company=company, is_active=True
    ).order_by("sort_order", "id"):
        rows.append(
            {
                "priority": rule.priority,
                "keywords": rule.keyword_list(),
            }
        )
    return rows
