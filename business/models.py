import re
import uuid
from datetime import time, timedelta

from django.conf import settings
from django.db import models
from django.db.models.functions import Lower, Trim
from django.utils import timezone

from accounts.models import CompanyProfile


def subscription_invoice_upload_to(instance, filename):
    company_id = getattr(instance, "company_id", None) or "unknown"
    return f"subscription_invoices/{company_id}/{filename}"


def subscription_draft_invoice_upload_to(instance, filename):
    company_id = getattr(instance, "company_id", None) or "unknown"
    return f"subscription_invoice_drafts/{company_id}/{filename}"


def _add_years(day, years):
    years = int(years)
    try:
        return day.replace(year=day.year + years)
    except ValueError:
        return day.replace(year=day.year + years, day=28)


def _add_one_year(day):
    return _add_years(day, 1)


LOST_REMINDER_LEAD_DAYS = 15


def lost_reminder_resume_on(end_date):
    """Date client reminders start again after a Lost deal: 15 days before next year's end."""
    if not end_date:
        return None
    return _add_one_year(end_date) - timedelta(days=LOST_REMINDER_LEAD_DAYS)


def _field_file_meta(field_file) -> tuple[str, str]:
    if not field_file:
        return "", ""
    raw = field_file.name or ""
    name = raw.rsplit("/", 1)[-1]
    try:
        url = field_file.url
    except ValueError:
        url = ""
    return name, url


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
    catalog_products = models.ManyToManyField(
        "Product",
        blank=True,
        related_name="offering_vendors",
        help_text="Products this vendor can quote in procurement.",
    )
    catalog_companies = models.JSONField(
        default=list,
        blank=True,
        help_text="Product companies this vendor can quote in procurement.",
    )

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


class ReminderTemplate(models.Model):
    company = models.ForeignKey(
        CompanyProfile,
        on_delete=models.CASCADE,
        related_name="reminder_templates",
    )
    name = models.CharField(max_length=120)
    schedule = models.JSONField(
        default=list,
        blank=True,
        help_text="List of reminder phases (from/to days, send mode, times).",
    )
    is_default = models.BooleanField(
        default=False,
        help_text="Clients with no template assigned use the company default template.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-is_default", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["company"],
                condition=models.Q(is_default=True),
                name="unique_default_reminder_template_per_company",
            ),
        ]

    def __str__(self):
        label = self.name
        if self.is_default:
            label = f"{label} (Default)"
        return label


PAN_PATTERN = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")
PAN_FORMAT_ERROR = "Enter a valid PAN (e.g. ABCDE1234F)."


def normalize_pan(raw) -> str:
    return "".join(str(raw or "").split()).upper()


class Client(models.Model):
    company = models.ForeignKey(
        CompanyProfile, on_delete=models.CASCADE, related_name="clients"
    )
    name = models.CharField(max_length=255)
    gst_number = models.CharField("GST number", max_length=32, blank=True)
    pan_number = models.CharField("PAN number", max_length=10, blank=True)
    tenant_id = models.CharField("Tenant ID", max_length=255, blank=True)
    address = models.TextField(blank=True)
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
    reminder_template = models.ForeignKey(
        ReminderTemplate,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="clients",
        help_text="When blank, the company default reminder template is used.",
    )
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
    vendors = models.ManyToManyField(
        Vendor,
        blank=True,
        related_name="supplied_products",
        help_text="All vendors that sell this product.",
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
    discounted_price = models.DecimalField(max_digits=12, decimal_places=2, default=0)
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
    deal_status_set_at = models.DateTimeField(null=True, blank=True)
    lost_remark = models.TextField(blank=True)
    lost_reminder_resume_on = models.DateField(
        null=True,
        blank=True,
        help_text="After Lost, client renewal reminders start again on this date.",
    )
    win_form_completed = models.BooleanField(default=False)
    renewal_draft_start_date = models.DateField(null=True, blank=True)
    renewal_draft_end_date = models.DateField(null=True, blank=True)
    renewal_draft_quantity = models.PositiveIntegerField(null=True, blank=True)
    renewal_draft_billing_amount = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True
    )
    renewal_draft_vendor = models.ForeignKey(
        Vendor,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="renewal_draft_subscriptions",
    )
    renewal_draft_product = models.ForeignKey(
        "Product",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="renewal_draft_subscriptions",
    )
    renewal_draft_invoice_number = models.CharField(max_length=128, blank=True)
    renewal_draft_invoice_pdf = models.FileField(
        "new invoice PDF",
        upload_to=subscription_draft_invoice_upload_to,
        blank=True,
        null=True,
    )
    start_date = models.DateField()
    end_date = models.DateField(null=True, blank=True)
    quantity = models.PositiveIntegerField(default=1)
    billing_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    invoice_number = models.CharField(max_length=128, blank=True)
    invoice_pdf = models.FileField(
        "invoice PDF",
        upload_to=subscription_invoice_upload_to,
        blank=True,
        null=True,
    )
    serial_number = models.CharField(max_length=128, blank=True)
    subscription_code = models.CharField("subscription id", max_length=64, blank=True)
    zoho_invoice_id = models.CharField(max_length=64, blank=True)
    zoho_line_item_id = models.CharField(max_length=64, blank=True)
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
        constraints = [
            models.UniqueConstraint(
                models.F("company"),
                Lower(Trim("subscription_code")),
                condition=models.Q(subscription_code__regex=r"\S"),
                name="unique_company_normalized_subscription_code",
            ),
            models.UniqueConstraint(
                fields=["company", "zoho_invoice_id", "zoho_line_item_id"],
                condition=(
                    ~models.Q(zoho_invoice_id="") & ~models.Q(zoho_line_item_id="")
                ),
                name="unique_company_zoho_invoice_line",
            ),
        ]

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
        """Next period starts the day after the current end and runs for one year."""
        today = today or timezone.localdate()
        if self.end_date:
            new_start = self.end_date + timedelta(days=1)
        else:
            new_start = today
        return new_start, _add_one_year(new_start)

    def reminder_target_end(self, today=None):
        """End date used for client reminders. None means do not send."""
        if not self.end_date:
            return None
        today = today or timezone.localdate()
        if self.deal_status == self.DealStatus.LOST:
            resume = self.lost_reminder_resume_on
            if resume is None or today < resume:
                return None
            return _add_one_year(self.end_date)
        if self.deal_status in (self.DealStatus.WIN, self.DealStatus.CANCEL):
            return None
        return self.end_date

    def client_renewal_quantity(self):
        """Quantity the client submitted on a renewal email, if any."""
        line = (
            self.renewal_invite_lines.filter(
                renewal_count__isnull=False,
                invite__submitted_at__isnull=False,
            )
            .order_by("-invite__submitted_at", "-id")
            .first()
        )
        if line is None or line.renewal_count is None:
            return None
        return int(line.renewal_count)

    @property
    def win_form_empty(self) -> bool:
        return self.deal_status == self.DealStatus.WIN and not self.win_form_completed

    def renewal_form_initial(self) -> dict:
        """Values for the Win draft form and the Renew page new-detail fields."""
        suggested_start, suggested_end = self.renewal_dates()
        if self.win_form_completed:
            start = self.renewal_draft_start_date or suggested_start
            end = self.renewal_draft_end_date
            quantity = (
                self.renewal_draft_quantity
                if self.renewal_draft_quantity is not None
                else self.quantity
            )
            amount = (
                self.renewal_draft_billing_amount
                if self.renewal_draft_billing_amount is not None
                else self.billing_amount
            )
            vendor_id = self.renewal_draft_vendor_id
            product_id = self.renewal_draft_product_id
            invoice_number = self.renewal_draft_invoice_number
            pdf_name, pdf_url = _field_file_meta(
                self.renewal_draft_invoice_pdf or self.invoice_pdf
            )
        else:
            start, end = suggested_start, suggested_end
            submitted_qty = self.client_renewal_quantity()
            quantity = submitted_qty if submitted_qty is not None else self.quantity
            amount = self.billing_amount
            vendor_id = self.vendor_id
            product_id = None
            invoice_number = self.invoice_number
            pdf_name, pdf_url = _field_file_meta(self.invoice_pdf)
        draft_product = None
        if product_id:
            draft_product = getattr(self, "renewal_draft_product", None)
        return {
            "start_date": start,
            "end_date": end,
            "quantity": quantity,
            "billing_amount": amount,
            "vendor": str(vendor_id) if vendor_id else "",
            "product": str(product_id) if product_id else "",
            "product_name": draft_product.name if draft_product else "",
            "renew_other_plan": bool(product_id),
            "invoice_number": invoice_number or "",
            "invoice_pdf_name": pdf_name,
            "invoice_pdf_url": pdf_url,
        }

    def _set_renewal_draft(
        self,
        *,
        start_date,
        end_date,
        quantity,
        billing_amount,
        vendor,
        invoice_number="",
        invoice_pdf=None,
        product=None,
    ) -> None:
        self.deal_status = self.DealStatus.WIN
        if not self.deal_status_set_at:
            self.deal_status_set_at = timezone.now()
        self.win_form_completed = True
        self.renewal_draft_start_date = start_date
        self.renewal_draft_end_date = end_date
        self.renewal_draft_quantity = quantity
        self.renewal_draft_billing_amount = billing_amount
        self.renewal_draft_vendor = vendor
        self.renewal_draft_product = product
        self.renewal_draft_invoice_number = invoice_number or ""
        if invoice_pdf:
            self.renewal_draft_invoice_pdf = invoice_pdf

    def save_renewal_draft(
        self,
        *,
        start_date,
        end_date,
        quantity,
        billing_amount,
        vendor,
        invoice_number="",
        invoice_pdf=None,
        product=None,
    ) -> None:
        """Store intended renewal terms. Does not change the live subscription."""
        self._set_renewal_draft(
            start_date=start_date,
            end_date=end_date,
            quantity=quantity,
            billing_amount=billing_amount,
            vendor=vendor,
            invoice_number=invoice_number,
            invoice_pdf=invoice_pdf,
            product=product,
        )
        update_fields = [
            "deal_status",
            "deal_status_set_at",
            "win_form_completed",
            "renewal_draft_start_date",
            "renewal_draft_end_date",
            "renewal_draft_quantity",
            "renewal_draft_billing_amount",
            "renewal_draft_vendor",
            "renewal_draft_product",
            "renewal_draft_invoice_number",
        ]
        if invoice_pdf:
            update_fields.append("renewal_draft_invoice_pdf")
        self.save(update_fields=update_fields)
        self.sync_product_renewal(product, applied=False)

    def apply_saved_renewal(
        self,
        *,
        start_date,
        end_date,
        quantity,
        billing_amount,
        vendor,
        invoice_number="",
        invoice_pdf=None,
        product=None,
    ) -> None:
        """Apply renewal to the live subscription and mark the Win form filled."""
        applied_product = product or self.renewal_draft_product
        old_product = self.product
        self.start_date = start_date
        self.end_date = end_date
        self.quantity = quantity
        self.billing_amount = billing_amount
        self.vendor = vendor
        if applied_product is not None:
            self.product = applied_product
        self.status = self.Status.ACTIVE
        self.invoice_number = invoice_number or ""
        applied_pdf = invoice_pdf or self.renewal_draft_invoice_pdf
        if applied_pdf:
            self.invoice_pdf = applied_pdf
        self._set_renewal_draft(
            start_date=start_date,
            end_date=end_date,
            quantity=quantity,
            billing_amount=billing_amount,
            vendor=vendor,
            invoice_number=self.invoice_number,
            invoice_pdf=applied_pdf,
            product=applied_product,
        )
        update_fields = [
            "start_date",
            "end_date",
            "quantity",
            "billing_amount",
            "vendor",
            "status",
            "invoice_number",
            "deal_status",
            "deal_status_set_at",
            "win_form_completed",
            "renewal_draft_start_date",
            "renewal_draft_end_date",
            "renewal_draft_quantity",
            "renewal_draft_billing_amount",
            "renewal_draft_vendor",
            "renewal_draft_product",
            "renewal_draft_invoice_number",
        ]
        if applied_product is not None:
            update_fields.append("product")
        if applied_pdf:
            update_fields.extend(["invoice_pdf", "renewal_draft_invoice_pdf"])
        self.save(update_fields=update_fields)
        self.sync_product_renewal(
            applied_product,
            applied=True,
            old_product=old_product,
        )

    def sync_product_renewal(
        self,
        new_product,
        *,
        applied=False,
        old_product=None,
    ):
        """Keep a before/after list when renewal switches the product."""
        old_product = old_product or self.product
        old_id = getattr(old_product, "pk", None)
        new_id = getattr(new_product, "pk", None)
        pending = self.product_renewals.filter(applied=False).order_by("-id")
        if not new_product or new_id == old_id:
            pending.delete()
            return None
        client_name = ""
        if getattr(self, "client_id", None):
            client_name = self.client.name
        now = timezone.now()
        row = pending.first()
        if row is None:
            row = SubscriptionProductRenewal(
                company=self.company,
                subscription=self,
                client_name=client_name,
                old_product=old_product,
                old_product_name=old_product.name if old_product else "",
            )
        row.client_name = client_name or row.client_name
        row.new_product = new_product
        row.new_product_name = new_product.name
        if applied:
            row.applied = True
            row.applied_at = now
        row.save()
        if applied:
            pending.exclude(pk=row.pk).delete()
        return row


class SubscriptionProductRenewal(models.Model):
    """Product swap made while renewing a subscription."""

    company = models.ForeignKey(
        CompanyProfile,
        on_delete=models.CASCADE,
        related_name="subscription_product_renewals",
    )
    subscription = models.ForeignKey(
        ClientSubscription,
        on_delete=models.CASCADE,
        related_name="product_renewals",
    )
    client_name = models.CharField(max_length=255)
    old_product = models.ForeignKey(
        Product,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    new_product = models.ForeignKey(
        Product,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    old_product_name = models.CharField(max_length=255)
    new_product_name = models.CharField(max_length=255)
    applied = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    applied_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"{self.client_name}: {self.old_product_name} → {self.new_product_name}"


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
    zoho_quote_stage = models.CharField(max_length=64, blank=True)
    zoho_quote_sent_at = models.DateTimeField(null=True, blank=True)
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
        """Pending flag, unique deal badges, and Win-form-empty from linked subscriptions."""
        pending = False
        badges: list[str] = []
        seen: set[str] = set()
        lines = list(self.lines.all())
        if not lines:
            return {
                "pending": True,
                "form_empty": False,
                "form_empty_count": 0,
                "badges": [],
            }
        form_empty_count = 0
        for line in lines:
            sub = getattr(line, "subscription", None)
            status = getattr(sub, "deal_status", "") or ""
            if status:
                if status not in seen:
                    seen.add(status)
                    badges.append(status)
                if getattr(sub, "win_form_empty", False):
                    form_empty_count += 1
            else:
                pending = True
        return {
            "pending": pending,
            "form_empty": form_empty_count > 0,
            "form_empty_count": form_empty_count,
            "badges": badges,
        }

    def _line_subscription(self, subscription_id: int):
        line = (
            self.lines.select_related("subscription")
            .filter(subscription_id=subscription_id)
            .first()
        )
        if line is None or line.subscription is None:
            raise ValueError("Subscription is not part of this renewal email.")
        return line.subscription

    def deal_line_items(self, next_url: str) -> list[dict]:
        items = []
        seen: set[int] = set()
        lines = self.lines.select_related(
            "subscription",
            "subscription__product",
            "subscription__client",
            "subscription__renewal_draft_product",
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
            initial = sub.renewal_form_initial()
            start = initial["start_date"]
            end = initial["end_date"]
            amount = initial["billing_amount"]
            items.append(
                {
                    "subscription_id": sub.pk,
                    "company_name": company_name,
                    "product_name": product_name,
                    "deal_status": status,
                    "deal_status_label": sub.get_deal_status_display() if status else "",
                    "win_form_completed": bool(sub.win_form_completed),
                    "form_empty": sub.win_form_empty,
                    "draft": {
                        "start_date": start.isoformat() if start else "",
                        "end_date": end.isoformat() if end else "",
                        "quantity": initial["quantity"],
                        "billing_amount": (
                            f"{amount:.2f}" if amount is not None else ""
                        ),
                        "vendor": initial["vendor"],
                        "product": initial.get("product") or "",
                        "product_name": initial.get("product_name") or "",
                        "renew_other_plan": bool(initial.get("renew_other_plan")),
                        "invoice_number": initial.get("invoice_number") or "",
                        "invoice_pdf_name": initial.get("invoice_pdf_name") or "",
                        "invoice_pdf_url": initial.get("invoice_pdf_url") or "",
                    },
                }
            )
        return items

    def apply_line_deal_status(
        self, subscription_id: int, status: str, remark: str = ""
    ) -> None:
        """Set Lost on one linked subscription. Win is saved only with the renewal form."""
        sub = self._line_subscription(subscription_id)
        if sub.deal_status and sub.deal_status != ClientSubscription.DealStatus.CANCEL:
            raise ValueError("Deal status is already set.")
        if status != ClientSubscription.DealStatus.LOST:
            raise ValueError("Choose Lost, or submit the Win renewal form.")
        sub.deal_status = status
        if not sub.deal_status_set_at:
            sub.deal_status_set_at = timezone.now()
        sub.lost_remark = (remark or "").strip()
        sub.lost_reminder_resume_on = lost_reminder_resume_on(sub.end_date)
        if not self.deal_status_set_at:
            self.deal_status_set_at = sub.deal_status_set_at
            self.save(update_fields=["deal_status_set_at"])
        sub.save(
            update_fields=[
                "deal_status",
                "deal_status_set_at",
                "lost_remark",
                "lost_reminder_resume_on",
            ]
        )

    def apply_line_renewal(
        self,
        subscription_id: int,
        *,
        start_date,
        quantity,
        billing_amount,
        vendor,
        invoice_number="",
        invoice_pdf=None,
        product=None,
        duration_years=1,
    ) -> None:
        """Apply the Win renewal. Duration is a number of years from the start date."""
        sub = self._line_subscription(subscription_id)
        if sub.deal_status == ClientSubscription.DealStatus.LOST:
            raise ValueError("Deal is already marked Lost.")
        try:
            years = int(duration_years)
        except (TypeError, ValueError):
            years = 0
        if years < 1 or years > 30:
            raise ValueError("Enter duration in years, from 1 to 30.")
        sub.apply_saved_renewal(
            start_date=start_date,
            end_date=_add_years(start_date, years),
            quantity=quantity,
            billing_amount=billing_amount,
            vendor=vendor,
            invoice_number=invoice_number,
            invoice_pdf=invoice_pdf,
            product=product,
        )
        if not self.deal_status_set_at:
            self.deal_status_set_at = sub.deal_status_set_at
            self.save(update_fields=["deal_status_set_at"])

    def save_line_renewal_draft(
        self,
        subscription_id: int,
        *,
        start_date,
        end_date,
        quantity,
        billing_amount,
        vendor,
        invoice_number="",
        invoice_pdf=None,
        product=None,
    ) -> None:
        sub = self._line_subscription(subscription_id)
        if sub.deal_status == ClientSubscription.DealStatus.LOST:
            raise ValueError("Deal is already marked Lost.")
        sub.save_renewal_draft(
            start_date=start_date,
            end_date=end_date,
            quantity=quantity,
            billing_amount=billing_amount,
            vendor=vendor,
            invoice_number=invoice_number,
            invoice_pdf=invoice_pdf,
            product=product,
        )

    @property
    def sent_to_emails(self) -> list[str]:
        recipients = list(self.recipients.all())
        if recipients:
            return [row.email for row in recipients]
        addr = (self.client_email or "").strip()
        return [addr] if addr else []

    def mark_recipient_opened(self, email: str) -> None:
        key = (email or "").strip().lower()
        if not key:
            return
        recipient = next(
            (row for row in self.recipients.all() if row.email.lower() == key),
            None,
        )
        if recipient is None or recipient.opened_at:
            return
        recipient.opened_at = timezone.now()
        recipient.save(update_fields=["opened_at"])


class RenewalSLASettings(models.Model):
    """Per-company clock and target for renewal quotation SLA."""

    class ClockMode(models.TextChoices):
        ALWAYS = "24x7", "24/7"
        WORKING = "working_hours", "Working hours"

    company = models.OneToOneField(
        CompanyProfile,
        on_delete=models.CASCADE,
        related_name="renewal_sla_settings",
    )
    clock_mode = models.CharField(
        max_length=20,
        choices=ClockMode.choices,
        default=ClockMode.WORKING,
    )
    target_hours = models.PositiveIntegerField(
        default=8,
        help_text="Hours allowed from renewal quantity received until the Zoho quote is sent.",
    )
    work_start = models.TimeField(default=time(9, 30))
    work_end = models.TimeField(default=time(18, 30))
    work_monday = models.BooleanField(default=True)
    work_tuesday = models.BooleanField(default=True)
    work_wednesday = models.BooleanField(default=True)
    work_thursday = models.BooleanField(default=True)
    work_friday = models.BooleanField(default=True)
    work_saturday = models.BooleanField(default=False)
    work_sunday = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "renewal SLA settings"
        verbose_name_plural = "renewal SLA settings"

    def __str__(self):
        return f"Renewal SLA settings for {self.company}"

    def working_weekdays(self) -> set[int]:
        mapping = (
            (self.work_monday, 0),
            (self.work_tuesday, 1),
            (self.work_wednesday, 2),
            (self.work_thursday, 3),
            (self.work_friday, 4),
            (self.work_saturday, 5),
            (self.work_sunday, 6),
        )
        days = {index for enabled, index in mapping if enabled}
        return days or {0, 1, 2, 3, 4}

    def uses_working_hours(self) -> bool:
        return self.clock_mode == self.ClockMode.WORKING and self.work_end > self.work_start


class SubscriptionRenewalInviteRecipient(models.Model):
    """One client inbox that received this renewal invite, with its own signed link."""

    invite = models.ForeignKey(
        SubscriptionRenewalInvite,
        on_delete=models.CASCADE,
        related_name="recipients",
    )
    email = models.EmailField()
    sent_at = models.DateTimeField(null=True, blank=True)
    opened_at = models.DateTimeField(null=True, blank=True)
    internet_message_id = models.CharField(max_length=998, blank=True)

    class Meta:
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(
                fields=["invite", "email"],
                name="uniq_renewal_invite_recipient_email",
            )
        ]

    def __str__(self):
        return f"{self.email} ← invite {self.invite_id}"


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


class SubscriptionRenewalInviteNewProduct(models.Model):
    """Extra catalog products requested on a renewal confirmation. Not a subscription."""

    invite = models.ForeignKey(
        SubscriptionRenewalInvite,
        on_delete=models.CASCADE,
        related_name="new_products",
    )
    product = models.ForeignKey(
        Product,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="renewal_invite_new_products",
    )
    company_name = models.CharField(max_length=255, blank=True)
    product_name = models.CharField(max_length=255)
    quantity = models.PositiveIntegerField()

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return f"{self.product_name} × {self.quantity}"


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


class CaseLockProduct(models.Model):
    """Product names offered on the Case lock public form."""

    company = models.ForeignKey(
        CompanyProfile,
        on_delete=models.CASCADE,
        related_name="caselock_products",
    )
    name = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["company", "name"],
                name="unique_caselock_product_per_company",
            ),
        ]

    def __str__(self):
        return self.name


class CaseLockInbox(models.Model):
    """
    Legacy per-product mailbox. New Case lock mail uses company.caselock_email.
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
    company = models.ForeignKey(
        CompanyProfile,
        on_delete=models.CASCADE,
        related_name="caselock_invites",
        null=True,
        blank=True,
    )
    inbox = models.ForeignKey(
        CaseLockInbox,
        on_delete=models.SET_NULL,
        related_name="invites",
        null=True,
        blank=True,
    )
    source_message_id = models.CharField(max_length=998)
    sender_email = models.CharField(max_length=254)
    mail_subject = models.CharField(max_length=255, blank=True)
    received_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the inbound mail arrived.",
    )
    email_sent_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-received_at", "-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["company", "source_message_id"],
                name="unique_caselock_invite_message_per_company",
            ),
        ]

    def __str__(self):
        return f"Invite {self.id} from {self.sender_email}"

    @property
    def inbound_at(self):
        return self.received_at or self.created_at


class CaseLockSubmission(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        APPROVED = "approved", "Final"
        DECLINED = "declined", "Declined"

    invite = models.OneToOneField(
        CaseLockInvite,
        on_delete=models.CASCADE,
        related_name="submission",
    )
    vendor_name = models.CharField(max_length=255, blank=True)
    vendor_gst = models.CharField("vendor GST", max_length=32, blank=True)
    client_name = models.CharField("customer name", max_length=255)
    client_contact_info = models.CharField("client contact info", max_length=255, blank=True)
    client_phone = models.CharField("client phone", max_length=64, blank=True)
    client_email = models.EmailField("client email 1", blank=True)
    client_email_2 = models.EmailField("client email 2", blank=True)
    client_gst = models.CharField("GST number", max_length=32, blank=True)
    product = models.ForeignKey(
        CaseLockProduct,
        on_delete=models.SET_NULL,
        related_name="submissions",
        null=True,
        blank=True,
    )
    product_name = models.CharField(max_length=255)
    product_quantity = models.PositiveIntegerField("quantity", default=1)
    timestamp = models.DateTimeField(
        auto_now_add=True,
        help_text="When the form was submitted.",
    )
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
    )
    status_changed_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When Final or Decline was set.",
    )

    class Meta:
        ordering = ["-timestamp"]

    def __str__(self):
        return f"Case lock {self.client_name}"

    def lock_key(self):
        gst = "".join((self.client_gst or "").split()).upper()
        if gst:
            customer = ("gst", gst)
        else:
            customer = ("name", (self.client_name or "").strip().casefold())
        if self.product_id:
            return ("id", self.product_id, self.product_quantity, customer)
        return (
            "name",
            (self.product_name or "").strip().lower(),
            self.product_quantity,
            customer,
        )


def annotate_internal_case_locks(submissions):
    """Mark first submitter of the current round with has_internal_case_lock.

    A round is GST (or company name if GST is empty) + product + quantity.
    Different company spellings with the same GST share one group. A Final
    closes that round; only later submissions can receive a new lock. Decline
    does not start a new round.
    """
    last_final_at = {}
    for submission in submissions:
        if submission.status != CaseLockSubmission.Status.APPROVED:
            continue
        decided = submission.status_changed_at
        if decided is None:
            continue
        key = submission.lock_key()
        previous = last_final_at.get(key)
        if previous is None or decided > previous:
            last_final_at[key] = decided

    winners = {}
    ordered = sorted(submissions, key=lambda s: (s.timestamp, s.pk))
    for submission in ordered:
        key = submission.lock_key()
        cutoff = last_final_at.get(key)
        if cutoff is not None and submission.timestamp <= cutoff:
            continue
        if key not in winners:
            winners[key] = submission.pk
    for submission in submissions:
        submission.has_internal_case_lock = winners.get(submission.lock_key()) == submission.pk
    return submissions


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

    class SourceChannel(models.TextChoices):
        EMAIL = "email", "Email"
        WHATSAPP = "whatsapp", "WhatsApp"
        INTERNAL = "internal", "Internal"

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
    source_channel = models.CharField(
        max_length=20,
        choices=SourceChannel.choices,
        blank=True,
        default="",
        help_text="How the customer opened the ticket (email, WhatsApp, etc.).",
    )
    customer_phone = models.CharField(
        max_length=64,
        blank=True,
        help_text="Customer mobile for WhatsApp-originated tickets.",
    )
    customer_firm_name = models.CharField(
        max_length=255,
        blank=True,
        help_text="Customer or firm name from WhatsApp support intake.",
    )
    is_internal = models.BooleanField(
        default=True,
        help_text="True for tickets created in Ticket360 UI; False for email/IMAP.",
    )
    email_conversation_id = models.CharField(
        max_length=512,
        blank=True,
        db_index=True,
        help_text=(
            "Outlook conversation of the customer email that opened this ticket. "
            "Later replies in that thread stay on this ticket until it is closed."
        ),
    )
    is_oem = models.BooleanField(
        default=False,
        help_text="True for External (OEM) tickets sent to a manufacturer.",
    )
    oem_company_name = models.CharField(max_length=255, blank=True)
    oem_to_email = models.EmailField(blank=True)
    oem_cc_emails = models.TextField(blank=True)
    subscription_id = models.CharField(max_length=64, blank=True)
    transferred_to = models.ForeignKey(
        "helpdesk.Ticket",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="oem_transfer_sources",
        help_text="OEM ticket created when this internal ticket was transferred.",
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


class SupportTeam(models.Model):
    """Per-company support engineers and the auto-assign toggle for new tickets."""

    company = models.OneToOneField(
        CompanyProfile,
        on_delete=models.CASCADE,
        related_name="support_team",
    )
    auto_assign_enabled = models.BooleanField(
        default=False,
        help_text="When on, new internal and email tickets go to the least-loaded engineer.",
    )
    members = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        blank=True,
        related_name="support_teams",
    )
    last_assigned = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        help_text="Used to rotate between engineers with the same open-ticket count.",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "support team"
        verbose_name_plural = "support teams"

    def __str__(self):
        return f"Support team for {self.company}"


class EngineerPendingDigest(models.Model):
    """Records that an engineer already got today's assigned-ticket morning mail."""

    company = models.ForeignKey(
        CompanyProfile,
        on_delete=models.CASCADE,
        related_name="engineer_pending_digests",
    )
    engineer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="engineer_pending_digests",
    )
    sent_on = models.DateField()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["company", "engineer", "sent_on"],
                name="uniq_engineer_pending_digest_day",
            )
        ]
        verbose_name = "engineer pending digest"
        verbose_name_plural = "engineer pending digests"

    def __str__(self):
        return f"{self.engineer_id} {self.sent_on}"


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


class OemDiaryCompany(models.Model):
    """OEM Email Diary company (product manufacturers plus custom names)."""

    company = models.ForeignKey(
        CompanyProfile,
        on_delete=models.CASCADE,
        related_name="oem_diary_companies",
    )
    name = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name", "id"]
        constraints = [
            models.UniqueConstraint(
                Lower(Trim("name")),
                models.F("company"),
                name="unique_oem_diary_company_name",
            ),
        ]

    def __str__(self):
        return self.name


class OemDiaryEmail(models.Model):
    """Extra OEM contact email stored against an Email Diary company."""

    diary_company = models.ForeignKey(
        OemDiaryCompany,
        on_delete=models.CASCADE,
        related_name="emails",
    )
    email = models.EmailField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(
                Lower("email"),
                models.F("diary_company"),
                name="unique_oem_diary_email",
            ),
        ]

    def __str__(self):
        return self.email


class ProcurementDeal(models.Model):
    """One product line of a procurement deal. Lines share deal_no per company."""

    company = models.ForeignKey(
        CompanyProfile,
        on_delete=models.CASCADE,
        related_name="procurement_deals",
    )
    deal_no = models.CharField(max_length=32, db_index=True)
    deal_name = models.CharField(max_length=255, blank=True)
    category = models.CharField(max_length=128, blank=True)
    product = models.ForeignKey(
        Product,
        on_delete=models.PROTECT,
        related_name="procurement_deal_lines",
    )
    product_company = models.CharField(max_length=255, blank=True)
    quantity = models.PositiveIntegerField()
    price = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]
        indexes = [
            models.Index(fields=["company", "deal_no"]),
        ]

    def __str__(self):
        return f"{self.deal_no} / {self.product}"


class DealVendor(models.Model):
    company = models.ForeignKey(
        CompanyProfile,
        on_delete=models.CASCADE,
        related_name="deal_vendors",
    )
    deal_no = models.CharField(max_length=32, db_index=True)
    deal = models.ForeignKey(
        ProcurementDeal,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="invited_vendors",
    )
    vendor = models.ForeignKey(
        Vendor,
        on_delete=models.CASCADE,
        related_name="procurement_invites",
    )
    price = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    note = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(
                fields=["company", "deal_no", "vendor"],
                name="unique_deal_vendor_per_company_deal",
            ),
        ]

    def __str__(self):
        return f"{self.deal_no} / {self.vendor}"


class DealRound(models.Model):
    company = models.ForeignKey(
        CompanyProfile,
        on_delete=models.CASCADE,
        related_name="deal_rounds",
    )
    deal_no = models.CharField(max_length=32, db_index=True)
    round_number = models.PositiveIntegerField()
    name = models.CharField(max_length=64, blank=True)
    is_final = models.BooleanField(default=False)
    finalised_price = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True
    )
    finalised_vendor = models.ForeignKey(
        Vendor,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="finalised_deal_rounds",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["round_number"]
        constraints = [
            models.UniqueConstraint(
                fields=["company", "deal_no", "round_number"],
                name="unique_deal_round_per_company_deal",
            ),
        ]

    def __str__(self):
        return self.display_name

    @property
    def display_name(self) -> str:
        return (self.name or "").strip() or f"Round {self.round_number}"


class VendorPriceSubmission(models.Model):
    company = models.ForeignKey(
        CompanyProfile,
        on_delete=models.CASCADE,
        related_name="vendor_price_submissions",
    )
    deal_no = models.CharField(max_length=32, db_index=True)
    deal = models.ForeignKey(
        ProcurementDeal,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="price_submissions",
    )
    vendor = models.ForeignKey(
        Vendor,
        on_delete=models.CASCADE,
        related_name="price_submissions",
    )
    deal_round = models.ForeignKey(
        DealRound,
        on_delete=models.CASCADE,
        related_name="submissions",
    )
    submission_token = models.UUIDField(default=uuid.uuid4, unique=True)
    expires_at = models.DateTimeField()
    is_submitted = models.BooleanField(default=False)
    submitted_at = models.DateTimeField(null=True, blank=True)
    is_final_quotation = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return f"{self.vendor} / {self.deal_no} / {self.deal_round}"

    def is_expired(self) -> bool:
        return timezone.now() > self.expires_at


class VendorProductPrice(models.Model):
    submission = models.ForeignKey(
        VendorPriceSubmission,
        on_delete=models.CASCADE,
        related_name="product_prices",
    )
    product = models.ForeignKey(
        Product,
        on_delete=models.CASCADE,
        related_name="vendor_submitted_prices",
    )
    price = models.DecimalField(max_digits=12, decimal_places=2)
    note = models.TextField(blank=True)

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return f"{self.submission_id} / {self.product_id}"


class DealRoundVendorPrice(models.Model):
    deal_round = models.ForeignKey(
        DealRound,
        on_delete=models.CASCADE,
        related_name="vendor_prices",
    )
    vendor = models.ForeignKey(
        Vendor,
        on_delete=models.CASCADE,
        related_name="round_prices",
    )
    product = models.ForeignKey(
        Product,
        on_delete=models.CASCADE,
        related_name="round_vendor_prices",
    )
    price = models.DecimalField(max_digits=12, decimal_places=2)
    note = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(
                fields=["deal_round", "vendor", "product"],
                name="unique_round_vendor_product_price",
            ),
        ]

    def __str__(self):
        return f"{self.deal_round} / {self.vendor} / {self.product}"


class DealVendorPrice(models.Model):
    company = models.ForeignKey(
        CompanyProfile,
        on_delete=models.CASCADE,
        related_name="manual_deal_vendor_prices",
    )
    deal_no = models.CharField(max_length=32, db_index=True)
    deal = models.ForeignKey(
        ProcurementDeal,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="manual_vendor_prices",
    )
    vendor = models.ForeignKey(
        Vendor,
        on_delete=models.CASCADE,
        related_name="manual_deal_prices",
    )
    product = models.ForeignKey(
        Product,
        on_delete=models.CASCADE,
        related_name="manual_deal_prices",
    )
    price = models.DecimalField(max_digits=12, decimal_places=2)
    note = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return f"{self.deal_no} / {self.vendor} / {self.product}"


class Lead(models.Model):
    """Inbound sales lead (WhatsApp, email, or manual)."""

    class Source(models.TextChoices):
        EMAIL = "email", "Email"
        WHATSAPP = "whatsapp", "WhatsApp"
        MANUAL = "manual", "Manual"

    class Status(models.TextChoices):
        NEW = "new", "New"
        CONTACTED = "contacted", "Contacted"
        QUALIFIED = "qualified", "Qualified"
        WON = "won", "Won"
        LOST = "lost", "Lost"

    company = models.ForeignKey(
        CompanyProfile,
        on_delete=models.CASCADE,
        related_name="leads",
        null=True,
        blank=True,
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="leads_created",
        verbose_name="generated by",
        help_text="User who created the lead (manual). Blank for WhatsApp/email ingest.",
    )
    source = models.CharField(
        max_length=16,
        choices=Source.choices,
        default=Source.MANUAL,
    )
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.NEW,
    )
    contact_name = models.CharField(max_length=255)
    company_name = models.CharField("company", max_length=255, blank=True)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=64, blank=True)
    requirement = models.TextField(blank=True)
    notes = models.TextField(blank=True)
    products = models.ManyToManyField(
        Product,
        blank=True,
        related_name="leads",
    )
    whatsapp_wa_id = models.CharField(
        "WhatsApp user id",
        max_length=64,
        blank=True,
        db_index=True,
        help_text="Customer WhatsApp phone / wa_id.",
    )
    whatsapp_message_id = models.CharField(
        max_length=128,
        blank=True,
        help_text="Inbound WhatsApp message id (idempotency).",
    )
    email_message_id = models.CharField(
        max_length=255,
        blank=True,
        help_text="Inbound email Message-ID (idempotency).",
    )
    last_inbound_at = models.DateTimeField(
        "last WhatsApp inbound at",
        null=True,
        blank=True,
        help_text="When the customer last messaged us on WhatsApp.",
    )
    last_inbound_message_id = models.CharField(
        max_length=128,
        blank=True,
        help_text="Last processed inbound WhatsApp message id (retry protection).",
    )
    last_welcome_sent_at = models.DateTimeField(
        "last welcome/menu sent at",
        null=True,
        blank=True,
    )
    raw_payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["company", "whatsapp_message_id"],
                condition=~models.Q(whatsapp_message_id="")
                & models.Q(company__isnull=False),
                name="unique_lead_whatsapp_message_per_company",
            ),
            models.UniqueConstraint(
                fields=["whatsapp_message_id"],
                condition=~models.Q(whatsapp_message_id="")
                & models.Q(company__isnull=True),
                name="unique_lead_whatsapp_message_unassigned",
            ),
            models.UniqueConstraint(
                fields=["company", "email_message_id"],
                condition=~models.Q(email_message_id="")
                & models.Q(company__isnull=False),
                name="unique_lead_email_message_per_company",
            ),
            models.UniqueConstraint(
                fields=["email_message_id"],
                condition=~models.Q(email_message_id="")
                & models.Q(company__isnull=True),
                name="unique_lead_email_message_unassigned",
            ),
        ]

    def __str__(self):
        return f"{self.contact_name} ({self.get_source_display()})"

    @property
    def generated_by_label(self) -> str:
        """Who/what generated this lead — user name, or channel for inbound."""
        if self.created_by_id:
            user = self.created_by
            name = (user.get_full_name() or "").strip()
            if name:
                return name
            return (getattr(user, "email", None) or str(user)).strip() or "User"
        if self.source == self.Source.WHATSAPP:
            return "WhatsApp"
        if self.source == self.Source.EMAIL:
            return "Email"
        return self.get_source_display() or "—"


class TeamNotification(models.Model):
    """In-app bell item. One row per recipient so read state stays personal."""

    class Kind(models.TextChoices):
        SUPPORT_TICKET = "support_ticket", "Support ticket"
        QUOTATION_PRICE = "quotation_price", "Quotation price"
        FINAL_QUOTATION = "final_quotation", "Final quotation"
        CASELOCK_SUBMIT = "caselock_submit", "Case lock form"
        NEW_LEAD = "new_lead", "New lead"
        TICKET_OVERDUE = "ticket_overdue", "Ticket overdue"
        SUBSCRIPTION_EXPIRY = "subscription_expiry", "Subscription expiry"
        RENEWAL_CONFIRMED = "renewal_confirmed", "Renewal confirmed"
        TICKET_ASSIGNED = "ticket_assigned", "Ticket assigned"
        TICKET_REPLY = "ticket_reply", "Ticket reply"
        EMAIL_FAILED = "email_failed", "Email failed"

    company = models.ForeignKey(
        CompanyProfile,
        on_delete=models.CASCADE,
        related_name="team_notifications",
    )
    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="team_notifications",
    )
    kind = models.CharField(max_length=32, choices=Kind.choices)
    title = models.CharField(max_length=255)
    body = models.CharField(max_length=500, blank=True)
    target_url = models.CharField(max_length=500)
    dedupe_key = models.CharField(max_length=190)
    is_read = models.BooleanField(default=False)
    read_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["recipient", "dedupe_key"],
                name="unique_team_notification_per_recipient",
            ),
        ]
        indexes = [
            models.Index(fields=["recipient", "is_read", "created_at"]),
            models.Index(fields=["company", "created_at"]),
        ]

    def __str__(self):
        return self.title


class VendorPriceRequest(models.Model):
    """Price quote asked from a vendor for one expiring subscription."""

    company = models.ForeignKey(
        CompanyProfile,
        on_delete=models.CASCADE,
        related_name="vendor_price_requests",
    )
    subscription = models.ForeignKey(
        ClientSubscription,
        on_delete=models.CASCADE,
        related_name="vendor_price_requests",
    )
    vendor = models.ForeignKey(
        Vendor,
        on_delete=models.CASCADE,
        related_name="subscription_price_requests",
    )
    token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    product_name = models.CharField(max_length=255)
    quantity = models.PositiveIntegerField()
    client_name = models.CharField(max_length=255, blank=True)
    end_date = models.DateField(null=True, blank=True)
    price = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    email_sent_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"{self.vendor} / {self.product_name}"


class ZohoBooksSync(models.Model):
    """Tracks a Zoho Books copy so later syncs only ask for changes."""

    class Kind(models.TextChoices):
        INVOICE = "invoice", "Sales invoice"
        BILL = "bill", "Purchase bill"

    class Mode(models.TextChoices):
        IDLE = "idle", "Idle"
        FULL = "full", "Full"
        INCREMENTAL = "incremental", "Incremental"

    company = models.ForeignKey(
        CompanyProfile,
        on_delete=models.CASCADE,
        related_name="zoho_books_syncs",
    )
    organization_id = models.CharField(max_length=64)
    kind = models.CharField(max_length=16, choices=Kind.choices)
    mode = models.CharField(max_length=16, choices=Mode.choices, default=Mode.IDLE)
    next_page = models.PositiveIntegerField(default=1)
    last_completed_at = models.DateTimeField(null=True, blank=True)
    modified_after = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["company", "organization_id", "kind"],
                name="unique_zoho_books_sync",
            ),
        ]

    def __str__(self):
        return f"{self.company_id} {self.kind}"


class ZohoBooksDocument(models.Model):
    """One saved Zoho Books sales invoice or purchase bill."""

    company = models.ForeignKey(
        CompanyProfile,
        on_delete=models.CASCADE,
        related_name="zoho_books_documents",
    )
    organization_id = models.CharField(max_length=64)
    kind = models.CharField(max_length=16, choices=ZohoBooksSync.Kind.choices)
    zoho_id = models.CharField(max_length=64)
    number = models.CharField(max_length=128, blank=True)
    document_date = models.DateField(null=True, blank=True)
    due_date = models.DateField(null=True, blank=True)
    party_name = models.CharField(max_length=255, blank=True)
    gst_no = models.CharField(max_length=32, blank=True)
    status = models.CharField(max_length=32, blank=True)
    currency_code = models.CharField(max_length=8, blank=True)
    total = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    balance = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    last_modified_raw = models.CharField(max_length=40, blank=True)
    synced_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-document_date", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["company", "organization_id", "kind", "zoho_id"],
                name="unique_zoho_books_document",
            ),
        ]
        indexes = [
            models.Index(
                fields=["company", "organization_id", "kind", "document_date"],
                name="zoho_doc_period_idx",
            ),
            models.Index(
                fields=["company", "organization_id", "kind", "number"],
                name="zoho_doc_number_idx",
            ),
        ]

    def __str__(self):
        return self.number or self.zoho_id


class ZohoBooksDocumentLine(models.Model):
    """Product line saved with a Zoho invoice or bill."""

    document = models.ForeignKey(
        ZohoBooksDocument,
        on_delete=models.CASCADE,
        related_name="lines",
    )
    line_id = models.CharField(max_length=64)
    item_id = models.CharField(max_length=64, blank=True)
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    sku = models.CharField(max_length=128, blank=True)
    quantity = models.PositiveIntegerField(default=1)
    amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    serial_number = models.CharField(max_length=128, blank=True)
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["document", "line_id"],
                name="unique_zoho_books_document_line",
            ),
        ]
        indexes = [
            models.Index(fields=["item_id"], name="zoho_line_item_idx"),
        ]

    def __str__(self):
        return self.name
