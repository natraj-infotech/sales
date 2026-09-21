from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from .models import User, CompanyProfile, CompanyMember


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    ordering = ("email",)
    list_display = ("email", "first_name", "last_name", "is_staff", "is_active")
    search_fields = ("email", "first_name", "last_name")
    fieldsets = (
        (None, {"fields": ("email", "password")}),
        (
            "Permissions",
            {
                "fields": (
                    "is_active",
                    "is_staff",
                    "is_superuser",
                    "groups",
                    "user_permissions",
                ),
            },
        ),
        ("Important dates", {"fields": ("last_login", "date_joined")}),
    )
    add_fieldsets = (
        (
            None,
            {
                "classes": ("wide",),
                "fields": ("email", "password1", "password2"),
            },
        ),
    )


@admin.register(CompanyProfile)
class CompanyProfileAdmin(admin.ModelAdmin):
    list_display = (
        "company_name",
        "user",
        "email",
        "phone",
        "gst_number",
        "created_at",
    )
    search_fields = ("company_name", "email", "gst_number", "user__email")
    fields = (
        "user",
        "company_name",
        "company_logo",
        "phone",
        "email",
        "address",
        "gst_number",
        "cin_number",
        "support_email",
        "support_email_app_password",
        "caselock_email",
        "caselock_email_app_password",
        "oem_email",
        "oem_email_app_password",
        "outgoing_email",
        "outgoing_email_app_password",
        "purchase_department_emails",
        "regards_email",
        "regards_contact",
        "expiry_email_initial_paragraph",
        "expiry_email_end_paragraph",
        "expiry_client_emails_enabled",
        "expiry_team_digest_enabled",
        "expiry_notify_at_days",
        "expiry_team_extra_emails",
        "expiry_team_digest_last_sent",
    )


@admin.register(CompanyMember)
class CompanyMemberAdmin(admin.ModelAdmin):
    list_display = (
        "company",
        "user",
        "role",
        "sales_access",
        "can_grant_sales",
        "support_ticket_access",
        "joined_at",
    )
    list_filter = ("role", "sales_access", "can_grant_sales", "support_ticket_access")
    search_fields = ("company__company_name", "user__email")
    autocomplete_fields = ("company", "user")
