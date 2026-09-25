"""Persistent WhatsApp chat session state (multi-step flows)."""

from __future__ import annotations

from django.db import transaction

from business.models import Lead

from .models import WhatsAppConversation


def get_or_create_session(lead: Lead) -> WhatsAppConversation:
    session, _created = WhatsAppConversation.objects.get_or_create(lead=lead)
    return session


@transaction.atomic
def reset_session(session: WhatsAppConversation) -> None:
    session.phase = WhatsAppConversation.Phase.IDLE
    session.flow = ""
    session.step = ""
    session.draft = {}
    session.save(update_fields=["phase", "flow", "step", "draft", "updated_at"])


@transaction.atomic
def start_flow(session: WhatsAppConversation, *, flow: str, step: str, draft: dict | None = None) -> None:
    session.phase = WhatsAppConversation.Phase.ACTIVE
    session.flow = flow
    session.step = step
    session.draft = draft or {}
    session.save(update_fields=["phase", "flow", "step", "draft", "updated_at"])


@transaction.atomic
def advance_step(session: WhatsAppConversation, *, step: str, draft_updates: dict | None = None) -> None:
    session.step = step
    if draft_updates:
        merged = dict(session.draft or {})
        merged.update(draft_updates)
        session.draft = merged
    session.save(update_fields=["step", "draft", "updated_at"])
