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
    email = models.EmailField(blank=True)
    phone = models.CharField("Contact", max_length=64, blank=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


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

    company = models.ForeignKey(
        CompanyProfile, on_delete=models.CASCADE, related_name="subscriptions"
    )
    client = models.ForeignKey(
        Client, on_delete=models.CASCADE, related_name="subscriptions"
    )
    product = models.ForeignKey(
        Product, on_delete=models.PROTECT, related_name="subscriptions"
    )
    vendor = models.ForeignKey(
        Vendor, on_delete=models.SET_NULL, null=True, blank=True
    )
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.ACTIVE
    )
    start_date = models.DateField()
    end_date = models.DateField(null=True, blank=True)
    quantity = models.PositiveIntegerField(default=1)
    billing_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    invoice_number = models.CharField(max_length=128, blank=True)
    serial_number = models.CharField(max_length=128, blank=True)
    subscription_code = models.CharField("subscription id", max_length=64, blank=True)
    notes = models.TextField(blank=True)
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
