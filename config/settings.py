"""
Django settings for the AI Social Media Scheduler.

Design goal: the SAME settings file must work in three contexts without edits —
  1. Local dev on this laptop  (SQLite, DEBUG on)
  2. Render web service        (Neon Postgres, DEBUG off)
  3. GitHub Actions scheduler  (Neon Postgres, headless, no web server)

Everything that differs between those contexts is read from environment
variables, never hardcoded. Locally those vars come from a .env file; in
production they come from Render env vars / GitHub Secrets.
"""

import os
from pathlib import Path

import dj_database_url
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

# Load .env into os.environ for local dev. In production the vars already
# exist in the real environment, so the missing-file case is a harmless no-op.
load_dotenv(BASE_DIR / ".env")


def env_bool(name: str, default: bool = False) -> bool:
    """Parse a truthy env var. '1', 'true', 'yes', 'on' (any case) -> True."""
    return os.environ.get(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


# --- Core security ---
# A dev fallback keeps `runserver` working before you set a real key, but it is
# obviously insecure — production MUST supply SECRET_KEY via the environment.
SECRET_KEY = os.environ.get(
    "DJANGO_SECRET_KEY",
    "django-insecure-dev-only-key-change-me-in-production",
)

DEBUG = env_bool("DJANGO_DEBUG", default=True)

# Comma-separated list, e.g. "myapp.onrender.com,localhost,127.0.0.1"
ALLOWED_HOSTS = [h.strip() for h in os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if h.strip()]

# Render injects the external hostname here; add it automatically.
RENDER_EXTERNAL_HOSTNAME = os.environ.get("RENDER_EXTERNAL_HOSTNAME")
if RENDER_EXTERNAL_HOSTNAME:
    ALLOWED_HOSTS.append(RENDER_EXTERNAL_HOSTNAME)

# Where the Next.js SPA is served. Used to (a) trust its origin for CSRF and
# (b) bounce OAuth callbacks back to it. The SPA proxies /api/* to Django, so
# the browser only ever talks to this origin.
FRONTEND_ORIGIN = os.environ.get("FRONTEND_ORIGIN", "http://localhost:3000")

# CSRF needs full origins (scheme + host) for any non-localhost form posts.
CSRF_TRUSTED_ORIGINS = [
    o.strip() for o in os.environ.get("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",") if o.strip()
]
# The SPA origin posts to /api/* through its proxy — trust it for CSRF.
CSRF_TRUSTED_ORIGINS += [FRONTEND_ORIGIN, "http://localhost:3000", "http://127.0.0.1:3000"]
CSRF_TRUSTED_ORIGINS = list(dict.fromkeys(CSRF_TRUSTED_ORIGINS))  # de-dup, keep order
if RENDER_EXTERNAL_HOSTNAME:
    CSRF_TRUSTED_ORIGINS.append(f"https://{RENDER_EXTERNAL_HOSTNAME}")

# When the app runs behind a TLS-terminating proxy (the cloudflared tunnel used
# for local OAuth testing, or Render in production), trust the forwarded scheme
# and host. Without this, request.build_absolute_uri() would emit
# http://localhost/... for OAuth redirect URIs instead of the real https://host.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
USE_X_FORWARDED_HOST = True


# --- Token encryption ---
# Fernet key used to encrypt OAuth tokens before they touch the database.
# Generated once with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# Kept out of code; loaded from the environment. Empty locally until you set it.
TOKEN_ENCRYPTION_KEY = os.environ.get("TOKEN_ENCRYPTION_KEY", "")


# --- External services ---
# Cloudinary (Phase 1): one URL holds cloud name + key + secret. The cloudinary
# SDK also auto-reads the CLOUDINARY_URL env var; we surface it here so view code
# can check "is upload configured?" without importing the SDK.
CLOUDINARY_URL = os.environ.get("CLOUDINARY_URL", "")

# Gemini (Phase 2): free key from https://aistudio.google.com/. Empty until set;
# core.ai.generate_metadata() raises a clear error rather than calling the SDK.
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

# Google / YouTube OAuth (Phase 3): from a Google Cloud project's OAuth client
# (type "Web application"). Empty until set; core.youtube gates on these.
GOOGLE_OAUTH_CLIENT_ID = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "")
GOOGLE_OAUTH_CLIENT_SECRET = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "")

# Instagram API with Instagram Login (Phase 5). These are the *Instagram* app
# id/secret from the Meta app's Instagram product ("API setup with Instagram
# login") — NOT the Facebook app id/secret. core.instagram gates on these.
INSTAGRAM_APP_ID = os.environ.get("INSTAGRAM_APP_ID", "")
INSTAGRAM_APP_SECRET = os.environ.get("INSTAGRAM_APP_SECRET", "")

# Legacy Facebook-app credentials (kept for reference; the Instagram integration
# now uses the Instagram-login flow above).
META_APP_ID = os.environ.get("META_APP_ID", "")
META_APP_SECRET = os.environ.get("META_APP_SECRET", "")

# LinkedIn OAuth (Phase 6): from a linkedin.com/developers app with the
# "Share on LinkedIn" + "Sign In with LinkedIn using OpenID Connect" products.
# Empty until set; core.linkedin gates on these.
LINKEDIN_CLIENT_ID = os.environ.get("LINKEDIN_CLIENT_ID", "")
LINKEDIN_CLIENT_SECRET = os.environ.get("LINKEDIN_CLIENT_SECRET", "")

# Telegram failure alerts (Phase 4). Empty until the user adds the secrets;
# notify_failure() no-ops cleanly while these are blank.
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# Shared secret guarding the /tasks/run-publisher/ endpoint, pinged by an
# external cron (cron-job.org) every minute so posts publish on time. Blank =
# endpoint always 403s (disabled).
CRON_KEY = os.environ.get("CRON_KEY", "")

# --- Email notifications ---
# SMTP credentials for outbound alert emails (publish success / failure /
# skipped / token-expiry). For Gmail, EMAIL_HOST_USER is your address and
# EMAIL_HOST_PASSWORD is a 16-char App Password (NOT your login password).
# Leaving EMAIL_HOST_USER blank makes the notifier no-op cleanly — same graceful
# degradation as the Telegram alerts above, so the publish job never crashes.
EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
EMAIL_HOST = os.environ.get("EMAIL_HOST", "smtp.gmail.com")
EMAIL_PORT = int(os.environ.get("EMAIL_PORT", "587"))
EMAIL_USE_TLS = env_bool("EMAIL_USE_TLS", default=True)
EMAIL_HOST_USER = os.environ.get("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.environ.get("EMAIL_HOST_PASSWORD", "")
# Where alerts are delivered. Falls back to the sender address if unset.
NOTIFY_EMAIL = os.environ.get("NOTIFY_EMAIL", "") or EMAIL_HOST_USER
# Absolute base URL used inside emails. The publish job (GitHub Actions) has no
# HTTP request to derive a URL from, so it reads this. e.g. https://app.onrender.com
DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "http://localhost:8000")


# --- Applications ---
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "core",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # WhiteNoise serves static files directly from the web process — no separate
    # CDN/host needed, which is what makes the $0 Render deploy viable.
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    # Activates the visitor's local timezone from the `tz` cookie so dates
    # render in their wall-clock time (storage stays UTC). See core/middleware.py.
    "core.middleware.TimezoneMiddleware",
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
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"


# --- Database ---
# If DATABASE_URL is set (Render / GitHub Actions / local-against-Neon), parse it.
# Otherwise fall back to SQLite so the project runs the instant you clone it.
DATABASE_URL = os.environ.get("DATABASE_URL")
if DATABASE_URL:
    DATABASES = {
        "default": dj_database_url.parse(
            DATABASE_URL,
            conn_max_age=600,        # reuse connections; Neon likes pooled conns
            ssl_require=True,        # Neon requires TLS
        )
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }


# --- Password validation ---
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]


# --- Internationalization / time ---
# We store everything in UTC (USE_TZ=True) and convert to the user's local time
# only at the display/input edges. This is the backbone of correct scheduling.
LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True


# --- Static files ---
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"   # collectstatic target for WhiteNoise
STATICFILES_DIRS = [BASE_DIR / "static"]  # project-level assets (app.css, etc.)
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# After login go straight into the app; after logout, back to the login page.
LOGIN_REDIRECT_URL = "core:dashboard"
LOGOUT_REDIRECT_URL = "login"


# --- Logging ---
# Observability requirement: every publish attempt is logged. A simple console
# logger is enough — Render captures stdout, and GitHub Actions shows it in the
# job log. We name a dedicated 'scheduler' logger the publish code will use.
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {"format": "{asctime} {levelname} {name} {message}", "style": "{"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "verbose"},
    },
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {
        "scheduler": {"handlers": ["console"], "level": "INFO", "propagate": False},
    },
}

# Local-only: also write logs to a file. A backgrounded runserver can buffer its
# stdout, so a per-record-flushed file is the reliable way to read errors during
# dev. In prod (DEBUG off) logs go to stdout, which Render / GitHub Actions capture.
if DEBUG:
    LOGGING["handlers"]["file"] = {
        "class": "logging.FileHandler",
        "filename": BASE_DIR / "debug.log",
        "formatter": "verbose",
    }
    LOGGING["root"]["handlers"].append("file")
    LOGGING["loggers"]["scheduler"]["handlers"].append("file")
