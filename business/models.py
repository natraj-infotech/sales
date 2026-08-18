import uuid
from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone

from accounts.models import CompanyProfile


class Vendor(models.Model):
    company = models.ForeignKey(
        CompanyProfile, on_delete=models.CASCADE, related_name="vendors"
    )
    name = models.CharField(max_length=255)
    gst_number = models.CharField("GST number", max_length=32, blank=True)
    email = models.EmailField("email 1", blank=True)
    email_2 = models.EmailField("email 2", blank=True)
    email_3 = models.EmailField("email 3", blank=True)
    phone = models.CharField("phone 1", max_length=64, blank=True)
    phone_2 = models.CharField("phone 2", max_length=64, blank=True)
    phone_3 = models.CharField("phone 3", max_length=64, blank=True)
    website = models.URLField(blank=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Client(models.Model):
    company = models.ForeignKey(
        CompanyProfile, on_delete=models.CASCADE, related_name="clients"
    )
    name = models.CharField(max_length=255)
    gst_number = models.CharField("GST number", max_length=32, blank=True)
    email = models.EmailField("Email 1", blank=True)
    email_2 = models.EmailField("Email 2", blank=True)
    email_3 = models.EmailField("Email 3", blank=True)
    phone = models.CharField("Contact 1", max_length=64, blank=True)
    phone_2 = models.CharField("Contact 2", max_length=64, blank=True)
    phone_3 = models.CharField("Contact 3", max_length=64, blank=True)
    notes = models.TextField(blank=True)
    renewal_emails_enabled = models.BooleanField(
        "renewal emails",
        default=True,
        help_text="When off, this client is not sent expiry or renewal reminder emails.",
    )
    use_custom_reminder = models.BooleanField(
        "use custom reminder",
        default=False,
        help_text="When on, this client uses its own reminder schedule and/or email wording.",
    )
    reminder_schedule = models.JSONField(
        blank=True,
        null=True,
        help_text="Custom reminder phases for this client. Blank fields inherit company default.",
    )
    reminder_email_subject = models.CharField(max_length=255, blank=True)
    reminder_email_initial_paragraph = models.TextField(blank=True)
    reminder_email_end_paragraph = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    def contact_emails(self) -> list[str]:
        emails: list[str] = []
        seen: set[str] = set()
        for raw in (self.email, self.email_2, self.email_3):
            addr = (raw or "").strip()
            key = addr.lower()
            if addr and key not in seen:
                seen.add(key)
                emails.append(addr)
        return emails


class Product(models.Model):
    company = models.ForeignKey(
        CompanyProfile, on_delete=models.CASCADE, related_name="products"
    )
    vendor = models.ForeignKey(
        Vendor,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="products",
    )
    name = models.CharField(max_length=255)
    category = models.CharField(max_length=128, blank=True)
    asset_type = models.CharField("type", max_length=128, blank=True)
    company_name = models.CharField("company", max_length=255, blank=True)
    tax = models.CharField(max_length=64, blank=True)
    depreciation = models.CharField(max_length=128, blank=True)
    hsn_code = models.CharField("HSN code", max_length=32, blank=True)
    company_phone = models.CharField("company number", max_length=64, blank=True)
    company_email = models.EmailField("company email", blank=True)
    components = models.TextField(blank=True)
    notes = models.TextField(blank=True)
    sku = models.CharField(max_length=64, blank=True)
    description = models.TextField(blank=True)
    unit_price = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class ClientSubscription(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        PAUSED = "paused", "Paused"
        ENDED = "ended", "Ended"

    class DealStatus(models.TextChoices):
        WIN = "win", "Win"
        LOST = "lost", "Lost"
        CANCEL = "cancel", "Cancel"

    company = models.ForeignKey(
        CompanyProfile, on_delete=models.CASCADE, related_name="subscriptions"
    )
    client = models.ForeignKey(
        Client, on_delete=models.CASCADE, related_name="subscriptions"
    )
    product = models.ForeignKey(
        Product, on_delete=models.CASCADE, related_name="subscriptions"
    )
    vendor = models.ForeignKey(
        Vendor, on_delete=models.CASCADE, null=True, blank=True
    )
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.ACTIVE
    )
    deal_status = models.CharField(
        max_length=16,
        choices=DealStatus.choices,
        blank=True,
        default="",
    )
    start_date = models.DateField()
    end_date = models.DateField(null=True, blank=True)
    quantity = models.PositiveIntegerField(default=1)
    billing_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    invoice_number = models.CharField(max_length=128, blank=True)
    serial_number = models.CharField(max_length=128, blank=True)
    subscription_code = models.CharField("subscription id", max_length=64, blank=True)
    notes = models.TextField(blank=True)
    expiry_reminder_stopped_for_end_date = models.DateField(
        null=True,
        blank=True,
        help_text="When set to the current end date, client reminder emails stop "
        "until the subscription is renewed with a new end date.",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-start_date", "-id"]

    def __str__(self):
        return f"{self.client} — {self.product}"

    @property
    def subscription_display_id(self):
        return f"{self.pk:03d}"

    @property
    def subscription_list_id(self):
        code = (self.subscription_code or "").strip()
        return code if code else self.subscription_display_id

    @property
    def days_until_expiry(self):
        if not self.end_date:
            return None
        return (self.end_date - timezone.localdate()).days

    def renewal_dates(self, today=None):
        """Next start/end after renew: same term length, starting day after current end."""
        today = today or timezone.localdate()
        if self.end_date:
            term_days = (self.end_date - self.start_date).days
            if term_days < 1:
                term_days = 365
            new_start = self.end_date + timedelta(days=1)
            new_end = new_start + timedelta(days=term_days)
        else:
            new_start = today
            new_end = today + timedelta(days=365)
        return new_start, new_end


class SubscriptionExpiryNotice(models.Model):
    """Tracks client reminder emails already sent for a subscription threshold."""

    subscription = models.ForeignKey(
        ClientSubscription,
        on_delete=models.CASCADE,
        related_name="expiry_notices",
    )
    days_before = models.PositiveSmallIntegerField()
    sent_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["subscription", "days_before"],
                name="unique_expiry_notice_per_subscription_day",
            ),
        ]
        ordering = ["-sent_at"]

    def __str__(self):
        return f"{self.subscription_id} @ {self.days_before} days"


class SubscriptionRenewalInvite(models.Model):
    """One-time link emailed to clients to confirm subscription renewals."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(
        CompanyProfile,
        on_delete=models.CASCADE,
        related_name="renewal_invites",
    )
    client_email = models.EmailField()
    email_subject = models.CharField(max_length=255, blank=True)
    internet_message_id = models.CharField(max_length=998, blank=True)
    email_sent_at = models.DateTimeField(null=True, blank=True)
    read_at = models.DateTimeField(null=True, blank=True)
    read_source = models.CharField(max_length=32, blank=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    submitted_by_email = models.EmailField(blank=True)
    zoho_quote_id = models.CharField(max_length=64, blank=True)
    zoho_quote_number = models.CharField(max_length=128, blank=True)
    zoho_quote_url = models.URLField(blank=True)
    quotation_sent_at = models.DateTimeField(null=True, blank=True)
    quotation_error = models.TextField(blank=True)
    deal_status = models.CharField(
        max_length=16,
        choices=ClientSubscription.DealStatus.choices,
        blank=True,
        default="",
    )
    deal_status_set_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-email_sent_at", "-created_at"]

    def __str__(self):
        return f"Renewal invite {self.id} → {self.client_email}"

    @property
    def is_submitted(self) -> bool:
        return self.submitted_at is not None

    @property
    def response_received(self) -> bool:
        return self.is_submitted

    def allowed_submitter_emails(self) -> list[str]:
        emails: list[str] = []
        seen: set[str] = set()

        def add(raw: str) -> None:
            addr = (raw or "").strip()
            key = addr.lower()
            if addr and key not in seen:
                seen.add(key)
                emails.append(addr)

        add(self.client_email)
        for line in self.lines.all():
            client = getattr(getattr(line, "subscription", None), "client", None)
            if client is None:
                continue
            for addr in client.contact_emails():
                add(addr)
        return emails

    def is_allowed_submitter(self, email: str) -> bool:
        key = (email or "").strip().lower()
        if not key:
            return False
        return key in {addr.lower() for addr in self.allowed_submitter_emails()}

    def deal_table_state(self) -> dict:
        """Pending flag and unique deal badges from linked subscriptions."""
        pending = False
        badges: list[str] = []
        seen: set[str] = set()
        lines = list(self.lines.all())
        if not lines:
            return {"pending": True, "badges": []}
        for line in lines:
            status = getattr(getattr(line, "subscription", None), "deal_status", "") or ""
            if status:
                if status not in seen:
                    seen.add(status)
                    badges.append(status)
            else:
                pending = True
        return {"pending": pending, "badges": badges}

    def deal_line_items(self, next_url: str) -> list[dict]:
        from urllib.parse import urlencode

        from django.urls import reverse

        items = []
        seen: set[int] = set()
        lines = self.lines.select_related(
            "subscription",
            "subscription__product",
            "subscription__client",
        ).order_by("id")
        for line in lines:
            sub = line.subscription
            if not sub or sub.pk in seen:
                continue
            seen.add(sub.pk)
            status = sub.deal_status or ""
            company_name = line.company_name or (
                sub.client.name if sub.client_id else ""
            )
            product_name = line.product_name or sub.product.name
            renew_url = ""
            if not status:
                renew_path = reverse("subscription_renew", kwargs={"pk": sub.pk})
                renew_url = f"{renew_path}?{urlencode({'next': next_url})}"
            items.append(
                {
                    "subscription_id": sub.pk,
                    "company_name": company_name,
                    "product_name": product_name,
                    "deal_status": status,
                    "deal_status_label": sub.get_deal_status_display() if status else "",
                    "renew_url": renew_url,
                }
            )
        return items

    def apply_line_deal_status(self, subscription_id: int, status: str) -> None:
        """Set Win/Lost/Cancel on one linked subscription only."""
        line = (
            self.lines.select_related("subscription")
            .filter(subscription_id=subscription_id)
            .first()
        )
        if line is None or line.subscription is None:
            raise ValueError("Subscription is not part of this renewal email.")
        sub = line.subscription
        if sub.deal_status:
            raise ValueError("Deal status is already set.")
        sub.deal_status = status
        sub.save(update_fields=["deal_status"])


class SubscriptionRenewalInviteLine(models.Model):
    invite = models.ForeignKey(
        SubscriptionRenewalInvite,
        on_delete=models.CASCADE,
        related_name="lines",
    )
    subscription = models.ForeignKey(
        ClientSubscription,
        on_delete=models.CASCADE,
        related_name="renewal_invite_lines",
    )
    company_name = models.CharField(max_length=255)
    product_name = models.CharField(max_length=255)
    current_quantity = models.PositiveIntegerField()
    renewal_count = models.PositiveIntegerField(null=True, blank=True)

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return f"{self.company_name} — {self.product_name}"


class SubscriptionExpiryDispatch(models.Model):
    """Tracks each expiry reminder dispatch so daily quotas can be enforced."""

    class Source(models.TextChoices):
        AUTOMATIC = "auto", "Automatic"
        MANUAL = "manual", "Manual"

    subscription = models.ForeignKey(
        ClientSubscription,
        on_delete=models.CASCADE,
        related_name="expiry_dispatches",
    )
    threshold_window = models.IntegerField()
    sent_on = models.DateField()
    send_number = models.PositiveSmallIntegerField(default=1)
    # Scheduled slots are counted per source, so a manual send never uses up an
    # automatic slot for the day (and vice versa).
    source = models.CharField(
        max_length=10,
        choices=Source.choices,
        default=Source.AUTOMATIC,
    )
    sent_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-sent_at"]
        constraints = [
            models.UniqueConstraint(
                fields=[
                    "subscription",
                    "threshold_window",
                    "sent_on",
                    "send_number",
                    "source",
                ],
                name="unique_expiry_dispatch_per_send_slot",
            ),
        ]

    def __str__(self):
        return (
            f"{self.subscription_id} @ {self.threshold_window} days "
            f"on {self.sent_on} #{self.send_number} ({self.source})"
        )


class CaseLockInbox(models.Model):
    """
    Inbound mailbox tied to a product; unread mail triggers a Case lock form link
    emailed to the sender.
    """

    company = models.ForeignKey(
        CompanyProfile,
        on_delete=models.CASCADE,
        related_name="caselock_inboxes",
    )
    product = models.ForeignKey(
        Product,
        on_delete=models.CASCADE,
        related_name="caselock_inboxes",
    )
    mailbox_email = models.EmailField(
        help_text="IMAP mailbox address to poll for this product.",
    )
    mailbox_app_password = models.CharField(
        "mailbox app password",
        max_length=256,
        blank=True,
        help_text="IMAP app password (e.g. Gmail). Leave blank for Microsoft 365 if using Graph API credentials in settings.",
    )
    is_active = models.BooleanField(default=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["product__name"]
        constraints = [
            models.UniqueConstraint(
                fields=["company", "product"],
                name="unique_caselock_inbox_per_company_product",
            ),
        ]

    def __str__(self):
        return f"{self.mailbox_email} → {self.product.name}"


class CaseLockInvite(models.Model):
    """One inbound message → one secure link to the case lock form."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    inbox = models.ForeignKey(
        CaseLockInbox,
        on_delete=models.CASCADE,
        related_name="invites",
    )
    source_message_id = models.CharField(max_length=998)
    sender_email = models.CharField(max_length=254)
    mail_subject = models.CharField(max_length=255, blank=True)
    email_sent_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["inbox", "source_message_id"],
                name="unique_caselock_invite_message_per_inbox",
            ),
        ]

    def __str__(self):
        return f"Invite {self.id} from {self.sender_email}"


class CaseLockSubmission(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        APPROVED = "approved", "Approved"
        DECLINED = "declined", "Declined"

    invite = models.OneToOneField(
        CaseLockInvite,
        on_delete=models.CASCADE,
        related_name="submission",
    )
    vendor_name = models.CharField(max_length=255)
    vendor_gst = models.CharField("vendor GST", max_length=32, blank=True)
    client_name = models.CharField(max_length=255)
    client_contact_info = models.CharField("client contact info", max_length=255, blank=True)
    client_phone = models.CharField("client phone", max_length=64, blank=True)
    client_email = models.EmailField("client email 1", blank=True)
    client_email_2 = models.EmailField("client email 2", blank=True)
    client_gst = models.CharField("client GST", max_length=32, blank=True)
    product_name = models.CharField(max_length=255)
    product_quantity = models.PositiveIntegerField("product quantity required", default=1)
    timestamp = models.DateTimeField(
        auto_now_add=True,
        help_text="When the form was submitted.",
    )
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
    )

    class Meta:
        ordering = ["-timestamp"]

    def __str__(self):
        return f"Case lock {self.vendor_name} / {self.client_name}"


class SupportTicket(models.Model):
    class Department(models.TextChoices):
        GENERAL = "general", "General"
        BILLING = "billing", "Billing"
        TECHNICAL = "technical", "Technical"
        SALES = "sales", "Sales"

    class Priority(models.TextChoices):
        LOW = "low", "Low"
        MEDIUM = "medium", "Medium"
        HIGH = "high", "High"
        URGENT = "urgent", "Urgent"

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        IN_PROGRESS = "in_progress", "In progress"
        RESOLVED = "resolved", "Resolved"
        CLOSED = "closed", "Closed"

    company = models.ForeignKey(
        CompanyProfile,
        on_delete=models.CASCADE,
        related_name="support_tickets",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="support_tickets",
    )
    subject = models.CharField(max_length=255)
    description = models.TextField()
    department = models.CharField(
        max_length=20, choices=Department.choices, default=Department.GENERAL
    )
    priority = models.CharField(
        max_length=10, choices=Priority.choices, default=Priority.MEDIUM
    )
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.OPEN
    )
    assignee = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assigned_support_tickets",
    )
    due_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    remarks = models.CharField(max_length=255, blank=True)
    source_message_id = models.CharField(
        max_length=998,
        blank=True,
        null=True,
        unique=True,
        db_index=True,
        help_text="RFC5322 Message-ID when created from email import.",
    )
    source_sender = models.CharField(
        max_length=254,
        blank=True,
        help_text="From address on the inbound email.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return self.subject

    @property
    def reference(self):
        return f"TK-{self.pk:05d}"


class SupportTicketComment(models.Model):
    ticket = models.ForeignKey(
        SupportTicket, on_delete=models.CASCADE, related_name="comments"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="ticket_comments"
    )
    body = models.TextField()
    internal_note = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]

    def __str__(self):
        return f"{self.ticket.reference} comment by {self.user}"


class SupportTicketMeta(models.Model):
    """CRM extras for django-helpdesk Ticket rows (department, remarks, tenant flags)."""

    class PrioritySource(models.TextChoices):
        MANUAL = "manual", "Manual"
        RULE = "rule", "Keyword rule"
        DEFAULT = "default", "Default"

    ticket = models.OneToOneField(
        "helpdesk.Ticket",
        on_delete=models.CASCADE,
        related_name="crm_meta",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="helpdesk_tickets_created",
    )
    department = models.CharField(
        max_length=20,
        choices=SupportTicket.Department.choices,
        default=SupportTicket.Department.GENERAL,
    )
    remarks = models.CharField(max_length=255, blank=True)
    is_internal = models.BooleanField(
        default=True,
        help_text="True for tickets created in Ticket360 UI; False for email/IMAP.",
    )
    completed_at = models.DateTimeField(null=True, blank=True)
    first_response_due_at = models.DateTimeField(null=True, blank=True)
    resolve_due_at = models.DateTimeField(null=True, blank=True)
    first_responded_at = models.DateTimeField(null=True, blank=True)
    priority_source = models.CharField(
        max_length=20,
        choices=PrioritySource.choices,
        default=PrioritySource.DEFAULT,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "support ticket meta"
        verbose_name_plural = "support ticket meta"

    def __str__(self):
        return f"Meta for ticket {self.ticket_id}"


class SupportSLASettings(models.Model):
    """Per-company SLA mode, hours (24×7), and email auto-priority toggle."""

    class SLAMode(models.TextChoices):
        RESPONSE = "response", "First response only"
        RESOLVE = "resolve", "Resolve only"
        BOTH = "both", "First response + resolve"

    company = models.OneToOneField(
        CompanyProfile,
        on_delete=models.CASCADE,
        related_name="support_sla_settings",
    )
    sla_mode = models.CharField(
        max_length=20,
        choices=SLAMode.choices,
        default=SLAMode.RESOLVE,
    )
    email_auto_priority = models.BooleanField(
        default=False,
        help_text="When on, inbound email tickets get priority from subject keywords.",
    )
    # Resolve hours (24×7 calendar hours)
    urgent_resolve_hours = models.PositiveIntegerField(default=4)
    high_resolve_hours = models.PositiveIntegerField(default=24)
    medium_resolve_hours = models.PositiveIntegerField(default=72)
    low_resolve_hours = models.PositiveIntegerField(default=168)
    # First-response hours
    urgent_response_hours = models.PositiveIntegerField(default=1)
    high_response_hours = models.PositiveIntegerField(default=2)
    medium_response_hours = models.PositiveIntegerField(default=8)
    low_response_hours = models.PositiveIntegerField(default=24)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "support SLA settings"
        verbose_name_plural = "support SLA settings"

    def __str__(self):
        return f"SLA settings for {self.company}"

    def resolve_hours_for(self, priority: str) -> int:
        mapping = {
            "urgent": self.urgent_resolve_hours,
            "high": self.high_resolve_hours,
            "medium": self.medium_resolve_hours,
            "low": self.low_resolve_hours,
        }
        return int(mapping.get(priority, self.medium_resolve_hours) or 0)

    def response_hours_for(self, priority: str) -> int:
        mapping = {
            "urgent": self.urgent_response_hours,
            "high": self.high_response_hours,
            "medium": self.medium_response_hours,
            "low": self.low_response_hours,
        }
        return int(mapping.get(priority, self.medium_response_hours) or 0)

    def tracks_response(self) -> bool:
        return self.sla_mode in (self.SLAMode.RESPONSE, self.SLAMode.BOTH)

    def tracks_resolve(self) -> bool:
        return self.sla_mode in (self.SLAMode.RESOLVE, self.SLAMode.BOTH)


class TicketPriorityRule(models.Model):
    """Customisable subject keywords → priority for a company."""

    company = models.ForeignKey(
        CompanyProfile,
        on_delete=models.CASCADE,
        related_name="ticket_priority_rules",
    )
    priority = models.CharField(
        max_length=10,
        choices=SupportTicket.Priority.choices,
    )
    keywords = models.CharField(
        max_length=500,
        help_text="Comma-separated keywords matched against the ticket subject.",
    )
    sort_order = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["sort_order", "id"]
        verbose_name = "ticket priority rule"
        verbose_name_plural = "ticket priority rules"

    def __str__(self):
        return f"{self.company_id}:{self.priority} ({self.keywords[:40]})"

    def keyword_list(self) -> list[str]:
        return [
            part.strip().lower()
            for part in (self.keywords or "").split(",")
            if part.strip()
        ]
