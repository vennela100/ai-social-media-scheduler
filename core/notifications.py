"""
Outcome notifications for the scheduler.

Every scheduled post produces exactly ONE notification email, matched to its
terminal outcome:

    PUBLISHED        -> notify_success   ("your post is live")
    FAILED (retries  -> notify_failure   ("failed after 3 attempts")
      exhausted)
    NEEDS_RECONNECT  -> notify_skipped   ("skipped — token expired, reconnect")

Token-expiry *reminders* (sent from the dashboard before a post is even due)
use notify_token_expiry, throttled to once per 24h per account by the caller.

Design note: these run inside the GitHub Actions publish job. Failing to SEND an
email must never crash that job or mask the original publish result — so the SMTP
call is wrapped. But per the "no silent failures" rule we do NOT use
fail_silently=True (which swallows errors invisibly): we let send_mail raise,
catch it, and log it loudly. The job survives; the failure is on the record.
"""

import logging

from django.conf import settings
from django.core.mail import send_mail
from django.utils import timezone

logger = logging.getLogger("scheduler")

# A horizontal rule reused in every email body for a consistent, scannable look.
_RULE = "━━━━━━━━━━━━━━━━━━━━━━━━"


def _send_email(subject: str, body: str, *, context: str) -> bool:
    """Send one alert email. No-op (logged) if unconfigured; never raises.

    Returns True only if the message actually went out — callers use that to
    decide whether to record that a reminder was delivered.
    """
    sender = settings.EMAIL_HOST_USER
    recipient = settings.NOTIFY_EMAIL
    if not sender or not recipient:
        logger.warning("%s: email not configured; skipping alert", context)
        return False
    try:
        # fail_silently=False on purpose: we want the exception so we can LOG it.
        send_mail(subject, body, sender, [recipient], fail_silently=False)
    except Exception as exc:  # SMTP / auth / network — log, but don't crash the job
        logger.error("%s: could not send email: %s", context, exc)
        return False
    logger.info("%s: email sent to %s", context, recipient)
    return True


def _platform_name(obj) -> str:
    """Human platform label for either a ScheduledPost or a SocialAccount."""
    account = getattr(obj, "social_account", obj)
    return account.get_platform_display()


def _post_title(post) -> str:
    """Best human title for a post: the user's title, else the filename, else id."""
    video = post.video
    return video.user_title or video.original_filename or f"Post {post.id}"


def notify_success(post) -> bool:
    """Tell the user a ScheduledPost published successfully. Returns True if sent."""
    platform = _platform_name(post)
    published = timezone.now().strftime("%d %b %Y at %H:%M")
    body = (
        "Your scheduled post just went live!\n\n"
        f"{_RULE}\n"
        f"Platform   : {platform}\n"
        f"Title      : {_post_title(post)}\n"
        f"Published  : {published} UTC\n"
        f"Visibility : {post.get_visibility_display()}\n"
        f"{_RULE}\n\n"
        f"View all your posts:\n{settings.DASHBOARD_URL}"
    )
    return _send_email(
        f"✅ Your {platform} post is live!", body,
        context=f"notify_success[post {post.id}]",
    )


def notify_failure(post) -> bool:
    """Alert the user a post failed permanently (retries exhausted). True if sent."""
    platform = _platform_name(post)
    body = (
        f"Your scheduled post could not be published after {post.MAX_RETRIES} attempts.\n\n"
        f"{_RULE}\n"
        f"Platform   : {platform}\n"
        f"Title      : {_post_title(post)}\n"
        f"Scheduled  : {post.scheduled_time_utc.strftime('%d %b %Y at %H:%M')} UTC\n"
        f"Error      : {post.last_error or 'Unknown error'}\n"
        f"Retries    : {post.retry_count} of {post.MAX_RETRIES}\n"
        f"{_RULE}\n\n"
        "What to do next:\n"
        f"1. Check your {platform} account is still connected\n"
        "2. Reschedule the post from your dashboard\n\n"
        f"Go to dashboard:\n{settings.DASHBOARD_URL}"
    )
    return _send_email(
        f"❌ Your {platform} post failed to publish", body,
        context=f"notify_failure[post {post.id}]",
    )


def notify_skipped(post) -> bool:
    """Alert the user a post was skipped because the platform token is dead.

    This is NOT a retryable failure — the post will not publish until the user
    reconnects, so the email tells them exactly that.
    """
    platform = _platform_name(post)
    body = (
        f"Your scheduled post was skipped because your {platform} token has expired.\n\n"
        f"{_RULE}\n"
        f"Platform   : {platform}\n"
        f"Title      : {_post_title(post)}\n"
        f"Scheduled  : {post.scheduled_time_utc.strftime('%d %b %Y at %H:%M')} UTC\n"
        f"Reason     : {platform} token expired\n"
        f"{_RULE}\n\n"
        "This post has NOT published and will NOT retry automatically.\n\n"
        "Fix in 2 steps:\n"
        f"1. Reconnect your {platform} account, then\n"
        "2. Reschedule the post\n\n"
        f"Dashboard:\n{settings.DASHBOARD_URL}"
    )
    return _send_email(
        f"⚠️ Your {platform} post was skipped — reconnect needed", body,
        context=f"notify_skipped[post {post.id}]",
    )


def notify_token_expiry(account, dashboard_url: str) -> bool:
    """Warn the user a platform token is expiring/expired. Returns True if sent.

    `dashboard_url` is passed in by the dashboard view (built from the live
    request); the publish job doesn't call this — it uses notify_skipped instead.
    """
    days = account.days_until_expiry()
    expired = account.is_expired()
    platform = account.get_platform_display()

    if expired:
        subject = f"❌ {platform} token expired — posts are paused"
        urgency = "Your token has already expired. Posts are paused right now."
        action = "Reconnect immediately"
    elif days is not None and days <= 3:
        subject = f"🚨 {platform} token expires in {days} days"
        urgency = f"Critical — only {days} days left before posts stop."
        action = "Reconnect now"
    else:
        days_text = f"in {days} days" if days is not None else "soon"
        subject = f"⚠️ {platform} token expires {days_text}"
        urgency = f"Your {platform} token expires {days_text}."
        action = "Reconnect soon"

    expires = (
        account.token_expires_at.strftime("%d %b %Y")
        if account.token_expires_at else "unknown"
    )
    body = (
        f"{urgency}\n\n"
        f"{_RULE}\n"
        f"Platform   : {platform}\n"
        f"Expires    : {expires}\n"
        f"Days left  : {max(days, 0) if days is not None else '?'}\n"
        f"{_RULE}\n\n"
        f"{action}:\n{dashboard_url}"
    )
    return _send_email(
        subject, body, context=f"notify_token_expiry[{account.platform}]",
    )
