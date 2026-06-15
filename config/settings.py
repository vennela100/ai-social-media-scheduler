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

# CSRF needs full origins (scheme + host) for any non-localhost form posts.
CSRF_TRUSTED_ORIGINS = [
    o.strip() for o in os.environ.get("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",") if o.strip()
]
if RENDER_EXTERNAL_HOSTNAME:
    CSRF_TRUSTED_ORIGINS.append(f"https://{RENDER_EXTERNAL_HOSTNAME}")


# --- Token encryption ---
# Fernet key used to encrypt OAuth tokens before they touch the database.
# Generated once with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# Kept out of code; loaded from the environment. Empty locally until you set it.
TOKEN_ENCRYPTION_KEY = os.environ.get("TOKEN_ENCRYPTION_KEY", "")


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
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "/"


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
