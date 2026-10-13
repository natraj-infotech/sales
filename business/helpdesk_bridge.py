"""Bridge between Ticket360 UI and django-helpdesk models."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.text import slugify
from helpdesk.models import FollowUp, Queue, Ticket
from helpdesk.update_ticket import update_ticket

from accounts.models import CompanyProfile
from business.models import SupportTicketMeta
from business.support_inbox import _detect_imap_host

# UI status keys ↔ helpdesk integer statuses
STATUS_UI_TO_HD = {
    "open": Ticket.OPEN_STATUS,
    "in_progress": Ticket.REOPENED_STATUS,
    "resolved": Ticket.RESOLVED_STATUS,
    "closed": Ticket.CLOSED_STATUS,
}
STATUS_HD_TO_UI = {v: k for k, v in STATUS_UI_TO_HD.items()}

STATUS_CHOICES = (
    ("open", "Open"),
    ("in_progress", "In progress"),
    ("resolved", "Resolved"),
    ("closed", "Closed"),
)

PRIORITY_UI_TO_HD = {
    "urgent": 1,
    "high": 2,
    "medium": 3,
    "low": 4,
}
PRIORITY_HD_TO_UI = {
    1: "urgent",
    2: "high",
    3: "medium",
    4: "low",
    5: "low",
}
PRIORITY_LABELS = {
    "urgent": "Urgent",
    "high": "High",
    "medium": "Medium",
    "low": "Low",
}

DEPARTMENT_CHOICES = (
    ("general", "General"),
    ("billing", "Billing"),
    ("technical", "Technical"),
    ("sales", "Sales"),
)
DEPARTMENT_LABELS = dict(DEPARTMENT_CHOICES)


def company_queue_slug(company: CompanyProfile) -> str:
    base = slugify(company.company_name)[:32] or "company"
    return f"c{company.pk}-{base}"[:50]


@transaction.atomic
def sync_company_queue(company: CompanyProfile) -> Queue:
    """Ensure a helpdesk Queue exists for the company and IMAP settings match."""
    slug = company_queue_slug(company)
    title = (company.company_name or f"Company {company.pk}")[:100]
    queue, _created = Queue.objects.get_or_create(
        slug=slug,
        defaults={
            "title": title,
            "email_address": company.support_email or None,
            "allow_public_submission": False,
            "allow_email_submission": False,
        },
    )
    if queue.title != title:
        queue.title = title

    support_email = (company.support_email or "").strip()
    app_password = (company.support_email_app_password or "").strip()
    queue.email_address = support_email or None

    if support_email and app_password:
        host = _detect_imap_host(support_email)
        from business.support_inbox import _is_microsoft_host

        queue.email_box_type = "imap"
        queue.email_box_host = host
        queue.email_box_port = int(getattr(settings, "SUPPORT_IMAP_PORT", 993) or 993)
        queue.email_box_ssl = True
        queue.email_box_user = support_email
        queue.email_box_pass = app_password
        queue.email_box_imap_folder = getattr(settings, "SUPPORT_IMAP_MAILBOX", "INBOX") or "INBOX"
        queue.email_box_interval = 1
        # M365 often blocks basic IMAP auth; Graph path imports tickets instead.
        queue.allow_email_submission = not _is_microsoft_host(host)
    else:
        queue.allow_email_submission = False

    queue.save()
    return queue


def get_company_queue(company: CompanyProfile) -> Queue:
    return sync_company_queue(company)


def tickets_for_company(company: CompanyProfile, *, oem: bool | None = None):
    queue = get_company_queue(company)
    qs = (
        Ticket.objects.filter(queue=queue)
        .select_related(
            "assigned_to",
            "queue",
            "crm_meta",
            "crm_meta__created_by",
            "crm_meta__transferred_to",
        )
        .order_by("-created", "-id")
    )
    if oem is True:
        qs = qs.filter(crm_meta__is_oem=True)
    elif oem is False:
        qs = qs.filter(Q(crm_meta__isnull=True) | Q(crm_meta__is_oem=False))
    return qs


def get_company_ticket(company: CompanyProfile, pk: int) -> Ticket:
    queue = get_company_queue(company)
    return Ticket.objects.select_related(
        "assigned_to",
        "queue",
        "crm_meta",
        "crm_meta__created_by",
        "crm_meta__transferred_to",
    ).get(pk=pk, queue=queue)


@dataclass
class MailFileView:
    name: str
    url: str
    is_image: bool


@dataclass
class CommentView:
    user_email: str
    body: str
    internal_note: bool
    created_at: timezone.datetime
    attachments: list[MailFileView] = field(default_factory=list)
    outbound: bool = False
    # Raw files, loaded only when a caller asks to attach them to an email.
    files: list[tuple[str, bytes, str]] = field(default_factory=list)

    @property
    def user(self):
        return type("U", (), {"email": self.user_email})()


def _is_image_attachment(attachment) -> bool:
    mime = (getattr(attachment, "mime_type", None) or "").lower()
    return mime.startswith("image/") and "svg" not in mime


def _mail_files(ticket_pk: int, followup) -> list[MailFileView]:
    from django.urls import reverse

    files: list[MailFileView] = []
    for attachment in followup.followupattachment_set.all():
        name = attachment.filename or "attachment"
        files.append(
            MailFileView(
                name=name,
                url=reverse(
                    "support_ticket_attachment",
                    args=[ticket_pk, attachment.pk],
                ),
                is_image=_is_image_attachment(attachment),
            )
        )
    return files


class TicketView:
    """Present helpdesk Ticket with the same attribute names Ticket360 templates expect."""

    def __init__(self, ticket: Ticket, meta: SupportTicketMeta | None = None):
        from django.core.exceptions import ObjectDoesNotExist

        self._ticket = ticket
        self._meta = meta
        if self._meta is None:
            try:
                self._meta = ticket.crm_meta
            except ObjectDoesNotExist:
                self._meta = None

    @property
    def pk(self):
        return self._ticket.pk

    @property
    def id(self):
        return self._ticket.pk

    @property
    def reference(self):
        return f"TK-{self._ticket.pk:05d}"

    @property
    def subject(self):
        return self._ticket.title

    @property
    def description(self):
        from business.email_body_clean import clean_inbound_email_body

        return clean_inbound_email_body(self._ticket.description or "")

    @property
    def status(self):
        return STATUS_HD_TO_UI.get(self._ticket.status, "open")

    def get_status_display(self):
        return dict(STATUS_CHOICES).get(self.status, self.status)

    @property
    def department(self):
        return (self._meta.department if self._meta else "general") or "general"

    def get_department_display(self):
        return DEPARTMENT_LABELS.get(self.department, self.department.title())

    @property
    def priority(self):
        return PRIORITY_HD_TO_UI.get(self._ticket.priority, "medium")

    def get_priority_display(self):
        return PRIORITY_LABELS.get(self.priority, self.priority.title())

    @property
    def assignee(self):
        return self._ticket.assigned_to

    @property
    def assignee_id(self):
        return self._ticket.assigned_to_id

    @property
    def remarks(self):
        return (self._meta.remarks if self._meta else "") or ""

    @property
    def source_sender(self):
        if self._meta and self._meta.is_internal:
            return ""
        return self._ticket.submitter_email or ""

    @property
    def customer_phone(self):
        return (self._meta.customer_phone if self._meta else "") or ""

    @property
    def customer_firm_name(self):
        return (self._meta.customer_firm_name if self._meta else "") or ""

    @property
    def source_channel(self):
        return (self._meta.source_channel if self._meta else "") or ""

    def get_source_channel_display(self):
        val = self.source_channel
        if not val:
            return ""
        return dict(SupportTicketMeta.SourceChannel.choices).get(val, val.replace("_", " ").title())

    @property
    def contact_display(self):
        """Email or mobile shown on the support dashboard."""
        phone = self.customer_phone
        if phone:
            return phone
        sender = self.source_sender
        if sender:
            return sender
        return getattr(self.created_by, "email", "") or ""

    @property
    def created_by(self):
        if self._meta and self._meta.created_by_id:
            return self._meta.created_by
        email = self._ticket.submitter_email or ""
        return type("U", (), {"email": email})()

    @property
    def created_at(self):
        return self._ticket.created

    @property
    def updated_at(self):
        return self._ticket.modified

    @property
    def completed_at(self):
        if self._meta and self._meta.completed_at:
            return self._meta.completed_at
        if self.status in ("resolved", "closed"):
            return self._ticket.modified
        return None

    @property
    def first_response_due_at(self):
        return self._meta.first_response_due_at if self._meta else None

    @property
    def resolve_due_at(self):
        return self._meta.resolve_due_at if self._meta else None

    @property
    def first_responded_at(self):
        return self._meta.first_responded_at if self._meta else None

    @property
    def sla(self):
        """Attached by wrap_* when company settings are provided."""
        return getattr(self, "_sla", None)

    @property
    def is_oem(self):
        return bool(self._meta and self._meta.is_oem)

    @property
    def oem_company_name(self):
        return (self._meta.oem_company_name if self._meta else "") or ""

    @property
    def oem_to_email(self):
        return (self._meta.oem_to_email if self._meta else "") or ""

    @property
    def oem_cc_emails(self):
        return (self._meta.oem_cc_emails if self._meta else "") or ""

    @property
    def subscription_id(self):
        return (self._meta.subscription_id if self._meta else "") or ""

    @property
    def transferred_to_id(self):
        return self._meta.transferred_to_id if self._meta else None

    @property
    def transferred_to_reference(self):
        pk = self.transferred_to_id
        if not pk:
            return ""
        return f"TK-{pk:05d}"


def wrap_ticket(ticket: Ticket, *, sla_settings=None) -> TicketView:
    view = TicketView(ticket)
    if sla_settings is not None:
        from business.sla import sla_status_for_meta

        view._sla = sla_status_for_meta(
            view._meta, settings_obj=sla_settings, ticket_status=view.status
        )
    return view


def wrap_tickets(tickets, *, sla_settings=None) -> list[TicketView]:
    return [wrap_ticket(t, sla_settings=sla_settings) for t in tickets]


def ensure_meta(
    ticket: Ticket,
    *,
    created_by=None,
    department: str = "general",
    remarks: str = "",
    is_internal: bool = True,
    is_oem: bool = False,
    oem_company_name: str = "",
    oem_to_email: str = "",
    oem_cc_emails: str = "",
    subscription_id: str = "",
    source_channel: str = "",
    customer_phone: str = "",
    customer_firm_name: str = "",
) -> SupportTicketMeta:
    meta, created = SupportTicketMeta.objects.get_or_create(
        ticket=ticket,
        defaults={
            "created_by": created_by,
            "department": department or "general",
            "remarks": remarks or "",
            "is_internal": is_internal,
            "is_oem": is_oem,
            "oem_company_name": oem_company_name or "",
            "oem_to_email": oem_to_email or "",
            "oem_cc_emails": oem_cc_emails or "",
            "subscription_id": subscription_id or "",
            "source_channel": source_channel or "",
            "customer_phone": customer_phone or "",
            "customer_firm_name": customer_firm_name or "",
        },
    )
    if not created:
        dirty = []
        if created_by and meta.created_by_id is None:
            meta.created_by = created_by
            dirty.append("created_by")
        if dirty:
            meta.save(update_fields=dirty)
    return meta


def create_internal_ticket(
    *,
    company: CompanyProfile,
    user,
    subject: str,
    description: str,
    department: str = "general",
    priority: str = "medium",
    priority_source: str = "manual",
) -> Ticket:
    from business.sla import apply_sla_to_ticket

    queue = get_company_queue(company)
    now = timezone.now()
    ui_priority = priority if priority in PRIORITY_UI_TO_HD else "medium"
    ticket = Ticket(
        title=subject[:200],
        queue=queue,
        created=now,
        modified=now,
        # Internal tickets have no external submitter; outbound mail is for email tickets.
        submitter_email=None,
        status=Ticket.OPEN_STATUS,
        description=description,
        priority=PRIORITY_UI_TO_HD.get(ui_priority, 3),
    )
    ticket.save()
    ensure_meta(
        ticket,
        created_by=user,
        department=department,
        is_internal=True,
    )
    FollowUp.objects.create(
        ticket=ticket,
        date=now,
        title="Ticket Opened",
        comment="",
        public=False,
        user=user,
        new_status=Ticket.OPEN_STATUS,
    )
    apply_sla_to_ticket(
        company,
        ticket,
        priority=ui_priority,
        priority_source=priority_source,
    )
    try:
        from business.support_team import auto_assign_ticket

        auto_assign_ticket(company, ticket)
    except Exception:
        import logging

        logging.getLogger(__name__).exception(
            "Auto-assign failed for internal ticket_id=%s", ticket.pk
        )
    try:
        from business.team_notifications import notify_new_ticket

        notify_new_ticket(ticket)
    except Exception:
        import logging

        logging.getLogger(__name__).exception(
            "Team notification failed for internal ticket_id=%s", ticket.pk
        )
    return ticket


def create_oem_ticket(
    *,
    company: CompanyProfile,
    user,
    subject: str,
    description: str,
    oem_company_name: str = "",
    oem_to_email: str = "",
    oem_cc_emails: str = "",
    subscription_id: str = "",
    assignee=None,
    department: str = "technical",
    priority: str = "medium",
) -> Ticket:
    from business.sla import apply_sla_to_ticket

    queue = get_company_queue(company)
    now = timezone.now()
    ui_priority = priority if priority in PRIORITY_UI_TO_HD else "medium"
    ticket = Ticket(
        title=subject[:200],
        queue=queue,
        created=now,
        modified=now,
        submitter_email=(oem_to_email or "").strip() or None,
        status=Ticket.OPEN_STATUS,
        description=description,
        priority=PRIORITY_UI_TO_HD.get(ui_priority, 3),
        assigned_to=assignee,
    )
    ticket.save()
    ensure_meta(
        ticket,
        created_by=user,
        department=department,
        is_internal=True,
        is_oem=True,
        oem_company_name=oem_company_name,
        oem_to_email=oem_to_email,
        oem_cc_emails=oem_cc_emails,
        subscription_id=subscription_id,
    )
    FollowUp.objects.create(
        ticket=ticket,
        date=now,
        title="OEM ticket opened",
        comment="",
        public=False,
        user=user,
        new_status=Ticket.OPEN_STATUS,
    )
    apply_sla_to_ticket(
        company,
        ticket,
        priority=ui_priority,
        priority_source="manual",
    )
    return ticket


def set_ticket_priority(
    company: CompanyProfile,
    ticket: Ticket,
    priority: str,
    *,
    user=None,
    priority_source: str = "manual",
) -> bool:
    """Update priority and recalculate SLA dues from ticket.created. Returns True if changed."""
    from business.sla import apply_sla_to_ticket

    ui_priority = priority if priority in PRIORITY_UI_TO_HD else None
    if ui_priority is None:
        return False
    new_hd = PRIORITY_UI_TO_HD[ui_priority]
    if ticket.priority == new_hd:
        apply_sla_to_ticket(
            company, ticket, priority=ui_priority, priority_source=priority_source
        )
        return False
    ticket.priority = new_hd
    ticket.modified = timezone.now()
    ticket.save(update_fields=["priority", "modified"])
    if user is not None:
        FollowUp.objects.create(
            ticket=ticket,
            date=timezone.now(),
            title=f"Priority set to {PRIORITY_LABELS.get(ui_priority, ui_priority)}",
            comment="",
            public=False,
            user=user,
        )
    apply_sla_to_ticket(
        company,
        ticket,
        priority=ui_priority,
        priority_source=priority_source,
    )
    return True


def set_ticket_status(ticket: Ticket, ui_status: str, user=None) -> bool:
    """Update status via helpdesk update_ticket. Returns True if status changed."""
    hd_status = STATUS_UI_TO_HD.get(ui_status)
    if hd_status is None or ticket.status == hd_status:
        return False
    update_ticket(
        user=user,
        ticket=ticket,
        comment="",
        public=False,
        new_status=hd_status,
    )
    meta = ensure_meta(ticket)
    if ui_status in ("resolved", "closed"):
        if not meta.completed_at:
            meta.completed_at = timezone.now()
            meta.save(update_fields=["completed_at", "updated_at"])
    elif ui_status in ("open", "in_progress"):
        if meta.completed_at:
            meta.completed_at = None
            meta.save(update_fields=["completed_at", "updated_at"])
    return True


def set_ticket_assignee(ticket: Ticket, assignee, user=None, comment: str = "") -> bool:
    """
    Assign/unassign a ticket. Returns True if the assignee changed.

    Uses a private follow-up so django-helpdesk does not send its default
    assign mail; Ticket360 sends a branded notification separately.
    """
    owner_id = assignee.pk if assignee else 0
    if ticket.assigned_to_id == (assignee.pk if assignee else None):
        return False
    update_ticket(
        user=user,
        ticket=ticket,
        comment=comment,
        public=False,
        owner=owner_id,
    )
    return True


def set_ticket_remarks(ticket: Ticket, remarks: str) -> None:
    meta = ensure_meta(ticket)
    meta.remarks = (remarks or "")[:255]
    meta.save(update_fields=["remarks", "updated_at"])


def add_comment(
    *,
    ticket: Ticket,
    user,
    body: str,
    internal_note: bool = False,
) -> FollowUp:
    from business.sla import mark_first_response

    public = not internal_note
    followup = update_ticket(
        user=user,
        ticket=ticket,
        comment=body,
        public=public,
        new_status=ticket.status,
    )
    # Public staff replies count as first response for response SLA.
    if public and user is not None:
        mark_first_response(ticket)
    return followup


def _file_payloads(followup) -> list[tuple[str, bytes, str]]:
    payloads: list[tuple[str, bytes, str]] = []
    for attachment in followup.followupattachment_set.all():
        if not attachment.file:
            continue
        try:
            with attachment.file.open("rb") as handle:
                content = handle.read()
        except (OSError, ValueError):
            continue
        if not content:
            continue
        name = attachment.filename or "attachment"
        mime = (attachment.mime_type or "application/octet-stream").split(";")[0].strip()
        payloads.append((name, content, mime or "application/octet-stream"))
    return payloads


def followups_as_comments(
    ticket: Ticket, *, load_files: bool = False
) -> list[CommentView]:
    from business.email_body_clean import clean_inbound_email_body

    rows = (
        FollowUp.objects.filter(ticket=ticket)
        .select_related("user")
        .prefetch_related("followupattachment_set")
        .order_by("date", "id")
    )
    comments: list[CommentView] = []
    for fu in rows:
        email = ""
        if fu.user_id and fu.user:
            email = fu.user.email
        else:
            match = re.search(r"(?:from|by)\s+(\S+@\S+)", fu.title or "", re.I)
            if match:
                email = match.group(1).rstrip(".,;")
            elif ticket.submitter_email:
                email = ticket.submitter_email
        body = clean_inbound_email_body(fu.comment or "", empty_fallback="")
        attachments = _mail_files(ticket.pk, fu)
        if not body and not attachments:
            continue
        comments.append(
            CommentView(
                user_email=email or "system",
                body=body,
                internal_note=not fu.public,
                created_at=fu.date,
                attachments=attachments,
                outbound=(fu.title or "").lower().startswith("e-mail sent"),
                files=_file_payloads(fu) if load_files and fu.public else [],
            )
        )
    return comments
