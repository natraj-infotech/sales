try:
    from .celery import app as celery_app
except Exception:  # pragma: no cover - keep Django booting without Celery installed
    celery_app = None

__all__ = ("celery_app",)
