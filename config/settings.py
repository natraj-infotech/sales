"""
Django settings for client subscription starter project.
"""

import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
# interpolate=False so passwords with $ (e.g. $Nat$7508) are not rewritten.
load_dotenv(BASE_DIR / ".env", interpolate=False)

SECRET_KEY = os.environ.get(
    "DJANGO_SECRET_KEY", "django-insecure-dev-only-change-in-production"
)
DEBUG = os.environ.get("DJANGO_DEBUG", "True").lower() in ("1", "true", "yes")

ALLOWED_HOSTS = [
    h.strip()
    for h in os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")
    if h.strip()
]

# HTTP only for loopback. Any public host (sslip.io, real domain, etc.) uses HTTPS
# so OAuth callback URLs match Azure/Google (AADSTS50011 is http vs https).
LOCAL_HOSTNAMES = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})
_LOCAL_HOSTNAMES = LOCAL_HOSTNAMES


def _hostname_only(host: str) -> str:
    host = host.strip().lower()
    if host.startswith("[") and "]" in host:
        return host[1:].split("]", 1)[0]
    return host.split(":", 1)[0]


def _is_local_host(host: str) -> bool:
    return _hostname_only(host) in _LOCAL_HOSTNAMES


_use_https = any(not _is_local_host(h) for h in ALLOWED_HOSTS if h and h != "*")

# Reverse proxy (nginx / sslip.io / load balancer) terminates TLS and forwards
# HTTP to Django. Trust X-Forwarded-Proto so request.is_secure() is correct.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
USE_X_FORWARDED_HOST = True
SESSION_COOKIE_SECURE = _use_https
CSRF_COOKIE_SECURE = _use_https

_csrf_env = [
    origin.strip().rstrip("/")
    for origin in os.environ.get("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",")
    if origin.strip()
]
if _csrf_env:
    CSRF_TRUSTED_ORIGINS = _csrf_env
else:
    CSRF_TRUSTED_ORIGINS = []
    _seen_origins: set[str] = set()
    for _host in ALLOWED_HOSTS:
        if not _host or _host == "*":
            continue
        if _host.startswith("."):
            _origin = f"https://*{_host}"
        else:
            _scheme = "http" if _is_local_host(_host) else "https"
            _origin = f"{_scheme}://{_host}"
        if _origin not in _seen_origins:
            _seen_origins.add(_origin)
            CSRF_TRUSTED_ORIGINS.append(_origin)
        if _is_local_host(_host) and ":" not in _host:
            _origin_port = f"http://{_host}:8000"
            if _origin_port not in _seen_origins:
                _seen_origins.add(_origin_port)
                CSRF_TRUSTED_ORIGINS.append(_origin_port)

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.sites",
    "django.contrib.humanize",
    "allauth",
    "allauth.account",
    "allauth.socialaccount",
    "allauth.socialaccount.providers.google",
    "allauth.socialaccount.providers.microsoft",
    "rest_framework",
    "django_cleanup.apps.CleanupConfig",
    "accounts",
    "business.apps.BusinessConfig",
    "whatsapp.apps.WhatsappConfig",
    "helpdesk",
]

MIDDLEWARE = [
    "accounts.middleware.PreferHttpsExceptLocalhostMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "allauth.account.middleware.AccountMiddleware",
    "accounts.middleware.RequireCompanyProfileMiddleware",
    "accounts.middleware.ExceptionLoggingMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "accounts.context_processors.oauth_availability",
                "accounts.context_processors.company_access",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

# PostgreSQL only (SQLite fallback removed — set POSTGRES_* in .env).
_postgres_db = os.environ.get("POSTGRES_DB", "").strip()
if not _postgres_db:
    raise ImproperlyConfigured(
        "POSTGRES_DB is required. Configure PostgreSQL in .env "
        "(POSTGRES_DB, POSTGRES_USER, POSTGRES_PASSWORD, POSTGRES_HOST, POSTGRES_PORT)."
    )

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": _postgres_db,
        "USER": os.environ.get("POSTGRES_USER", "postgres"),
        "PASSWORD": os.environ.get("POSTGRES_PASSWORD", ""),
        "HOST": os.environ.get("POSTGRES_HOST", "localhost"),
        "PORT": os.environ.get("POSTGRES_PORT", "5432"),
        "CONN_MAX_AGE": int(os.environ.get("POSTGRES_CONN_MAX_AGE", "0")),
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator",
    },
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "Asia/Kolkata"
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

AUTH_USER_MODEL = "accounts.User"
SITE_ID = 1

AUTHENTICATION_BACKENDS = (
    "django.contrib.auth.backends.ModelBackend",
    "allauth.account.auth_backends.AuthenticationBackend",
)

LOGIN_REDIRECT_URL = "/dashboard/"
LOGOUT_REDIRECT_URL = "/accounts/login/"

ACCOUNT_LOGIN_METHODS = {"email"}
ACCOUNT_SIGNUP_FIELDS = ["email*", "password1*", "password2*"]
ACCOUNT_USER_MODEL_USERNAME_FIELD = None
ACCOUNT_EMAIL_VERIFICATION = "none"
ACCOUNT_ADAPTER = "accounts.adapters.AccountAdapter"
# django-allauth default is "http", which made Microsoft receive
# http://sales....sslip.io/accounts/microsoft/login/callback/.
ACCOUNT_DEFAULT_HTTP_PROTOCOL = "https" if _use_https else "http"

SOCIALACCOUNT_AUTO_SIGNUP = True
SOCIALACCOUNT_EMAIL_AUTHENTICATION = True
SOCIALACCOUNT_EMAIL_AUTHENTICATION_AUTO_CONNECT = True
SOCIALACCOUNT_ADAPTER = "accounts.adapters.SocialAccountAdapter"
SOCIALACCOUNT_LOGIN_ON_GET = True

_google_cid = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "").strip()
_google_sec = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "").strip()
_ms_cid = os.environ.get("MICROSOFT_OAUTH_CLIENT_ID", "").strip()
_ms_sec = os.environ.get("MICROSOFT_OAUTH_CLIENT_SECRET", "").strip()
_ms_tenant = os.environ.get("MICROSOFT_OAUTH_TENANT", "organizations").strip()

SOCIALACCOUNT_PROVIDERS = {
    "google": {
        "APP": {
            "client_id": _google_cid or "unset",
            "secret": _google_sec or "unset",
            "key": "",
        },
        "SCOPE": ["profile", "email"],
        "AUTH_PARAMS": {"access_type": "online"},
    },
    "microsoft": {
        "APP": {
            "client_id": _ms_cid or "unset",
            "secret": _ms_sec or "unset",
        },
        "TENANT": _ms_tenant or "organizations",
        "AUTH_PARAMS": {"prompt": "select_account"},
    },
}

EMAIL_BACKEND = "business.helpdesk_smtp_backend.CompanySMTPEmailBackend"
DEFAULT_FROM_EMAIL = os.environ.get("DEFAULT_FROM_EMAIL", "noreply@localhost").strip()

# Outbound SMTP override (optional — auto-detected from mailbox domain/MX when unset)
OUTBOUND_SMTP_HOST = os.environ.get("OUTBOUND_SMTP_HOST", "").strip()
OUTBOUND_SMTP_PORT = int(os.environ.get("OUTBOUND_SMTP_PORT", "587") or 587)

# Inbound support mail (IMAP) — used by `manage.py fetch_support_inbox`
SUPPORT_IMAP_HOST = os.environ.get("SUPPORT_IMAP_HOST", "imap.gmail.com")
SUPPORT_IMAP_PORT = int(os.environ.get("SUPPORT_IMAP_PORT", "993"))
SUPPORT_IMAP_MAILBOX = os.environ.get("SUPPORT_IMAP_MAILBOX", "INBOX")
SUPPORT_IMAP_MARK_SEEN = os.environ.get("SUPPORT_IMAP_MARK_SEEN", "true").lower() in (
    "1",
    "true",
    "yes",
)

# Microsoft Graph (OAuth client credentials) used for M365 support inbox reading.
MICROSOFT_GRAPH_CLIENT_ID = os.environ.get("MICROSOFT_GRAPH_CLIENT_ID", _ms_cid).strip()
MICROSOFT_GRAPH_CLIENT_SECRET = os.environ.get(
    "MICROSOFT_GRAPH_CLIENT_SECRET", _ms_sec
).strip()
MICROSOFT_GRAPH_TENANT = os.environ.get(
    "MICROSOFT_GRAPH_TENANT", _ms_tenant or "organizations"
).strip()

# Celery (background worker + scheduler)
CELERY_BROKER_URL = os.environ.get("CELERY_BROKER_URL", "filesystem://")
CELERY_RESULT_BACKEND = os.environ.get(
    "CELERY_RESULT_BACKEND", "cache+memory://"
)
CELERY_TIMEZONE = TIME_ZONE
CELERY_TASK_TRACK_STARTED = True
CELERY_TASK_TIME_LIMIT = 300
if CELERY_BROKER_URL.startswith("filesystem://"):
    _celery_queue_in = str(BASE_DIR / ".celery" / "out")
    _celery_queue_out = str(BASE_DIR / ".celery" / "out")
    _celery_queue_processed = str(BASE_DIR / ".celery" / "processed")
    os.makedirs(_celery_queue_in, exist_ok=True)
    os.makedirs(_celery_queue_out, exist_ok=True)
    os.makedirs(_celery_queue_processed, exist_ok=True)
    CELERY_BROKER_TRANSPORT_OPTIONS = {
        "data_folder_in": _celery_queue_in,
        "data_folder_out": _celery_queue_out,
        "data_folder_processed": _celery_queue_processed,
    }
# Public links in outbound Case lock emails (set in production, e.g. https://your-domain.com)
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").strip()

# Zoho CRM (OAuth for Quotes / Accounts). DC: in | com | eu | com.au | jp
ZOHO_CLIENT_ID = os.environ.get("ZOHO_CLIENT_ID", "").strip()
ZOHO_CLIENT_SECRET = os.environ.get("ZOHO_CLIENT_SECRET", "").strip()
ZOHO_DC = (os.environ.get("ZOHO_DC", "in") or "in").strip().lower()
ZOHO_REDIRECT_URI = os.environ.get(
    "ZOHO_REDIRECT_URI",
    "",
).strip()

# WhatsApp Cloud API (shared infrastructure — leads, future features)
WHATSAPP_VERIFY_TOKEN = os.environ.get("WHATSAPP_VERIFY_TOKEN", "").strip()
WHATSAPP_ACCESS_TOKEN = os.environ.get("WHATSAPP_ACCESS_TOKEN", "").strip()
WHATSAPP_API_VERSION = os.environ.get("WHATSAPP_API_VERSION", "v26.0").strip() or "v26.0"
WHATSAPP_WABA_ID = os.environ.get("WHATSAPP_WABA_ID", "").strip()
WHATSAPP_META_APP_ID = os.environ.get("WHATSAPP_META_APP_ID", "").strip()
WHATSAPP_DEFAULT_PHONE_NUMBER_ID = os.environ.get(
    "WHATSAPP_DEFAULT_PHONE_NUMBER_ID", ""
).strip()
WHATSAPP_DEFAULT_COMPANY_ID = os.environ.get("WHATSAPP_DEFAULT_COMPANY_ID", "").strip()
WHATSAPP_DEFAULT_FLOW_HANDLER = (
    os.environ.get("WHATSAPP_DEFAULT_FLOW_HANDLER", "lead") or "lead"
).strip()
# PEM contents (use \n for newlines) or path to PEM file for Flow encryption
WHATSAPP_FLOW_PRIVATE_KEY_PEM = os.environ.get("WHATSAPP_FLOW_PRIVATE_KEY_PEM", "").strip()
WHATSAPP_FLOW_PRIVATE_KEY_PATH = os.environ.get(
    "WHATSAPP_FLOW_PRIVATE_KEY_PATH", ""
).strip()
WHATSAPP_FLOW_PRIVATE_KEY_PASSWORD = os.environ.get(
    "WHATSAPP_FLOW_PRIVATE_KEY_PASSWORD", ""
).strip() or None
# Welcome/menu on new WhatsApp conversation (inactivity window in hours)
WHATSAPP_WELCOME_INACTIVITY_HOURS = float(
    os.environ.get("WHATSAPP_WELCOME_INACTIVITY_HOURS", "24") or 24
)
WHATSAPP_WELCOME_MODE = (
    os.environ.get("WHATSAPP_WELCOME_MODE", "auto") or "auto"
).strip().lower()
WHATSAPP_WELCOME_BODY = os.environ.get(
    "WHATSAPP_WELCOME_BODY",
    "Welcome to Tracker365. How can we help you today?",
).strip()
WHATSAPP_WELCOME_HEADER = os.environ.get("WHATSAPP_WELCOME_HEADER", "").strip()
WHATSAPP_WELCOME_FOOTER = os.environ.get("WHATSAPP_WELCOME_FOOTER", "").strip()
# JSON array or "id:Title,id2:Title2"
WHATSAPP_WELCOME_BUTTONS = os.environ.get(
    "WHATSAPP_WELCOME_BUTTONS",
    "lead:New enquiry,support:Support",
).strip()
WHATSAPP_WELCOME_FLOW_ID = os.environ.get("WHATSAPP_WELCOME_FLOW_ID", "").strip()
WHATSAPP_WELCOME_FLOW_CTA = os.environ.get(
    "WHATSAPP_WELCOME_FLOW_CTA", "Open form"
).strip()

# Combined backups (PostgreSQL + MEDIA_ROOT)
BACKUP_ENABLED = os.environ.get("BACKUP_ENABLED", "true").lower() in (
    "1",
    "true",
    "yes",
)
BACKUP_DIR = os.environ.get("BACKUP_DIR", "").strip() or str(BASE_DIR / "backups")
BACKUP_RETENTION_DAYS = int(os.environ.get("BACKUP_RETENTION_DAYS", "14") or 14)
BACKUP_PG_DUMP = os.environ.get("BACKUP_PG_DUMP", "").strip()
BACKUP_PG_RESTORE = os.environ.get("BACKUP_PG_RESTORE", "").strip()
BACKUP_PG_DUMP_TIMEOUT = int(os.environ.get("BACKUP_PG_DUMP_TIMEOUT", "3600") or 3600)
# Daily schedule hour/minute in CELERY_TIMEZONE (Asia/Kolkata)
BACKUP_SCHEDULE_HOUR = int(os.environ.get("BACKUP_SCHEDULE_HOUR", "2") or 2)
BACKUP_SCHEDULE_MINUTE = int(os.environ.get("BACKUP_SCHEDULE_MINUTE", "0") or 0)

from celery.schedules import crontab  # noqa: E402

CELERY_BEAT_SCHEDULE = {
    "fetch-helpdesk-email-every-60-seconds": {
        "task": "business.tasks.fetch_helpdesk_email_task",
        "schedule": 60.0,
    },
    "fetch-caselock-inbox-every-15-seconds": {
        "task": "business.tasks.fetch_caselock_inbox_task",
        "schedule": 15.0,
    },
    "send-expiry-notifications-daytime": {
        "task": "business.tasks.send_expiry_notifications_task",
        "schedule": 60.0,
    },
    "poll-renewal-read-receipts-every-2-minutes": {
        "task": "business.tasks.poll_renewal_read_receipts_task",
        "schedule": 120.0,
    },
    "create-sales-backup-daily": {
        "task": "business.tasks.create_backup_task",
        "schedule": crontab(
            hour=BACKUP_SCHEDULE_HOUR,
            minute=BACKUP_SCHEDULE_MINUTE,
        ),
    },
}

# ---------------------------------------------------------------------------
# django-helpdesk — Ticket360 UI stays primary; helpdesk is the ticket engine
# ---------------------------------------------------------------------------
HELPDESK_TEAMS_MODE_ENABLED = False
HELPDESK_ALLOW_NON_STAFF_TICKET_UPDATE = True
HELPDESK_CREATE_TICKET_HIDE_ASSIGNED_TO = True
HELPDESK_PRIVATE_FOLLOWUP_MEANS_NO_EMAILS = True

# Keep UI status labels: Open / In progress / Resolved / Closed
HELPDESK_TICKET_OPEN_STATUS = 1
HELPDESK_TICKET_REOPENED_STATUS = 2  # shown as "In progress"
HELPDESK_TICKET_RESOLVED_STATUS = 3
HELPDESK_TICKET_CLOSED_STATUS = 4
HELPDESK_TICKET_DUPLICATE_STATUS = 5
HELPDESK_TICKET_STATUS_CHOICES = (
    (1, "Open"),
    (2, "In progress"),
    (3, "Resolved"),
    (4, "Closed"),
)
HELPDESK_TICKET_OPEN_STATUSES = (1, 2)
HELPDESK_TICKET_STATUS_CHOICES_FLOW = {
    1: (1, 2, 3, 4),
    2: (1, 2, 3, 4),
    3: (1, 2, 3, 4),
    4: (1, 2, 4),
}


def _redirect_helpdesk_builtin_ui(request):
    """Hide helpdesk's built-in screens; keep Ticket360 URLs only."""
    from django.shortcuts import redirect
    import re

    match = re.search(r"/tickets/(\d+)/", request.path or "")
    if match:
        return redirect("support_ticket_detail", pk=int(match.group(1)))
    return redirect("support_ticket_list")


HELPDESK_STAFF_VIEW_PROTECTOR = _redirect_helpdesk_builtin_ui
HELPDESK_PUBLIC_VIEW_PROTECTOR = _redirect_helpdesk_builtin_ui

LOG_DIR = BASE_DIR / "logs"
os.makedirs(LOG_DIR, exist_ok=True)

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": (
                "{asctime} {levelname} {name} {module}:{funcName}:{lineno} {message}"
            ),
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "verbose",
        },
        "file": {
            "class": "logging.handlers.RotatingFileHandler",
            "filename": str(LOG_DIR / "sales.log"),
            "maxBytes": 5 * 1024 * 1024,
            "backupCount": 5,
            "encoding": "utf-8",
            "formatter": "verbose",
        },
    },
    "root": {
        "handlers": ["console", "file"],
        "level": "INFO",
    },
    "loggers": {
        "django.request": {
            "handlers": ["console", "file"],
            "level": "ERROR",
            "propagate": False,
        },
        "accounts": {
            "handlers": ["console", "file"],
            "level": "INFO",
            "propagate": False,
        },
        "business": {
            "handlers": ["console", "file"],
            "level": "INFO",
            "propagate": False,
        },
        "whatsapp": {
            "handlers": ["console", "file"],
            "level": "INFO",
            "propagate": False,
        },
    },
}
