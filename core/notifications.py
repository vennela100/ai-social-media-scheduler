"""
Failure notifications for the scheduler (Phase 4).

When a ScheduledPost exhausts its retries and is marked FAILED, we alert the
user. Telegram for now (free bot API); swap the body for email later if needed.

Design note: this runs inside the GitHub Actions publish job. A failure to SEND
the alert must never crash that job or mask the original publish error — so the
network call is wrapped and the function degrades to a logged warning.
"""

import logging

import requests
from django.conf import settings

logger = logging.getLogger("scheduler")


def _send(text: str, *, context: str) -> bool:
    """Send a Telegram message. No-op (logged) if unconfigured; never raises.

    Returns True only if the send actually went out — callers use that to decide
    whether to record that a reminder was delivered.
    """
    if not settings.TELEGRAM_BOT_TOKEN or not settings.TELEGRAM_CHAT_ID:
        logger.warning("%s: Telegram not configured; skipping alert", context)
        return False
    try:
        requests.post(
            f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage",
            data={"chat_id": settings.TELEGRAM_CHAT_ID, "text": text},
            timeout=10,  # never hang the caller on a slow Telegram API
        )
        return True
    except requests.RequestException as exc:
        logger.error("%s: could not send Telegram alert: %s", context, exc)
        return False


def notify_failure(post) -> None:
    """Alert the user when a ScheduledPost exhausts its retries."""
    message = f"Post {post.id} ({post.social_account.platform}) failed: {post.last_error}"
    _send(message, context=f"notify_failure[post {post.id}]")


def notify_token_expiry(account, dashboard_url: str) -> bool:
    """Warn the user a platform token is expiring/expired. Returns True if sent."""
    days = account.days_until_expiry()
    status_line = "Already expired" if account.is_expired() else f"Expires in {days} days"
    message = (
        "🔔 Token expiry warning\n\n"
        f"Platform: {account.get_platform_display()}\n"
        f"Status: {status_line}\n"
        f"Action needed: Log in and reconnect at {dashboard_url}\n\n"
        "Your scheduled posts will pause if not reconnected."
    )
    return _send(message, context=f"notify_token_expiry[{account.platform}]")
