from django.apps import AppConfig


class BusinessConfig(AppConfig):
    name = "business"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        from business import helpdesk_signals  # noqa: F401
