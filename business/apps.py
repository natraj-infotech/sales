from django.apps import AppConfig


class BusinessConfig(AppConfig):
    name = "business"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        from business import helpdesk_signals  # noqa: F401
        from business.local_scheduler import start_if_runserver

        start_if_runserver()
