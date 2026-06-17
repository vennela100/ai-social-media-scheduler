"""
Publishing orchestration (Phase 4).

`run()` is what the publish_due_posts management command (and thus the GitHub
Actions cron) calls. It:
  1. Recovers any posts stuck in PROCESSING from a previous crashed run.
  2. Finds pending posts whose time has come (respecting retry backoff).
  3. Atomically claims each, publishes it, and records the outcome.

Kept separate from the command so it can be unit-tested directly.
"""

import datetime as dt
import logging
import os
import re

from django.core.exceptions import ImproperlyConfigured
from django.utils import timezone

from . import instagram, linkedin, storage, youtube
from .models import ScheduledPost, Video
from .notifications import notify_failure

logger = logging.getLogger("scheduler")

# A post claimed (PROCESSING) but not finished within this window is assumed
# orphaned by a crashed run and is reset to PENDING.
STUCK_PROCESSING_MINUTES = 30

# Days to keep a fully-published video's source before archiving it (deleting the
# heavy Cloudinary file, keeping only a thumbnail). The window gives the user time
# to schedule the same video to more platforms before its source disappears.
SOURCE_RETENTION_DAYS = int(os.environ.get("SOURCE_RETENTION_DAYS", "7"))


# --- Per-platform publishers: each takes a post, returns the platform post id ---

def _caption_for(post: ScheduledPost) -> str:
    """A single caption blob for platforms that take one (Instagram, LinkedIn)."""
    if post.final_caption:
        return post.final_caption
    ai = post.ai_content
    if ai:
        parts = [p for p in (ai.generated_title, ai.generated_description, ai.generated_hashtags) if p]
        return "\n\n".join(parts)
    return post.video.original_filename or ""


def _publish_youtube(post: ScheduledPost) -> str:
    account = post.social_account
    ai = post.ai_content
    if ai and ai.generated_title:
        title = ai.generated_title
        description = post.final_caption or ai.generated_description
        # Tags may be comma- or space-separated, with or without '#'.
        tags = [t.strip().lstrip("#") for t in re.split(r"[,\n\s]+", ai.generated_hashtags or "") if t.strip().strip("#")]
    else:
        caption = post.final_caption or post.video.original_filename or "Untitled"
        title = caption.splitlines()[0][:100] if caption else "Untitled"
        description = caption
        tags = []
    return youtube.publish(
        account, video_url=post.video.file_url, title=title, description=description,
        tags=tags, privacy=post.visibility,
    )


def _publish_instagram(post: ScheduledPost) -> str:
    # Instagram has no private-publish option; reels go out public regardless.
    return instagram.publish(
        post.social_account, video_url=post.video.file_url, caption=_caption_for(post)
    )


def _publish_linkedin(post: ScheduledPost) -> str:
    return linkedin.publish(
        post.social_account, media_url=post.video.file_url, caption=_caption_for(post),
        visibility=post.visibility, media_type=post.video.media_type,
    )


PUBLISHERS = {
    "youtube": _publish_youtube,
    "instagram": _publish_instagram,
    "linkedin": _publish_linkedin,
}


def _retry_delay(post: ScheduledPost) -> int:
    """Backoff seconds before the next attempt, with a safe fallback."""
    try:
        return max(0, int(post.next_retry_delay_seconds()))
    except NotImplementedError:
        logger.warning(
            "next_retry_delay_seconds() not implemented — retrying next tick with "
            "no backoff. Implement it in core/models.py for proper backoff."
        )
        return 0


def _cooling_down(post: ScheduledPost, now) -> bool:
    """True if a previously-failed post is still inside its backoff window."""
    if post.retry_count <= 0:
        return False
    ready_at = post.updated_at + dt.timedelta(seconds=_retry_delay(post))
    return now < ready_at


def recover_stuck(now=None) -> int:
    """Reset posts orphaned in PROCESSING back to PENDING. Returns how many."""
    now = now or timezone.now()
    cutoff = now - dt.timedelta(minutes=STUCK_PROCESSING_MINUTES)
    stuck = ScheduledPost.objects.filter(
        status=ScheduledPost.Status.PROCESSING, updated_at__lt=cutoff
    )
    count = stuck.update(status=ScheduledPost.Status.PENDING)
    if count:
        logger.warning("Recovered %d post(s) stuck in PROCESSING", count)
    return count


def process_post(post: ScheduledPost) -> str:
    """Publish one already-claimed (PROCESSING) post; set + return its status."""
    Status = ScheduledPost.Status
    platform = post.social_account.platform
    publisher = PUBLISHERS.get(platform)

    if publisher is None:
        post.status = Status.FAILED
        post.last_error = f"No publisher registered for platform '{platform}'."
        post.save(update_fields=["status", "last_error", "updated_at"])
        logger.error("Post %s: %s", post.id, post.last_error)
        notify_failure(post)
        return post.status

    try:
        from google.auth.exceptions import RefreshError
    except Exception:  # pragma: no cover - google libs always present here
        RefreshError = ()

    try:
        platform_post_id = publisher(post)
    except (RefreshError, ImproperlyConfigured) as exc:
        # Auth is dead — no number of retries fixes this; ask the user to reconnect.
        post.status = Status.NEEDS_RECONNECT
        post.last_error = f"Authentication failed: {exc}"[:1000]
        post.save(update_fields=["status", "last_error", "updated_at"])
        logger.warning("Post %s needs reconnect: %s", post.id, exc)
        notify_failure(post)
        return post.status
    except Exception as exc:
        post.retry_count += 1
        post.last_error = str(exc)[:1000]
        if post.can_retry():
            post.status = Status.PENDING  # will retry after backoff
            logger.error(
                "Post %s failed (attempt %d/%d), will retry: %s",
                post.id, post.retry_count, post.MAX_RETRIES, exc,
            )
        else:
            post.status = Status.FAILED
            logger.error("Post %s failed permanently after %d attempts: %s",
                         post.id, post.retry_count, exc)
        post.save(update_fields=["status", "retry_count", "last_error", "updated_at"])
        if post.status == Status.FAILED:
            notify_failure(post)
        return post.status
    else:
        post.status = Status.PUBLISHED
        post.platform_post_id = platform_post_id
        post.last_error = ""
        post.save(update_fields=["status", "platform_post_id", "last_error", "updated_at"])
        logger.info("Post %s published to %s as %s", post.id, platform, platform_post_id)
        return post.status


def _archive_source(video: Video) -> bool:
    """Delete a fully-published video's heavy Cloudinary source, keeping a thumb.

    Preserves a small standalone thumbnail FIRST so the dashboard still shows a
    preview; only deletes the source if that succeeds (so we never strand a video
    with no image at all). Returns True if the source was archived.
    """
    if not video.cloudinary_public_id or video.source_deleted:
        return False

    thumb = storage.preserve_thumbnail(video.thumbnail_url)
    if thumb is None:
        logger.warning("Skipping archive of video %s: thumbnail preserve failed", video.pk)
        return False

    try:
        storage.delete_media(video.cloudinary_public_id, media_type=video.media_type)
    except Exception as exc:
        logger.warning("Archive of video %s failed deleting source: %s", video.pk, exc)
        return False

    video.thumbnail_url = thumb["url"]
    video.thumbnail_public_id = thumb["public_id"]
    video.source_deleted = True
    video.save(update_fields=["thumbnail_url", "thumbnail_public_id", "source_deleted"])
    logger.info("Archived source for video %s (kept thumbnail %s)", video.pk, thumb["public_id"])
    return True


def cleanup_published_sources(now=None, retention_days: int = SOURCE_RETENTION_DAYS) -> int:
    """Archive sources of videos whose posts are all published and old enough.

    Returns how many sources were archived. Safe to run every tick: it only
    touches videos that still have a source, are fully published, and were
    uploaded more than `retention_days` ago.
    """
    now = now or timezone.now()
    cutoff = now - dt.timedelta(days=retention_days)
    candidates = Video.objects.filter(
        source_deleted=False, uploaded_at__lt=cutoff
    ).exclude(cloudinary_public_id="")

    archived = 0
    for video in candidates:
        if video.is_fully_published() and _archive_source(video):
            archived += 1
    if archived:
        logger.info("Archived %d published video source(s)", archived)
    return archived


def run(now=None) -> dict:
    """Process all due posts. Returns a summary dict of outcomes."""
    now = now or timezone.now()
    recover_stuck(now)

    summary = {"claimed": 0, "published": 0, "failed": 0, "needs_reconnect": 0, "retrying": 0, "skipped_backoff": 0}

    due = ScheduledPost.objects.filter(
        status=ScheduledPost.Status.PENDING, scheduled_time_utc__lte=now
    ).select_related("social_account", "video", "ai_content")

    for post in due:
        if _cooling_down(post, now):
            summary["skipped_backoff"] += 1
            continue

        # Atomic claim: only one runner can flip PENDING -> PROCESSING.
        claimed = ScheduledPost.objects.filter(
            pk=post.pk, status=ScheduledPost.Status.PENDING
        ).update(status=ScheduledPost.Status.PROCESSING)
        if not claimed:
            continue
        summary["claimed"] += 1

        post.refresh_from_db()
        status = process_post(post)
        if status == ScheduledPost.Status.PUBLISHED:
            summary["published"] += 1
        elif status == ScheduledPost.Status.FAILED:
            summary["failed"] += 1
        elif status == ScheduledPost.Status.NEEDS_RECONNECT:
            summary["needs_reconnect"] += 1
        else:  # back to PENDING for a future retry
            summary["retrying"] += 1

    # Reclaim storage: archive sources of videos that are fully published + aged out.
    summary["archived"] = cleanup_published_sources(now)

    logger.info("publish_due_posts run complete: %s", summary)
    return summary
