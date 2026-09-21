from django.contrib import admin

from .models import WhatsAppAccount, WhatsAppMessageLog


@admin.register(WhatsAppAccount)
class WhatsAppAccountAdmin(admin.ModelAdmin):
    list_display = (
        "label",
        "company",
        "phone_number_id",
        "display_phone",
        "is_active",
        "updated_at",
    )
    list_filter = ("is_active", "company")
    search_fields = ("label", "phone_number_id", "display_phone", "waba_id")


@admin.register(WhatsAppMessageLog)
class WhatsAppMessageLogAdmin(admin.ModelAdmin):
    list_display = (
        "created_at",
        "direction",
        "event_type",
        "wa_from",
        "wa_to",
        "company",
        "wa_message_id",
    )
    list_filter = ("direction", "company")
    search_fields = ("wa_message_id", "wa_from", "wa_to", "event_type")
    readonly_fields = ("payload", "created_at")
