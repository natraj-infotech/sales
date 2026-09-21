from django.apps import AppConfig


class WhatsappConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "whatsapp"
    verbose_name = "WhatsApp"

    def ready(self):
        # Register built-in handlers (leads, future features).
        from . import handlers  # noqa: F401
