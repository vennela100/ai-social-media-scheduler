"""
Post-publish engagement analytics.

After a ScheduledPost goes live, each platform can report back views / likes /
comments. This module is the thin dispatch layer between a post and the right
platform's `fetch_stats()`:

    refresh_post(post)        -> update one post's stored stats
    refresh_for_user(user)    -> refresh all of a user's published posts

It's deliberately lazy and fail-open: platform fetchers never raise (they return
None), stats are only re-fetched once they're STATS_STALE_AFTER old (so repeated
dashboard clicks are cheap), and a None result leaves the previous numbers in
place. The management command `refresh_stats` calls refresh_all() on a cron.
"""

import datetime as dt
import logging

from django.utils import timezone

from . import instagram, linkedin, youtube
from .models import ScheduledPost

logger = logging.getLogger("scheduler")

# platform -> its fetch_stats(account, platform_post_id) callable.
PROVIDERS = {
    "youtube": youtube.fetch_stats,
    "instagram": instagram.fetch_stats,
    "linkedin": linkedin.fetch_stats,
}

# Don't re-hit a platform API for a post refreshed more recently than this.
# Engagement doesn't change second-to-second, and platforms rate-limit reads.
STATS_STALE_AFTER = dt.timedelta(minutes=30)


def _is_stale(post: ScheduledPost, now) -> bool:
    return post.stats_updated_at is None or (now - post.stats_updated_at) >= STATS_STALE_AFTER


def refresh_post(post: ScheduledPost, *, force: bool = False, now=None) -> bool:
    """Fetch and store fresh stats for one published post. Returns True if updated.

    No-ops (returns False) for posts that aren't published, have no platform id,
    have no provider, or were refreshed too recently (unless force=True).
    """
    now = now or timezone.now()
    if post.status != ScheduledPost.Status.PUBLISHED or not post.platform_post_id:
        return False
    # Demo connections have no real token / platform post — there's nothing to
    # fetch. No-op cleanly instead of making a doomed API call.
    if post.social_account.access_token == "demo":
        return False
    if not force and not _is_stale(post, now):
        return False

    provider = PROVIDERS.get(post.social_account.platform)
    if provider is None:
        return False

    stats = provider(post.social_account, post.platform_post_id)
    if stats is None:
        return False  # fetch failed/unavailable — keep whatever we had

    post.stat_views = stats.get("views")
    post.stat_likes = stats.get("likes")
    post.stat_comments = stats.get("comments")
    post.stats_updated_at = now
    post.save(update_fields=["stat_views", "stat_likes", "stat_comments", "stats_updated_at"])
    logger.info("Refreshed stats for post %s (%s): %s", post.id, post.social_account.platform, stats)
    return True


def refresh_for_user(user, *, force: bool = False) -> dict:
    """Refresh stats for all of one user's published posts. Returns a summary."""
    now = timezone.now()
    posts = ScheduledPost.objects.filter(
        video__user=user, status=ScheduledPost.Status.PUBLISHED
    ).select_related("social_account", "video")
    updated = sum(1 for p in posts if refresh_post(p, force=force, now=now))
    return {"updated": updated}


def refresh_all(*, force: bool = False) -> dict:
    """Refresh stats for every published post (used by the cron command)."""
    now = timezone.now()
    posts = ScheduledPost.objects.filter(
        status=ScheduledPost.Status.PUBLISHED
    ).select_related("social_account", "video")
    updated = sum(1 for p in posts if refresh_post(p, force=force, now=now))
    return {"updated": updated, "considered": len(posts)}
