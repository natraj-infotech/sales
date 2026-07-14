"""
Django settings for client subscription starter project.
"""

import os
from pathlib import Path

from celery.schedules import crontab
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

SECRET_KEY = os.environ.get(
    "DJANGO_SECRET_KEY", "django-insecure-dev-only-change-in-production"
)
DEBUG = os.environ.get("DJANGO_DEBUG", "True").lower() in ("1", "true", "yes")

ALLOWED_HOSTS = [
    h.strip()
    for h in os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")
    if h.strip()
]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.sites",
    "allauth",
    "allauth.account",
    "allauth.socialaccount",
    "allauth.socialaccount.providers.google",
    "allauth.socialaccount.providers.microsoft",
    "accounts",
    "business",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "allauth.account.middleware.AccountMiddleware",
    "accounts.middleware.RequireCompanyProfileMiddleware",
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

# PostgreSQL when POSTGRES_DB is set; otherwise SQLite. Set USE_SQLITE=1 to force SQLite.
_use_sqlite = os.environ.get("USE_SQLITE", "").lower() in ("1", "true", "yes")
_postgres_db = os.environ.get("POSTGRES_DB", "").strip()

if _use_sqlite or not _postgres_db:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }
else:
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

STATIC_URL = "static/"

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

EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

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
CELERY_TASK_TIME_LIMIT = 120
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

CELERY_BEAT_SCHEDULE = {
    "fetch-support-inbox-every-10-seconds": {
        "task": "business.tasks.fetch_support_inbox_task",
        "schedule": 10.0,
    },
    "fetch-caselock-inbox-every-15-seconds": {
        "task": "business.tasks.fetch_caselock_inbox_task",
        "schedule": 15.0,
    },
    "send-expiry-notifications-daily": {
        "task": "business.tasks.send_expiry_notifications_task",
        "schedule": crontab(hour=11, minute=0),
    },
    "send-expiry-notifications-evening": {
        "task": "business.tasks.send_expiry_notifications_task",
        "schedule": crontab(hour=18, minute=0),
    },
}
