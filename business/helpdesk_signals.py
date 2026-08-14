from django.dispatch import receiver
from helpdesk.models import FollowUp
from helpdesk.signals import new_ticket_done

from business.email_body_clean import clean_inbound_email_body
from business.models import SupportTicketMeta


@receiver(new_ticket_done)
def ensure_meta_for_helpdesk_ticket(sender, ticket, **kwargs):
    """Email/IMAP (and other helpdesk creates) get CRM meta with is_internal=False."""
    SupportTicketMeta.objects.get_or_create(
        ticket=ticket,
        defaults={
            "department": "general",
            "is_internal": False,
            "remarks": "",
        },
    )


@receiver(new_ticket_done)
def scrub_external_caution_from_ticket(sender, ticket, **kwargs):
    """
    Strip M365 external-mail CAUTION banners from description / first follow-ups.

    Covers django-helpdesk IMAP imports as well as our Graph path.
    """
    cleaned = clean_inbound_email_body(ticket.description or "")
    if cleaned != (ticket.description or ""):
        ticket.description = cleaned
        ticket.save(update_fields=["description"])

    for fu in FollowUp.objects.filter(ticket=ticket).exclude(comment=""):
        c = clean_inbound_email_body(fu.comment or "")
        if c != (fu.comment or ""):
            fu.comment = c
            fu.save(update_fields=["comment"])