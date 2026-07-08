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
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.company_name

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
