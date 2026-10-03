import logging

from django.dispatch import receiver
from helpdesk.models import FollowUp
from helpdesk.signals import new_ticket_done, update_ticket_done

from business.email_body_clean import clean_inbound_email_body
from business.models import SupportTicketMeta

logger = logging.getLogger(__name__)


@receiver(new_ticket_done)
def ensure_meta_for_helpdesk_ticket(sender, ticket, **kwargs):
    """Email/IMAP (and other helpdesk creates) get CRM meta with is_internal=False."""
    try:
        SupportTicketMeta.objects.get_or_create(
            ticket=ticket,
            defaults={
                "department": "general",
                "is_internal": False,
                "remarks": "",
            },
        )
    except Exception:
        logger.exception(
            "SupportTicketMeta create failed ticket_id=%s",
            getattr(ticket, "pk", None),
        )


@receiver(new_ticket_done)
def scrub_external_caution_from_ticket(sender, ticket, **kwargs):
    """
    Strip M365 external-mail CAUTION banners from description / first follow-ups.

    Covers django-helpdesk IMAP imports as well as our Graph path.
    """
    try:
        cleaned = clean_inbound_email_body(ticket.description or "")
        if cleaned != (ticket.description or ""):
            ticket.description = cleaned
            ticket.save(update_fields=["description"])

        for fu in FollowUp.objects.filter(ticket=ticket).exclude(comment=""):
            c = clean_inbound_email_body(fu.comment or "")
            if c != (fu.comment or ""):
                fu.comment = c
                fu.save(update_fields=["comment"])
    except Exception:
        logger.exception(
            "Email caution scrub failed ticket_id=%s",
            getattr(ticket, "pk", None),
        )


_EMAIL_TICKET_SENDERS = {"helpdesk_inbox", "create_object_from_email_message"}


@receiver(new_ticket_done)
def email_sender_ticket_confirmation(sender, ticket, **kwargs):
    """Tell the person who emailed support that a ticket was created."""
    if sender not in _EMAIL_TICKET_SENDERS:
        return
    try:
        from business.helpdesk_assign_mail import queue_ticket_created_email
        from business.team_notifications import company_for_ticket

        company = company_for_ticket(ticket)
        if company is None:
            return
        queue_ticket_created_email(company=company, ticket=ticket)
    except Exception:
        logger.exception(
            "Ticket confirmation email failed for ticket_id=%s",
            getattr(ticket, "pk", None),
        )


@receiver(new_ticket_done)
def notify_team_of_new_ticket(sender, ticket, **kwargs):
    """Inbox imports (Graph and IMAP) show up on the team bell."""
    try:
        from business.team_notifications import notify_new_ticket

        notify_new_ticket(ticket)
    except Exception:
        logger.exception(
            "Team notification failed for new ticket_id=%s",
            getattr(ticket, "pk", None),
        )


@receiver(update_ticket_done)
def notify_team_of_customer_reply(sender, followup, **kwargs):
    """IMAP replies to an existing ticket (helpdesk also reopens closed ones)."""
    if sender != "create_object_from_email_message":
        return
    try:
        from business.team_notifications import notify_ticket_reply

        notify_ticket_reply(followup)
    except Exception:
        logger.exception(
            "Team notification failed for ticket reply followup_id=%s",
            getattr(followup, "pk", None),
        )
