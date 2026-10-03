from django.contrib.auth.models import AbstractUser, BaseUserManager
from django.conf import settings
from django.db import models


class UserManager(BaseUserManager):
    use_in_migrations = True

    def _create_user(self, email, password, **extra_fields):
        if not email:
            raise ValueError("Users must have an email address")
        email = self.normalize_email(email)
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, email, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", False)
        extra_fields.setdefault("is_superuser", False)
        return self._create_user(email, password, **extra_fields)

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        if extra_fields.get("is_staff") is not True:
            raise ValueError("Superuser must have is_staff=True.")
        if extra_fields.get("is_superuser") is not True:
            raise ValueError("Superuser must have is_superuser=True.")
        return self._create_user(email, password, **extra_fields)


class User(AbstractUser):
    username = None
    email = models.EmailField("email address", unique=True)

    objects = UserManager()

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []

    class Meta:
        db_table = "accounts_user"


class CompanyProfile(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="company_profile",
    )
    company_name = models.CharField(max_length=255)
    company_logo = models.ImageField(
        "company logo",
        upload_to="company_logos/",
        blank=True,
        null=True,
        help_text="Shown in the header of client expiry reminder emails.",
    )
    phone = models.CharField(max_length=64, blank=True)
    email = models.EmailField("company email", blank=True)
    address = models.TextField(blank=True)
    gst_number = models.CharField("GST number", max_length=32, blank=True)
    cin_number = models.CharField("CIN number", max_length=32, blank=True)
    support_email = models.EmailField("support email", blank=True)
    support_email_app_password = models.CharField(
        "support email app password",
        max_length=256,
        blank=True,
        help_text="App password for the support mailbox (e.g. Gmail app password).",
    )
    caselock_email = models.EmailField(
        "case lock email",
        blank=True,
        help_text="Mailbox that receives Case lock requests. Incoming mail gets a form link.",
    )
    caselock_email_app_password = models.CharField(
        "case lock email app password",
        max_length=256,
        blank=True,
        help_text="App password for the Case lock mailbox (e.g. Gmail app password).",
    )
    oem_email = models.EmailField(
        "OEM email",
        blank=True,
        help_text="From address used when sending OEM support tickets.",
    )
    oem_email_app_password = models.CharField(
        "OEM email app password",
        max_length=256,
        blank=True,
        help_text="App password for the OEM mailbox (e.g. Gmail app password).",
    )
    outgoing_email = models.EmailField(
        "outgoing / SMTP email",
        blank=True,
        help_text="Address used to send outbound mail.",
    )
    outgoing_email_app_password = models.CharField(
        "outgoing email app password",
        max_length=256,
        blank=True,
        help_text="App password for the outgoing mailbox.",
    )
    purchase_department_emails = models.TextField(
        "purchase department emails",
        blank=True,
        help_text="Recipients for procurement deal comparison PDFs "
        "(one per line, or comma-separated).",
    )
    regards_email = models.TextField(
        "regards emails",
        blank=True,
        help_text="Emails shown under Best Regards in client renewal emails "
        "(one per line, or comma-separated).",
    )
    regards_contact = models.CharField(
        "regards contact details",
        max_length=255,
        blank=True,
        help_text="Phone or other contact shown under Best Regards in client renewal emails.",
    )
    expiry_email_subject = models.CharField(
        "expiry email subject",
        max_length=255,
        blank=True,
        help_text="Subject for client renewal reminder emails. "
        "Leave blank to use the default. Merge tags: "
        "{{client_name}}, {{product_name}}, {{days_left}}, {{expiry_date}}.",
    )
    expiry_email_initial_paragraph = models.TextField(
        "expiry email initial paragraph",
        blank=True,
        help_text="Shown above the product table in client subscription expiry emails. "
        "Leave blank to use the default text.",
    )
    expiry_email_end_paragraph = models.TextField(
        "expiry email end paragraph",
        blank=True,
        help_text="Shown below the product table in client subscription expiry emails. "
        "Leave blank to use the default text.",
    )
    expiry_client_emails_enabled = models.BooleanField(
        "email clients before subscription expiry",
        default=False,
        help_text="Send renewal reminders to clients when subscriptions hit selected day thresholds.",
    )
    expiry_team_digest_enabled = models.BooleanField(
        "daily team digest for expiring subscriptions",
        default=False,
        help_text="Send a daily summary to your sales team listing subscriptions expiring soon.",
    )
    expiry_notify_at_days = models.CharField(
        max_length=64,
        default="30,15,7",
        help_text="Comma-separated days-before-expiry thresholds (e.g. 30,15,7).",
    )
    expiry_team_extra_emails = models.TextField(
        blank=True,
        help_text="Optional extra recipients for the team digest (comma or newline separated).",
    )
    expiry_team_digest_last_sent = models.DateField(null=True, blank=True)
    support_digest_last_sent = models.DateField(null=True, blank=True)
    zoho_refresh_token = models.TextField(blank=True)
    zoho_access_token = models.TextField(blank=True)
    zoho_access_token_expires_at = models.DateTimeField(null=True, blank=True)
    zoho_api_domain = models.CharField(max_length=128, blank=True)
    zoho_accounts_domain = models.CharField(max_length=128, blank=True)
    zoho_connected_email = models.EmailField(blank=True)
    zoho_connected_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.company_name

    @property
    def zoho_connected(self) -> bool:
        return bool((self.zoho_refresh_token or "").strip())

    def company_user_ids(self):
        member_ids = self.members.values_list("user_id", flat=True)
        return {self.user_id, *member_ids}


class CompanyMember(models.Model):
    class Role(models.TextChoices):
        OWNER = "owner", "Owner"
        MEMBER = "member", "Member"

    company = models.ForeignKey(
        CompanyProfile,
        on_delete=models.CASCADE,
        related_name="members",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="company_memberships",
    )
    role = models.CharField(
        max_length=20,
        choices=Role.choices,
        default=Role.MEMBER,
    )
    sales_access = models.BooleanField(
        default=False,
        help_text="Can view and manage sales module data for this company.",
    )
    can_grant_sales = models.BooleanField(
        default=False,
        help_text="Can grant or revoke sales module access for teammates.",
    )
    support_ticket_access = models.BooleanField(
        default=False,
        help_text="Can view and manage the support tickets dashboard for this company.",
    )
    joined_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "accounts_company_member"
        constraints = [
            models.UniqueConstraint(
                fields=["company", "user"],
                name="unique_company_member",
            ),
        ]
        ordering = ["role", "user__email"]

    def __str__(self):
        return f"{self.user.email} @ {self.company.company_name}"
