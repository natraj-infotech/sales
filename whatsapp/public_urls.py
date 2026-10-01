"""Build public URLs for WhatsApp media (requires PUBLIC_BASE_URL in production)."""

from __future__ import annotations

from django.conf import settings


def public_media_url(relative_path: str) -> str:
    """Turn a FileField .url path into an absolute HTTPS URL for Meta document messages."""
    base = (getattr(settings, "PUBLIC_BASE_URL", "") or "").strip().rstrip("/")
    if not base:
        return ""
    path = (relative_path or "").strip()
    if not path:
        return ""
    if not path.startswith("/"):
        path = "/" + path
    return f"{base}{path}"
