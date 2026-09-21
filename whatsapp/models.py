from django.conf import settings
from django.db import models

from accounts.models import CompanyProfile


class WhatsAppAccount(models.Model):
    """Maps a Meta phone_number_id to a company (shared WhatsApp infrastructure)."""

    company = models.ForeignKey(
        CompanyProfile,
        on_delete=models.CASCADE,
        related_name="whatsapp_accounts",
    )
    label = models.CharField(max_length=120, blank=True)
    phone_number_id = models.CharField(max_length=64, unique=True)
    waba_id = models.CharField("WhatsApp Business Account id", max_length=64, blank=True)
    display_phone = models.CharField(max_length=32, blank=True)
    access_token = models.TextField(
        blank=True,
        help_text="Optional override; falls back to WHATSAPP_ACCESS_TOKEN env.",
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["company__company_name", "phone_number_id"]

    def __str__(self):
        return self.label or self.display_phone or self.phone_number_id

    def resolved_access_token(self) -> str:
        return (self.access_token or getattr(settings, "WHATSAPP_ACCESS_TOKEN", "") or "").strip()


class WhatsAppMessageLog(models.Model):
    """Inbound/outbound WhatsApp traffic for debugging and future handlers."""

    class Direction(models.TextChoices):
        INBOUND = "inbound", "Inbound"
        OUTBOUND = "outbound", "Outbound"

    company = models.ForeignKey(
        CompanyProfile,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="whatsapp_message_logs",
    )
    account = models.ForeignKey(
        WhatsAppAccount,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="message_logs",
    )
    direction = models.CharField(max_length=16, choices=Direction.choices)
    wa_message_id = models.CharField(max_length=128, blank=True, db_index=True)
    wa_from = models.CharField(max_length=64, blank=True)
    wa_to = models.CharField(max_length=64, blank=True)
    event_type = models.CharField(max_length=64, blank=True)
    payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"{self.direction} {self.event_type or self.wa_message_id or self.pk}"
