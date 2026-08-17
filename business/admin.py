from django.contrib import admin

from .models import (
    Vendor,
    Client,
    CaseLockInbox,
    CaseLockInvite,
    CaseLockSubmission,
    Product,
    ClientSubscription,
    SubscriptionRenewalInvite,
    SubscriptionRenewalInviteLine,
    SupportTicket,
    SupportTicketComment,
    SupportTicketMeta,
    SupportSLASettings,
    TicketPriorityRule,
)


@admin.register(Vendor)
class VendorAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "company",
        "gst_number",
        "email",
        "phone",
        "created_at",
    )
    list_filter = ("company",)
    search_fields = ("name", "email", "gst_number", "phone")


@admin.register(Client)
class ClientAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "company",
        "gst_number",
        "email",
        "email_2",
        "email_3",
        "phone",
        "phone_2",
        "phone_3",
        "created_at",
    )
    list_filter = ("company",)
    search_fields = (
        "name",
        "email",
        "email_2",
        "email_3",
        "gst_number",
        "phone",
        "phone_2",
        "phone_3",
    )


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "category",
        "company",
        "vendor",
        "hsn_code",
        "sku",
        "unit_price",
        "created_at",
    )
    list_filter = ("company", "vendor")
    search_fields = (
        "name",
        "sku",
        "category",
        "company_name",
        "hsn_code",
        "components",
        "notes",
    )


@admin.register(ClientSubscription)
class ClientSubscriptionAdmin(admin.ModelAdmin):
    list_display = (
        "client",
        "product",
        "quantity",
        "serial_number",
        "subscription_code",
        "invoice_number",
        "status",
        "start_date",
        "end_date",
        "billing_amount",
        "company",
    )
    list_filter = ("company", "status")
    search_fields = (
        "client__name",
        "product__name",
        "serial_number",
        "subscription_code",
        "invoice_number",
        "notes",
    )


@admin.register(SubscriptionRenewalInvite)
class SubscriptionRenewalInviteAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "company",
        "client_email",
        "email_subject",
        "email_sent_at",
        "read_at",
        "read_source",
        "submitted_at",
        "quotation_sent_at",
        "zoho_quote_number",
        "created_at",
    )
    list_filter = ("company",)
    search_fields = ("client_email",)


@admin.register(SubscriptionRenewalInviteLine)
class SubscriptionRenewalInviteLineAdmin(admin.ModelAdmin):
    list_display = (
        "company_name",
        "product_name",
        "current_quantity",
        "renewal_count",
        "invite",
    )
    search_fields = ("company_name", "product_name")


@admin.register(SupportTicket)
class SupportTicketAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "subject",
        "company",
        "department",
        "priority",
        "assignee",
        "source_sender",
        "created_by",
        "status",
        "due_at",
        "completed_at",
        "created_at",
    )
    list_filter = ("company", "status", "department", "priority")
    search_fields = (
        "subject",
        "description",
        "created_by__email",
        "source_sender",
        "source_message_id",
        "remarks",
    )
    raw_id_fields = ("created_by",)


@admin.register(SupportTicketComment)
class SupportTicketCommentAdmin(admin.ModelAdmin):
    list_display = ("ticket", "user", "internal_note", "created_at")
    list_filter = ("internal_note", "created_at")
    search_fields = ("ticket__subject", "user__email", "body")


@admin.register(SupportTicketMeta)
class SupportTicketMetaAdmin(admin.ModelAdmin):
    list_display = (
        "ticket",
        "department",
        "is_internal",
        "priority_source",
        "first_response_due_at",
        "resolve_due_at",
        "first_responded_at",
        "created_by",
        "completed_at",
        "updated_at",
    )
    list_filter = ("department", "is_internal", "priority_source")
    search_fields = ("ticket__title", "remarks", "created_by__email")


@admin.register(SupportSLASettings)
class SupportSLASettingsAdmin(admin.ModelAdmin):
    list_display = (
        "company",
        "sla_mode",
        "email_auto_priority",
        "updated_at",
    )
    list_filter = ("sla_mode", "email_auto_priority")


@admin.register(TicketPriorityRule)
class TicketPriorityRuleAdmin(admin.ModelAdmin):
    list_display = (
        "company",
        "priority",
        "keywords",
        "sort_order",
        "is_active",
        "updated_at",
    )
    list_filter = ("company", "priority", "is_active")
    search_fields = ("keywords",)


@admin.register(CaseLockInbox)
class CaseLockInboxAdmin(admin.ModelAdmin):
    list_display = (
        "mailbox_email",
        "product",
        "company",
        "is_active",
        "updated_at",
    )
    list_filter = ("company", "is_active")
    search_fields = ("mailbox_email", "product__name", "notes")


@admin.register(CaseLockInvite)
class CaseLockInviteAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "inbox",
        "sender_email",
        "mail_subject",
        "email_sent_at",
        "created_at",
    )
    list_filter = ("inbox__company",)
    search_fields = ("sender_email", "mail_subject", "source_message_id")


@admin.register(CaseLockSubmission)
class CaseLockSubmissionAdmin(admin.ModelAdmin):
    list_display = (
        "vendor_name",
        "client_name",
        "client_phone",
        "client_email",
        "product_name",
        "product_quantity",
        "status",
        "timestamp",
    )
    list_filter = ("status",)
    search_fields = (
        "vendor_name",
        "client_name",
        "client_phone",
        "client_email",
        "client_email_2",
        "product_name",
        "vendor_gst",
        "client_gst",
    )
