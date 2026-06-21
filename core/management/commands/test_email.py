"""
Send one of each notification email using dummy data.

Use this to confirm your SMTP credentials work BEFORE relying on them for real
publish alerts:

    python manage.py test_email

It builds throwaway stand-in objects (no database rows are created or touched)
that look enough like a ScheduledPost / SocialAccount for each notifier, then
sends: success, failure, skipped, and a token-expiry reminder.
"""

import datetime as dt
from types import SimpleNamespace

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from core import notifications


def _dummy_post(platform_value: str, platform_label: str, *, retry_count: int = 0,
                last_error: str = "") -> SimpleNamespace:
    """A stand-in ScheduledPost exposing only what the notifiers read."""
    account = SimpleNamespace(
        platform=platform_value,
        get_platform_display=lambda: platform_label,
    )
    video = SimpleNamespace(user_title="Demo post — SMTP test", original_filename="demo.mp4")
    return SimpleNamespace(
        id=0,
        social_account=account,
        video=video,
        scheduled_time_utc=timezone.now(),
        visibility="public",
        get_visibility_display=lambda: "Public",
        retry_count=retry_count,
        last_error=last_error,
        MAX_RETRIES=3,
    )


def _dummy_account() -> SimpleNamespace:
    """A stand-in SocialAccount for the expiry reminder (2 days left)."""
    return SimpleNamespace(
        platform="linkedin",
        get_platform_display=lambda: "LinkedIn",
        token_expires_at=timezone.now() + dt.timedelta(days=2),
        days_until_expiry=lambda: 2,
        is_expired=lambda: False,
    )


class Command(BaseCommand):
    help = "Send a test email of each type (success/failure/skipped/expiry) using dummy data."

    def handle(self, *args, **options):
        if not settings.EMAIL_HOST_USER or not settings.NOTIFY_EMAIL:
            self.stderr.write(self.style.ERROR(
                "Email not configured: set EMAIL_HOST_USER and EMAIL_HOST_PASSWORD "
                "(and optionally NOTIFY_EMAIL) in your .env, then retry."
            ))
            return

        checks = [
            ("success", lambda: notifications.notify_success(
                _dummy_post("youtube", "YouTube"))),
            ("failure", lambda: notifications.notify_failure(
                _dummy_post("instagram", "Instagram", retry_count=3,
                            last_error="HTTP 400: media processing failed"))),
            ("skipped", lambda: notifications.notify_skipped(
                _dummy_post("linkedin", "LinkedIn"))),
            ("token-expiry", lambda: notifications.notify_token_expiry(
                _dummy_account(), settings.DASHBOARD_URL)),
        ]

        sent = 0
        for name, fn in checks:
            try:
                ok = fn()
            except Exception as exc:  # surface SMTP/auth errors clearly
                self.stderr.write(self.style.ERROR(f"  x {name}: {exc}"))
                continue
            if ok:
                sent += 1
                self.stdout.write(f"  ✓ {name} email sent")
            else:
                self.stderr.write(self.style.ERROR(
                    f"  ✗ {name} not sent (see log — likely an SMTP error)"))

        if sent == len(checks):
            self.stdout.write(self.style.SUCCESS(
                f"✅ Test emails sent — check {settings.NOTIFY_EMAIL}"))
        else:
            self.stderr.write(self.style.ERROR(
                f"Only {sent}/{len(checks)} emails sent. Check the SMTP error above."))
