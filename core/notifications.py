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


def notify_failure(post) -> None:
    """Alert the user when a ScheduledPost exhausts its retries."""
    # No-op cleanly until the user adds the Telegram secrets.
    if not settings.TELEGRAM_BOT_TOKEN or not settings.TELEGRAM_CHAT_ID:
        logger.warning(
            "notify_failure: Telegram not configured; skipping alert for post %s",
            post.id,
        )
        return

    message = f"Post {post.id} ({post.social_account.platform}) failed: {post.last_error}"
    try:
        requests.post(
            f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage",
            data={"chat_id": settings.TELEGRAM_CHAT_ID, "text": message},
            timeout=10,  # never hang the publish job on a slow Telegram API
        )
    except requests.RequestException as exc:
        # Swallow + log: the post is already FAILED; a dropped alert is not fatal.
        logger.error("notify_failure: could not send alert for post %s: %s", post.id, exc)
