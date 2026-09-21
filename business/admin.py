from django.contrib import admin

from .models import (
    Vendor,
    Client,
    CaseLockInbox,
    CaseLockInvite,
    CaseLockProduct,
    CaseLockSubmission,
    Product,
    Lead,
    ClientSubscription,
    SubscriptionRenewalInvite,
    SubscriptionRenewalInviteLine,
    SubscriptionRenewalInviteNewProduct,
    SubscriptionRenewalInviteRecipient,
    SupportTicket,
    SupportTicketComment,
    SupportTicketMeta,
    SupportSLASettings,
    TicketPriorityRule,
    OemDiaryCompany,
    OemDiaryEmail,
    ProcurementDeal,
    DealVendor,
    DealRound,
    VendorPriceSubmission,
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
    filter_horizontal = ("catalog_products",)
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
        "invoice_pdf",
        "status",
        "deal_status",
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


class SubscriptionRenewalInviteRecipientInline(admin.TabularInline):
    model = SubscriptionRenewalInviteRecipient
    extra = 0
    readonly_fields = ("email", "sent_at", "opened_at", "internet_message_id")


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
        "submitted_by_email",
        "quotation_sent_at",
        "deal_status",
        "zoho_quote_number",
        "created_at",
    )
    list_filter = ("company",)
    search_fields = ("client_email", "submitted_by_email", "recipients__email")
    inlines = (SubscriptionRenewalInviteRecipientInline,)


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


@admin.register(SubscriptionRenewalInviteNewProduct)
class SubscriptionRenewalInviteNewProductAdmin(admin.ModelAdmin):
    list_display = ("product_name", "quantity", "company_name", "invite")
    search_fields = ("product_name", "company_name")


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
        "is_oem",
        "oem_company_name",
        "oem_to_email",
        "subscription_id",
        "priority_source",
        "first_response_due_at",
        "resolve_due_at",
        "first_responded_at",
        "created_by",
        "completed_at",
        "updated_at",
    )
    list_filter = ("department", "is_internal", "is_oem", "priority_source")
    search_fields = ("ticket__title", "remarks", "created_by__email", "oem_company_name", "oem_to_email")


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


@admin.register(CaseLockProduct)
class CaseLockProductAdmin(admin.ModelAdmin):
    list_display = ("name", "company", "created_at")
    list_filter = ("company",)
    search_fields = ("name",)


@admin.register(CaseLockInvite)
class CaseLockInviteAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "company",
        "inbox",
        "sender_email",
        "mail_subject",
        "received_at",
        "email_sent_at",
        "created_at",
    )
    list_filter = ("company",)
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


class OemDiaryEmailInline(admin.TabularInline):
    model = OemDiaryEmail
    extra = 1


@admin.register(OemDiaryCompany)
class OemDiaryCompanyAdmin(admin.ModelAdmin):
    list_display = ("name", "company", "created_at")
    list_filter = ("company",)
    search_fields = ("name",)
    inlines = [OemDiaryEmailInline]


@admin.register(ProcurementDeal)
class ProcurementDealAdmin(admin.ModelAdmin):
    list_display = ("deal_no", "product", "quantity", "company", "created_at")
    list_filter = ("company",)
    search_fields = ("deal_no", "product__name")


@admin.register(DealVendor)
class DealVendorAdmin(admin.ModelAdmin):
    list_display = ("deal_no", "vendor", "company", "created_at")
    list_filter = ("company",)
    search_fields = ("deal_no", "vendor__name")


@admin.register(DealRound)
class DealRoundAdmin(admin.ModelAdmin):
    list_display = ("deal_no", "name", "round_number", "is_final", "company")
    list_filter = ("company", "is_final")
    search_fields = ("deal_no", "name")


@admin.register(VendorPriceSubmission)
class VendorPriceSubmissionAdmin(admin.ModelAdmin):
    list_display = (
        "deal_no",
        "vendor",
        "deal_round",
        "is_submitted",
        "expires_at",
        "company",
    )
    list_filter = ("company", "is_submitted")
    search_fields = ("deal_no", "vendor__name", "submission_token")


@admin.register(Lead)
class LeadAdmin(admin.ModelAdmin):
    list_display = (
        "contact_name",
        "company_name",
        "email",
        "phone",
        "source",
        "created_by",
        "status",
        "company",
        "created_at",
    )
    list_filter = ("company", "source", "status", "created_by")
    search_fields = (
        "contact_name",
        "company_name",
        "email",
        "phone",
        "requirement",
        "whatsapp_wa_id",
        "whatsapp_message_id",
        "email_message_id",
        "created_by__email",
    )
    filter_horizontal = ("products",)
    readonly_fields = (
        "created_at",
        "updated_at",
        "raw_payload",
        "created_by",
        "last_inbound_at",
        "last_inbound_message_id",
        "last_welcome_sent_at",
    )
