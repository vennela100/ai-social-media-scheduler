"""Core views.

Phase 1: a login-protected dashboard listing the user's videos, plus an upload
flow (browser -> Cloudinary -> Video row). Real features build on this.
"""

import datetime as dt
import logging

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login as auth_login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import UserCreationForm
from django.core.exceptions import ImproperlyConfigured
from django.db import connection
from django.db.models import Count
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.crypto import get_random_string
from django.views.decorators.http import require_POST
from django.views.decorators.csrf import csrf_exempt, ensure_csrf_cookie

from . import ai, analytics, instagram, linkedin, publishing, stats, youtube
from .forms import (
    AIContentForm,
    GenerateMetadataForm,
    VideoUploadForm,
    media_type_for,
)
from .models import AIContent, ScheduledPost, SocialAccount, Video
from .notifications import notify_token_expiry
from .storage import (
    delete_media,
    get_usage,
    human_bytes,
    is_configured,
    upload_media,
    usage_level,
)

logger = logging.getLogger("scheduler")


def home(request):
    """Front door. Logged in -> the app; logged out -> the login page.

    This is a private single-user tool, so there's no public marketing landing:
    the root simply routes to the right place.
    """
    if request.user.is_authenticated:
        return redirect("core:dashboard")
    return redirect("login")


def signup(request):
    """Create a new account (username + password) and sign in immediately."""
    if request.user.is_authenticated:
        return redirect("core:dashboard")
    if request.method == "POST":
        form = UserCreationForm(request.POST)
        if form.is_valid():
            user = form.save()
            auth_login(request, user)
            return redirect("core:dashboard")
    else:
        form = UserCreationForm()
    return render(request, "registration/signup.html", {"form": form})


@login_required
def dashboard(request):
    """List the signed-in user's videos and scheduled posts, with analytics."""
    videos = Video.objects.filter(user=request.user)
    posts = (
        ScheduledPost.objects.filter(video__user=request.user)
        .select_related("video", "social_account")
    )

    # One grouped query → {status: count}. Drives the headline stat cards and
    # the status breakdown strip below, instead of len() on the whole queryset.
    counts = {
        row["status"]: row["count"]
        for row in posts.values("status").annotate(count=Count("id"))
    }
    summary = stats.summarize(counts)
    summary["videos"] = videos.count()
    try:
        summary["success_rate"] = stats.success_rate(counts)
    except NotImplementedError:
        # Learning-mode stub not implemented yet — show "—" rather than crash.
        summary["success_rate"] = None

    # Per-status breakdown in the model's declared order, for the pills strip.
    breakdown = [
        (value, label, counts.get(value, 0))
        for value, label in ScheduledPost.Status.choices
    ]

    # Token-expiry banners + Telegram reminders for this user's accounts.
    token_banners = _token_health(request)

    # Resolve a direct reconnect link for any paused (needs_reconnect) post.
    posts = list(posts)
    for p in posts:
        p.reconnect_url = _connect_url(p.social_account.platform)

    storage = _storage_summary(videos)

    return render(
        request,
        "dashboard.html",
        {
            "videos": videos,
            "posts": posts,
            "summary": summary,
            "breakdown": breakdown,
            "storage": storage,
            "token_banners": token_banners,
            "current_tz": timezone.get_current_timezone_name(),
        },
    )


@login_required
def calendar_view(request):
    """Month grid of this user's scheduled + published posts."""
    import calendar as _cal

    today = timezone.localtime(timezone.now())
    try:
        year = int(request.GET.get("year", today.year))
        month = int(request.GET.get("month", today.month))
    except (TypeError, ValueError):
        year, month = today.year, today.month
    if not (1 <= month <= 12):
        year, month = today.year, today.month

    posts = (
        ScheduledPost.objects.filter(
            video__user=request.user,
            scheduled_time_utc__year=year,
            scheduled_time_utc__month=month,
        )
        .select_related("social_account", "video")
        .order_by("scheduled_time_utc")
    )
    by_day: dict[int, list] = {}
    for p in posts:
        day = timezone.localtime(p.scheduled_time_utc).day
        by_day.setdefault(day, []).append(p)

    cal = _cal.Calendar(firstweekday=6)  # weeks start Sunday
    weeks = []
    for week in cal.monthdayscalendar(year, month):
        weeks.append([
            {"day": d, "posts": by_day.get(d, []),
             "today": d == today.day and month == today.month and year == today.year}
            if d else None
            for d in week
        ])

    prev_y, prev_m = (year - 1, 12) if month == 1 else (year, month - 1)
    next_y, next_m = (year + 1, 1) if month == 12 else (year, month + 1)
    return render(request, "calendar.html", {
        "weeks": weeks,
        "month_name": _cal.month_name[month],
        "year": year, "month": month,
        "prev_y": prev_y, "prev_m": prev_m,
        "next_y": next_y, "next_m": next_m,
        "weekday_labels": ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"],
    })


@login_required
def analytics_view(request):
    """Engagement over time — per-post stat history charts."""
    posts = (
        ScheduledPost.objects.filter(
            video__user=request.user,
            status=ScheduledPost.Status.PUBLISHED,
        )
        .select_related("social_account", "video")
        .prefetch_related("stat_snapshots")
        .order_by("-scheduled_time_utc")
    )
    cards = []
    for p in posts:
        snaps = list(p.stat_snapshots.all())
        if not snaps:
            continue
        views = [s.views or 0 for s in snaps]
        # Build an SVG sparkline path for views over time.
        spark = _sparkline([s.views or 0 for s in snaps])
        cards.append({
            "post": p,
            "snaps": snaps,
            "latest": snaps[-1],
            "peak_views": max(views) if views else 0,
            "spark": spark,
        })
    return render(request, "analytics.html", {"cards": cards})


def _sparkline(values, *, width=240, height=44):
    """Return an SVG polyline points string for a list of numbers."""
    if not values:
        return ""
    if len(values) == 1:
        values = values * 2
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1
    n = len(values)
    pts = []
    for i, v in enumerate(values):
        x = round(i / (n - 1) * width, 1)
        y = round(height - (v - lo) / span * height, 1)
        pts.append(f"{x},{y}")
    return " ".join(pts)


def _storage_summary(videos):
    """Build account + current user's storage numbers for dashboard/storage UI."""
    videos = list(videos)
    usage = get_usage()
    cleanable = [
        v for v in videos
        if not v.source_deleted and v.cloudinary_public_id and v.is_fully_published()
    ]
    user_active_bytes = sum(v.source_size_bytes or 0 for v in videos if not v.source_deleted)
    storage = {
        "user_active": human_bytes(user_active_bytes),
        "user_active_bytes": user_active_bytes,
        "cleanable": len(cleanable),
        "cleanable_bytes": sum(v.source_size_bytes or 0 for v in cleanable),
        "cleanable_label": human_bytes(sum(v.source_size_bytes or 0 for v in cleanable)),
    }
    if not usage:
        return storage
    percent = usage["credits_used_percent"]
    if usage["storage_limit_bytes"]:
        percent = max(percent or 0, usage["storage_bytes"] / usage["storage_limit_bytes"] * 100)
    storage.update({
        "used": human_bytes(usage["storage_bytes"]),
        "limit": human_bytes(usage["storage_limit_bytes"]) if usage["storage_limit_bytes"] else None,
        "percent": percent,
        "level": usage_level(percent),
        "assets": usage["assets"],
        "plan": usage["plan"],
    })
    return storage


@login_required
def storage(request):
    """Per-user storage management: inspect active sources and clean safely."""
    videos = list(
        Video.objects.filter(user=request.user)
        .prefetch_related("scheduled_posts", "ai_contents")
    )
    for video in videos:
        video.can_archive_source = (
            not video.source_deleted
            and bool(video.cloudinary_public_id)
            and video.is_fully_published()
        )
    return render(
        request,
        "storage.html",
        {
            "videos": videos,
            "storage": _storage_summary(videos),
        },
    )


# Severity order for stacking expiry banners (most urgent first).
_BANNER_ORDER = {"expired": 0, "urgent": 1, "warning": 2}


def _token_health(request):
    """Build expiry banners for the dashboard and fire throttled email reminders.

    Returns a list of banner dicts (worst first). Auto-refresh platforms (YouTube)
    only surface when their refresh token is actually revoked. Email reminders
    go out at most once per 24h per account.
    """
    accounts = list(SocialAccount.objects.filter(user=request.user))
    banners = []
    reminder_cutoff = timezone.now() - dt.timedelta(hours=24)
    dashboard_url = request.build_absolute_uri(reverse("core:dashboard"))

    for acc in accounts:
        level = acc.health_level()
        if level not in _BANNER_ORDER:
            continue  # good / auto — nothing to show
        banners.append({
            "platform": acc.platform,
            "name": acc.get_platform_display(),
            "level": level,
            "days": acc.days_until_expiry(),
            "connect_url": _connect_url(acc.platform),
        })
        # Throttled email reminder (skip auto-refresh platforms entirely).
        if not acc.auto_refreshes() and (
            acc.last_reminder_sent_at is None or acc.last_reminder_sent_at < reminder_cutoff
        ):
            if notify_token_expiry(acc, dashboard_url):
                acc.last_reminder_sent_at = timezone.now()
                acc.save(update_fields=["last_reminder_sent_at"])

    banners.sort(key=lambda b: _BANNER_ORDER[b["level"]])
    return banners


def _create_pending_drafts(video, platforms):
    """Create one PENDING AIContent per selected platform.

    Generation itself happens asynchronously: the review page fires a request
    per draft so the three platforms generate in parallel with their own
    spinners, rather than blocking the upload for ~10-30s.
    """
    for platform in platforms:
        AIContent.objects.create(
            video=video,
            platform=platform,
            generation_status=AIContent.GenStatus.PENDING,
        )


@login_required
def upload(request):
    """Upload a video, then auto-draft content for each chosen platform.

    Flow: validate -> Cloudinary -> Video row -> AI drafts per platform ->
    land on the video's review page where the user edits and schedules.
    """
    if request.method == "POST":
        form = VideoUploadForm(request.POST, request.FILES)
        if form.is_valid():
            media_type = media_type_for(form.cleaned_data["video"].name) or "video"
            try:
                result = upload_media(form.cleaned_data["video"], media_type=media_type)
            except ImproperlyConfigured as exc:
                # Cloudinary key not set yet — tell the user plainly, don't 500.
                messages.error(request, f"Upload service not ready: {exc}")
                return render(request, "upload.html", _upload_context(form))
            except Exception as exc:  # network / Cloudinary error
                logger.error("Video upload failed: %s", exc)
                messages.error(request, "Upload failed. Please try again.")
                return render(request, "upload.html", _upload_context(form))

            video = Video.objects.create(
                user=request.user,
                media_type=media_type,
                file_url=result["file_url"],
                thumbnail_url=result["thumbnail_url"],
                original_filename=result["original_filename"],
                source_size_bytes=getattr(form.cleaned_data["video"], "size", 0) or 0,
                cloudinary_public_id=result.get("public_id", ""),
                user_title=form.cleaned_data["title"],
                user_description=form.cleaned_data["description"],
                category=form.cleaned_data["category"],
            )

            platforms = form.cleaned_data["platforms"]
            _create_pending_drafts(video, platforms)
            if platforms:
                messages.success(
                    request,
                    f"Uploaded. Generating content for {len(platforms)} platform(s) — "
                    "review, edit, and schedule below.",
                )
            else:
                messages.success(request, "Video uploaded.")
            return redirect("core:video_detail", pk=video.pk)
    else:
        form = VideoUploadForm()

    return render(request, "upload.html", _upload_context(form))


def _upload_context(form):
    return {
        "form": form,
        "upload_ready": is_configured(),
        "ai_ready": ai.is_configured(),
    }


@ensure_csrf_cookie
@login_required
def video_detail(request, pk):
    """Review hub: per-platform AI drafts with inline edit + schedule controls.

    @ensure_csrf_cookie guarantees the csrftoken cookie is set so the page's JS
    can POST to the generate endpoint for each pending draft.
    """
    video = get_object_or_404(Video, pk=pk, user=request.user)
    connected = set(
        SocialAccount.objects.filter(user=request.user).values_list("platform", flat=True)
    )
    # Bundle each draft with its platform limits, whether that account is
    # connected, and whether the platform accepts this media type (YouTube can't
    # take images), so the template can render hints + gate the schedule controls.
    drafts = [
        {
            "content": c,
            "limits": ai.PLATFORM_LIMITS.get(c.platform, {}),
            "connected": c.platform in connected,
            "media_ok": not (video.media_type == "image" and c.platform not in IMAGE_PLATFORMS),
        }
        for c in video.ai_contents.all()
    ]
    return render(
        request,
        "video_detail.html",
        {
            "video": video,
            "drafts": drafts,
            "form": GenerateMetadataForm(),
            "ai_ready": ai.is_configured(),
            "has_description": bool(video.user_description.strip()),
            "current_tz": timezone.get_current_timezone_name(),
            "hours": range(1, 13),
            "minutes": range(60),
        },
    )


@require_POST
@login_required
def generate_ai(request, pk):
    """Generate (or regenerate) one draft from the video's title + description.

    Returns JSON so the review page can fire these in parallel — one per
    platform — each updating its own card when it resolves.
    """
    content = get_object_or_404(AIContent, pk=pk, video__user=request.user)
    video = content.video

    # We persist with QuerySet.update() (a pure UPDATE) instead of content.save().
    # save() falls back to an INSERT if the row vanished mid-request (e.g. the
    # user deleted the video while it was generating), which then trips the FK.
    # update() just touches 0 rows in that case — no crash.
    rows = AIContent.objects.filter(pk=content.pk)

    if not ai.is_configured():
        rows.update(generation_status=AIContent.GenStatus.FAILED)
        return JsonResponse(
            {"ok": False, "error": "AI isn't configured (GEMINI_API_KEY)."}, status=200
        )

    try:
        result = ai.generate_metadata(
            content.platform,
            title=video.user_title,
            description=video.user_description,
            filename=video.original_filename,
            media_type=video.media_type,
            category=video.category,
        )
    except Exception as exc:  # SDK / network / parse — isolate to this platform
        logger.error("Generation failed for %s (AIContent %s): %s", content.platform, pk, exc)
        rows.update(generation_status=AIContent.GenStatus.FAILED)
        return JsonResponse(
            {"ok": False, "error": "Generation failed — tap regenerate to retry."}, status=200
        )

    rows.update(
        generated_title=result["title"],
        generated_description=result["description"],
        generated_hashtags=result["hashtags"],
        ai_model_used=result["model"],
        generation_status=AIContent.GenStatus.DONE,
    )

    # Nudge the user to describe the video when they didn't — better output next time.
    notice = "" if video.user_description.strip() else "For better results, describe your video above."
    return JsonResponse(
        {
            "ok": True,
            "title": result["title"],
            "description": result["description"],
            "hashtags": result["hashtags"],
            "notice": notice,
        }
    )


@login_required
def video_delete(request, pk):
    """Delete a video: its Cloudinary asset, its AI drafts, and its scheduled
    posts (the last two cascade via FK). POST-only so a stray link can't wipe it.
    """
    # Don't 404 on an already-deleted video (stale page / double-submit): just
    # tell the user it's gone and send them back to the dashboard.
    video = Video.objects.filter(pk=pk, user=request.user).first()
    if video is None:
        messages.info(request, "That video was already deleted.")
        return redirect("core:dashboard")
    if request.method != "POST":
        return redirect("core:video_detail", pk=video.pk)
    try:
        # If the source was archived, this id is already gone (harmless no-op);
        # the live asset is the preserved thumbnail, deleted next.
        delete_media(video.cloudinary_public_id, media_type=video.media_type)
        if video.thumbnail_public_id:
            delete_media(video.thumbnail_public_id, media_type="image")
    except Exception as exc:  # Cloudinary hiccup shouldn't block removing the row
        logger.warning("Cloudinary delete failed for video %s: %s", video.pk, exc)
    video.delete()
    messages.success(request, "Video deleted, along with its drafts and scheduled posts.")
    return redirect("core:dashboard")


@require_POST
@login_required
def storage_cleanup(request):
    """Archive the user's fully-published video sources right now to free space.

    Same archive the scheduler does on a 7-day delay, but on demand and ignoring
    the retention window — for when the user hits the "storage full" warning and
    wants space back immediately. Keeps each video's thumbnail.
    """
    eligible = [
        v for v in Video.objects.filter(user=request.user, source_deleted=False)
        .exclude(cloudinary_public_id="")
        if v.is_fully_published()
    ]
    freed = sum(1 for v in eligible if publishing._archive_source(v))
    if freed:
        messages.success(
            request,
            f"Freed up space: archived {freed} published video{'s' if freed != 1 else ''} "
            "(thumbnails kept). Re-upload to post them again.",
        )
    else:
        messages.info(
            request,
            "Nothing to clean up yet — only videos whose posts have all published "
            "can be archived.",
        )
    return redirect("core:storage")


def _purge_user_videos(user):
    """Delete ALL of `user`'s videos and their Cloudinary assets, ignoring publish
    status. Returns {"deleted": int, "failed": int, "freed_bytes": int}.

    DECISION POINT (learning mode): when a single video's Cloudinary delete fails
    (network hiccup, asset already gone, auth blip), do you still delete its DB row?

      • Delete the row anyway  -> the list clears completely and space is "freed" in
        the app's eyes, but the orphaned Cloudinary file may linger and keep costing
        storage (you've lost its public_id, so you can't easily retry).
      • Keep the row on failure -> no orphans; the user can re-run "Delete all" to
        retry, but the list won't fully empty in one click.

    Iterate `Video.objects.filter(user=user)`. For each, try delete_media() on the
    source (video.cloudinary_public_id, media_type=video.media_type) and, if present,
    the thumbnail (video.thumbnail_public_id, media_type="image") — mirror how
    video_delete() does it. Tally results and return the counts above.
    """
    # TODO(you): implement the purge loop + your chosen failure policy (≈8-10 lines).
    raise NotImplementedError


@require_POST
@login_required
def storage_delete_all(request):
    """Delete every uploaded video for this user, regardless of publish status.

    The nuclear option behind the conservative storage_cleanup: when the user just
    wants ALL their source files and Cloudinary assets gone to reclaim space. The
    template guards this with a typed/double confirmation since it's irreversible.
    """
    try:
        result = _purge_user_videos(request.user)
    except NotImplementedError:
        messages.error(
            request,
            "Delete-all isn't finished yet (the purge step is a pending learning-mode "
            "contribution). Nothing was deleted.",
        )
        return redirect("core:storage")

    deleted, failed = result["deleted"], result["failed"]
    if not deleted and not failed:
        messages.info(request, "Nothing to delete — you have no uploads.")
    elif failed:
        messages.warning(
            request,
            f"Deleted {deleted} video{'s' if deleted != 1 else ''}, but {failed} "
            f"couldn't be fully removed from storage. Try again to retry those.",
        )
    else:
        freed = human_bytes(result.get("freed_bytes", 0))
        messages.success(
            request,
            f"Deleted all {deleted} video{'s' if deleted != 1 else ''} and freed {freed}. "
            "This also removed their drafts and scheduled posts.",
        )
    return redirect("core:storage")


@require_POST
@login_required
def video_archive_source(request, pk):
    """Archive one fully-published source file, keeping its thumbnail/history."""
    video = get_object_or_404(Video, pk=pk, user=request.user)
    if video.source_deleted:
        messages.info(request, "That source file is already archived.")
        return redirect("core:storage")
    if not video.cloudinary_public_id:
        messages.info(request, "This upload has no source file to archive.")
        return redirect("core:storage")
    if not video.is_fully_published():
        messages.error(
            request,
            "This source is still needed. Archive is available after all posts for it are published.",
        )
        return redirect("core:storage")
    if publishing._archive_source(video):
        messages.success(request, f"Archived {video} and kept its thumbnail.")
    else:
        messages.error(request, "Could not archive that source file. Try again later.")
    return redirect("core:storage")


def _parse_local_datetime(raw):
    """Parse a datetime-local string as the active (local) tz -> aware datetime."""
    if not raw:
        return None
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            naive = dt.datetime.strptime(raw, fmt)
        except ValueError:
            continue
        return timezone.make_aware(naive, timezone.get_current_timezone())
    return None


def _parse_schedule_time(request):
    """Build an aware datetime from the date + 12-hour (AM/PM) schedule controls.

    Falls back to a legacy datetime-local field if those aren't present.
    """
    date_str = request.POST.get("sched_date", "").strip()
    hour = request.POST.get("sched_hour", "").strip()
    minute = request.POST.get("sched_minute", "").strip()
    ampm = request.POST.get("sched_ampm", "").strip().upper()
    if not (date_str and hour and minute and ampm):
        return _parse_local_datetime(request.POST.get("scheduled_time", "").strip())
    try:
        h = int(hour) % 12            # 12 -> 0, 1..11 stay
        if ampm == "PM":
            h += 12                   # 12 PM -> 12, 1 PM -> 13, ...
        naive = dt.datetime.strptime(f"{date_str} {h:02d}:{int(minute):02d}", "%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return None
    return timezone.make_aware(naive, timezone.get_current_timezone())


def _final_caption(content):
    """Assemble the caption stored on the post from the (edited) draft fields.

    YouTube publishes title/description/tags separately, so its caption is just
    the description. Instagram/LinkedIn take one text blob, so we append the
    hashtags below the body.
    """
    if content.platform == "youtube":
        return content.generated_description
    parts = [p for p in (content.generated_description, content.generated_hashtags) if p]
    return "\n\n".join(parts)


# Which platforms can actually publish a still image today. YouTube is video-only;
# our Instagram publisher only does video Reels — so images go to LinkedIn alone.
# (Decision point: widen this once an Instagram image-post path exists.)
IMAGE_PLATFORMS = {"linkedin"}


def _normalize_visibility(platform: str, raw):
    """Clamp a requested visibility to what the platform can actually honour.

    Instagram reels are always public; LinkedIn only offers PUBLIC or CONNECTIONS
    (so "unlisted" collapses to private→CONNECTIONS); an unknown value falls back
    to PUBLIC. Keeps the stored row, dashboard, and publisher in agreement and
    stops a tampered form from saving a bogus value.
    """
    V = ScheduledPost.Visibility
    visibility = raw if raw in V.values else V.PUBLIC
    if platform == "instagram":
        return V.PUBLIC
    if platform == "linkedin" and visibility == V.UNLISTED:
        return V.PRIVATE
    return visibility


@login_required
def schedule_content(request, pk):
    """Save the user's edits to a draft, then schedule it as a pending post."""
    content = get_object_or_404(AIContent, pk=pk, video__user=request.user)
    if request.method != "POST":
        return redirect("core:video_detail", pk=content.video.pk)

    # The source file is archived after publishing — there's nothing left to post.
    if content.video.source_deleted:
        messages.error(
            request,
            "This video's source was archived after publishing. Re-upload it to post again.",
        )
        return redirect("core:video_detail", pk=content.video.pk)

    # An image can only be scheduled to a platform that accepts images.
    if content.video.media_type == "image" and content.platform not in IMAGE_PLATFORMS:
        messages.error(
            request,
            f"{content.get_platform_display()} can't post a still image — images can "
            f"only go to {', '.join(p.title() for p in IMAGE_PLATFORMS)} right now.",
        )
        return redirect("core:video_detail", pk=content.video.pk)

    # Persist edits FIRST so they survive any validation bounce-back (the review
    # page re-reads them straight from the row).
    content.generated_title = request.POST.get("title", "").strip()
    content.generated_description = request.POST.get("description", "").strip()
    content.generated_hashtags = request.POST.get("hashtags", "").strip()
    content.save(update_fields=["generated_title", "generated_description", "generated_hashtags"])

    account = SocialAccount.objects.filter(
        user=request.user, platform=content.platform
    ).first()
    if not account:
        messages.error(
            request,
            f"Connect your {content.get_platform_display()} account before scheduling.",
        )
        return redirect("core:video_detail", pk=content.video.pk)

    when = _parse_schedule_time(request)
    if when is None:
        messages.error(request, "Pick a valid date and time.")
        return redirect("core:video_detail", pk=content.video.pk)
    if when <= timezone.now():
        messages.error(request, "Pick a time in the future.")
        return redirect("core:video_detail", pk=content.video.pk)

    violations = ai.validate_metadata(
        content.platform,
        content.generated_title,
        content.generated_description,
        content.generated_hashtags,
    )
    if violations:
        for v in violations:
            messages.error(request, v)
        return redirect("core:video_detail", pk=content.video.pk)

    # Visibility the user chose, clamped to what the platform can actually honour.
    visibility = _normalize_visibility(content.platform, request.POST.get("visibility"))

    post = ScheduledPost.objects.create(
        video=content.video,
        social_account=account,
        ai_content=content,
        final_caption=_final_caption(content),
        scheduled_time_utc=when,
        visibility=visibility,
        status=ScheduledPost.Status.PENDING,
    )
    messages.success(
        request,
        f"Scheduled to {content.get_platform_display()} ({post.get_visibility_display()}) "
        f"for {when:%b %d, %Y · %H:%M} UTC.",
    )
    return redirect("core:dashboard")


@require_POST
@login_required
def schedule_all(request, pk):
    """Schedule every ready draft of a video to one shared time (one click).

    Skips drafts still generating, platforms not connected, and image drafts on
    platforms that can't post images. Each gets the platform's default (public)
    visibility — use the per-draft form for finer control.
    """
    video = get_object_or_404(Video, pk=pk, user=request.user)
    if video.source_deleted:
        messages.error(request, "This video's source was archived. Re-upload it to post again.")
        return redirect("core:video_detail", pk=video.pk)

    when = _parse_schedule_time(request)
    if when is None:
        messages.error(request, "Pick a valid date and time.")
        return redirect("core:video_detail", pk=video.pk)
    if when <= timezone.now():
        messages.error(request, "Pick a time in the future.")
        return redirect("core:video_detail", pk=video.pk)

    scheduled, skipped = [], []
    for content in video.ai_contents.all():
        name = content.get_platform_display()
        if content.generation_status != AIContent.GenStatus.DONE:
            skipped.append(f"{name} (still drafting)")
            continue
        if video.media_type == "image" and content.platform not in IMAGE_PLATFORMS:
            skipped.append(f"{name} (no image support)")
            continue
        account = SocialAccount.objects.filter(user=request.user, platform=content.platform).first()
        if not account:
            skipped.append(f"{name} (not connected)")
            continue
        if ai.validate_metadata(content.platform, content.generated_title,
                                content.generated_description, content.generated_hashtags):
            skipped.append(f"{name} (needs edits)")
            continue
        ScheduledPost.objects.create(
            video=video, social_account=account, ai_content=content,
            final_caption=_final_caption(content), scheduled_time_utc=when,
            visibility=_normalize_visibility(content.platform, "public"),
            status=ScheduledPost.Status.PENDING,
        )
        scheduled.append(name)

    if scheduled:
        messages.success(
            request,
            f"Scheduled {len(scheduled)} post{'s' if len(scheduled) != 1 else ''} "
            f"({', '.join(scheduled)}) for {when:%b %d, %Y · %H:%M} UTC."
            + (f" Skipped: {', '.join(skipped)}." if skipped else "")
        )
        return redirect("core:dashboard")
    messages.error(request, "Nothing scheduled. " + (", ".join(skipped) if skipped else "No ready drafts."))
    return redirect("core:video_detail", pk=video.pk)


@require_POST
@login_required
def suggest_times(request, pk):
    """JSON: AI-suggested best posting slots for this draft's platform + niche.

    Returns 200 with {"ok": False, "error": ...} on any problem (not a 4xx/5xx)
    so the review-page JS can always parse the body and show a message inline,
    matching the generate_ai contract.
    """
    content = get_object_or_404(AIContent, pk=pk, video__user=request.user)
    if not ai.is_configured():
        return JsonResponse({
            "ok": True,
            "slots": ai.fallback_post_times(content.platform),
            "notice": "Using standard best-time suggestions because Gemini is not configured.",
        })
    try:
        slots = ai.suggest_post_times(content.platform, category=content.video.category)
    except Exception as exc:
        logger.warning("suggest_times failed for ai %s: %s", pk, exc)
        return JsonResponse({
            "ok": True,
            "slots": ai.fallback_post_times(content.platform),
            "notice": "Using standard best-time suggestions because Gemini is temporarily unavailable.",
        })
    return JsonResponse({"ok": True, "slots": slots})


@require_POST
@login_required
def refresh_stats(request):
    """Pull fresh views/likes/comments for the user's published posts, then return."""
    result = analytics.refresh_for_user(request.user, force=True)
    n = result["updated"]
    if n:
        messages.success(request, f"Updated stats for {n} post{'s' if n != 1 else ''}.")
    else:
        messages.info(request, "No new stats — already up to date (or none published yet).")
    return redirect("core:dashboard")


@require_POST
@login_required
def post_refresh_stats(request, pk):
    """Pull fresh engagement for one published post from the dashboard row."""
    post = get_object_or_404(
        ScheduledPost.objects.select_related("social_account", "video"),
        pk=pk,
        video__user=request.user,
    )

    if post.status != ScheduledPost.Status.PUBLISHED:
        messages.error(request, "Only published posts have stats to refresh.")
        return redirect("core:dashboard")
    if not post.platform_post_id:
        messages.error(request, "This post has no platform id yet, so stats cannot be refreshed.")
        return redirect("core:dashboard")

    if post.social_account.access_token == "demo":
        messages.info(
            request,
            f"{post.social_account.get_platform_display()} is a demo connection — live "
            "engagement isn't available. Connect the real account to pull stats.",
        )
        return redirect("core:dashboard")

    if analytics.refresh_post(post, force=True):
        post.refresh_from_db(fields=["stat_views", "stat_likes", "stat_comments", "stats_updated_at"])
        bits = []
        if post.stat_views is not None:
            bits.append(f"{post.stat_views} views")
        if post.stat_likes is not None:
            bits.append(f"{post.stat_likes} likes")
        if post.stat_comments is not None:
            bits.append(f"{post.stat_comments} comments")
        detail = ", ".join(bits) if bits else "no metrics reported"
        messages.success(request, f"Updated {post.social_account.get_platform_display()} stats: {detail}.")
    else:
        messages.error(
            request,
            f"Could not refresh {post.social_account.get_platform_display()} stats. "
            "Reconnect the account or try again later.",
        )
    return redirect("core:dashboard")


@require_POST
@login_required
def post_cancel(request, pk):
    """Cancel (delete) a not-yet-published scheduled post."""
    post = ScheduledPost.objects.filter(pk=pk, video__user=request.user).first()
    if post is None:
        messages.info(request, "That scheduled post is already gone.")
        return redirect("core:dashboard")
    if not post.is_editable():
        messages.error(
            request,
            "This post can't be cancelled — it's already publishing or published.",
        )
        return redirect("core:dashboard")
    platform = post.social_account.get_platform_display()
    post.delete()
    messages.success(request, f"Cancelled the {platform} post.")
    return redirect("core:dashboard")


@require_POST
@login_required
def post_retry(request, pk):
    """Requeue a failed (or needs-reconnect) post to publish on the next tick."""
    post = get_object_or_404(
        ScheduledPost.objects.select_related("social_account"),
        pk=pk,
        video__user=request.user,
    )
    retryable = (ScheduledPost.Status.FAILED, ScheduledPost.Status.NEEDS_RECONNECT)
    if post.status not in retryable:
        messages.error(request, "Only failed posts can be retried.")
        return redirect("core:dashboard")
    if post.social_account.is_expired():
        messages.error(
            request,
            f"Reconnect {post.social_account.get_platform_display()} first — its "
            "access has expired.",
        )
        return redirect("core:connections")
    post.status = ScheduledPost.Status.PENDING
    post.retry_count = 0
    post.last_error = ""
    post.scheduled_time_utc = timezone.now()  # due immediately
    post.save(update_fields=["status", "retry_count", "last_error", "scheduled_time_utc", "updated_at"])
    messages.success(
        request,
        f"{post.social_account.get_platform_display()} post requeued — it'll publish shortly.",
    )
    return redirect("core:dashboard")


@login_required
def post_edit(request, pk):
    """Edit a pending post's publish time and visibility before it goes out."""
    post = get_object_or_404(ScheduledPost, pk=pk, video__user=request.user)
    if not post.is_editable():
        messages.error(request, "This post can't be edited — it's already publishing or published.")
        return redirect("core:dashboard")

    if request.method != "POST":
        return render(request, "post_edit.html", {
            "post": post,
            "current_tz": timezone.get_current_timezone_name(),
            # Pre-fill the time controls with the existing time in the viewer's tz.
            "local": timezone.localtime(post.scheduled_time_utc),
        })

    when = _parse_schedule_time(request)
    if when is None:
        messages.error(request, "Pick a valid date and time.")
        return redirect("core:post_edit", pk=pk)
    if when <= timezone.now():
        messages.error(request, "Pick a time in the future.")
        return redirect("core:post_edit", pk=pk)

    visibility = _normalize_visibility(post.social_account.platform, request.POST.get("visibility"))
    post.scheduled_time_utc = when
    post.visibility = visibility
    # A failed/paused post the user reschedules should queue again from scratch.
    if post.status != ScheduledPost.Status.PENDING:
        post.status = ScheduledPost.Status.PENDING
        post.retry_count = 0
        post.last_error = ""
    post.save(update_fields=[
        "scheduled_time_utc", "visibility", "status", "retry_count", "last_error", "updated_at",
    ])
    messages.success(
        request,
        f"Updated — now scheduled for {when:%b %d, %Y · %H:%M} UTC ({post.get_visibility_display()}).",
    )
    return redirect("core:dashboard")


@login_required
def generate(request, pk):
    """Add a draft for another platform. Creates a PENDING row; the review page
    generates it (the JS picks up any pending draft and fills it in)."""
    video = get_object_or_404(Video, pk=pk, user=request.user)
    if request.method != "POST":
        return redirect("core:video_detail", pk=video.pk)

    form = GenerateMetadataForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Pick a platform first.")
        return redirect("core:video_detail", pk=video.pk)

    AIContent.objects.create(
        video=video,
        platform=form.cleaned_data["platform"],
        generation_status=AIContent.GenStatus.PENDING,
    )
    return redirect("core:video_detail", pk=video.pk)


@login_required
def aicontent_edit(request, pk):
    """Review/edit generated metadata; validate against platform limits on save."""
    content = get_object_or_404(AIContent, pk=pk, video__user=request.user)

    if request.method == "POST":
        form = AIContentForm(request.POST, instance=content)
        if form.is_valid():
            saved = form.save(commit=False)
            violations = ai.validate_metadata(
                content.platform,
                saved.generated_title,
                saved.generated_description,
                saved.generated_hashtags,
            )
            if violations:
                # Surface limit problems but let the user keep editing.
                for v in violations:
                    messages.error(request, v)
                return render(request, "aicontent_edit.html", {"form": form, "content": content})
            saved.save()
            messages.success(request, "Saved.")
            return redirect("core:video_detail", pk=content.video.pk)
    else:
        form = AIContentForm(instance=content)

    return render(request, "aicontent_edit.html", {"form": form, "content": content})


CONNECT_URL_NAMES = {
    "youtube": "core:youtube_connect",
    "instagram": "core:instagram_connect",
    "linkedin": "core:linkedin_connect",
}


def _connect_url(platform):
    return reverse(CONNECT_URL_NAMES[platform])


def _after_connect(request, account):
    """Post-(re)connect housekeeping: mark healthy, resume paused posts, toast.

    Used by every OAuth callback so a reconnect transparently recovers: the
    fresh token clears the bad status, any posts paused for this platform return
    to PENDING (resuming at their original time), and the reminder clock resets.
    """
    account.status = SocialAccount.Status.CONNECTED
    account.last_reminder_sent_at = None
    account.save(update_fields=["status", "last_reminder_sent_at"])

    resumed = ScheduledPost.objects.filter(
        social_account=account, status=ScheduledPost.Status.NEEDS_RECONNECT
    ).update(status=ScheduledPost.Status.PENDING, last_error="")

    name = account.get_platform_display()
    if account.auto_refreshes():
        msg = f"{name} reconnected successfully. Auto-refreshed — no action needed."
    elif account.token_expires_at:
        msg = f"{name} reconnected successfully. Token valid until {account.token_expires_at:%b %d, %Y}."
    else:
        msg = f"{name} reconnected successfully."
    if resumed:
        msg += f" Resumed {resumed} paused post{'s' if resumed != 1 else ''}."
    messages.success(request, msg)


@login_required
def connections(request):
    """Show connected platform accounts and connect/disconnect controls."""
    accounts = {a.platform: a for a in SocialAccount.objects.filter(user=request.user)}
    return render(
        request,
        "connections.html",
        {
            "accounts": accounts,
            "youtube_ready": youtube.is_configured(),
            "instagram_ready": instagram.is_configured(),
            "linkedin_ready": linkedin.is_configured(),
        },
    )


@login_required
def youtube_connect(request):
    """Kick off the Google OAuth flow."""
    if not youtube.is_configured():
        messages.error(request, "YouTube OAuth isn't configured (GOOGLE_OAUTH_CLIENT_ID/SECRET).")
        return redirect("core:connections")

    redirect_uri = request.build_absolute_uri(reverse("core:youtube_callback"))
    auth_url, state, code_verifier = youtube.build_auth_url(redirect_uri)
    request.session["youtube_oauth_state"] = state
    request.session["youtube_code_verifier"] = code_verifier
    return redirect(auth_url)


@login_required
def youtube_callback(request):
    """Handle Google's redirect: exchange the code and store tokens."""
    if request.GET.get("error"):
        messages.error(request, f"YouTube authorization was denied: {request.GET['error']}.")
        return redirect("core:connections")

    state = request.session.pop("youtube_oauth_state", None)
    code_verifier = request.session.pop("youtube_code_verifier", None)
    redirect_uri = request.build_absolute_uri(reverse("core:youtube_callback"))
    try:
        creds = youtube.exchange_code(
            redirect_uri, request.build_absolute_uri(), state, code_verifier
        )
        account = youtube.save_account(request.user, creds)
    except Exception as exc:
        logger.error("YouTube OAuth callback failed: %s", exc)
        detail = f" Details: {exc}" if settings.DEBUG else ""
        messages.error(request, f"Could not connect YouTube. Please try again.{detail}")
        return redirect("core:connections")

    _after_connect(request, account)
    return redirect("core:connections")


def _disconnect_account(request, platform):
    """Disconnect a platform WITHOUT losing its scheduled posts.

    Deleting the SocialAccount would cascade-delete every ScheduledPost tied to
    it. Instead we clear the tokens, mark the account needs-reconnect, and pause
    its still-pending posts — so they survive and resume on reconnect (see
    _after_connect, which flips needs_reconnect posts back to pending).
    """
    acc = SocialAccount.objects.filter(user=request.user, platform=platform).first()
    if not acc:
        return
    paused = ScheduledPost.objects.filter(
        social_account=acc,
        status__in=[ScheduledPost.Status.PENDING, ScheduledPost.Status.PROCESSING],
    ).update(
        status=ScheduledPost.Status.NEEDS_RECONNECT,
        last_error="Account disconnected — reconnect to resume.",
    )
    acc.access_token = ""
    acc.refresh_token = ""
    acc.status = SocialAccount.Status.NEEDS_RECONNECT
    acc.save(update_fields=["access_token", "refresh_token", "status"])
    msg = f"{acc.get_platform_display()} disconnected."
    if paused:
        msg += f" {paused} scheduled post{'s' if paused != 1 else ''} paused — reconnect to resume."
    messages.success(request, msg)


@login_required
def youtube_disconnect(request):
    """Disconnect YouTube, keeping (pausing) its scheduled posts."""
    if request.method == "POST":
        _disconnect_account(request, "youtube")
    return redirect("core:connections")


@login_required
def instagram_connect(request):
    """Kick off the Instagram Login OAuth flow."""
    if not instagram.is_configured():
        messages.error(request, "Instagram OAuth isn't configured (INSTAGRAM_APP_ID/SECRET).")
        return redirect("core:connections")

    state = get_random_string(32)
    request.session["instagram_oauth_state"] = state
    redirect_uri = request.build_absolute_uri(reverse("core:instagram_callback"))
    return redirect(instagram.build_auth_url(redirect_uri, state))


@login_required
def instagram_callback(request):
    """Handle Instagram's redirect: exchange code, store the long-lived token."""
    if request.GET.get("error"):
        messages.error(request, f"Instagram authorization was denied: {request.GET.get('error_description', request.GET['error'])}.")
        return redirect("core:connections")

    expected = request.session.pop("instagram_oauth_state", None)
    returned = request.GET.get("state")
    if not expected or returned != expected:
        # Log which failure mode so a recurring mismatch is diagnosable: "missing"
        # = the session didn't carry the state over (cookie/duplicate-tab issue);
        # "mismatch" = a stale or replayed callback. We deliberately log neither the
        # state values nor the session key — those are sensitive (session hijack).
        mode = "missing" if not expected else "mismatch"
        logger.warning("Instagram state check failed: mode=%s", mode)
        messages.error(request, "Instagram connection failed: state mismatch. Try again.")
        return redirect("core:connections")

    redirect_uri = request.build_absolute_uri(reverse("core:instagram_callback"))
    try:
        short_token, ig_user_id = instagram.exchange_code(redirect_uri, request.GET["code"])
        long_token, expires_in = instagram.long_lived_token(short_token)
        account = instagram.save_account(request.user, long_token, ig_user_id, expires_in)
    except Exception as exc:
        logger.error("Instagram OAuth callback failed: %s", exc)
        detail = f" Details: {exc}" if settings.DEBUG else ""
        messages.error(request, f"Could not connect Instagram. Please try again.{detail}")
        return redirect("core:connections")

    _after_connect(request, account)
    return redirect("core:connections")


@login_required
def instagram_disconnect(request):
    """Disconnect Instagram, keeping (pausing) its scheduled posts."""
    if request.method == "POST":
        _disconnect_account(request, "instagram")
    return redirect("core:connections")


@login_required
def linkedin_connect(request):
    """Kick off the LinkedIn OAuth flow."""
    if not linkedin.is_configured():
        messages.error(request, "LinkedIn OAuth isn't configured (LINKEDIN_CLIENT_ID/SECRET).")
        return redirect("core:connections")

    state = get_random_string(32)
    request.session["linkedin_oauth_state"] = state
    redirect_uri = request.build_absolute_uri(reverse("core:linkedin_callback"))
    return redirect(linkedin.build_auth_url(redirect_uri, state))


@login_required
def linkedin_callback(request):
    """Handle LinkedIn's redirect: exchange code, store the member token."""
    if request.GET.get("error"):
        messages.error(request, f"LinkedIn authorization was denied: {request.GET.get('error_description', request.GET['error'])}.")
        return redirect("core:connections")

    expected = request.session.pop("linkedin_oauth_state", None)
    if not expected or request.GET.get("state") != expected:
        messages.error(request, "LinkedIn connection failed: state mismatch. Try again.")
        return redirect("core:connections")

    redirect_uri = request.build_absolute_uri(reverse("core:linkedin_callback"))
    try:
        token, expires_in = linkedin.exchange_code(redirect_uri, request.GET["code"])
        account = linkedin.save_account(request.user, token, expires_in)
    except Exception as exc:
        logger.error("LinkedIn OAuth callback failed: %s", exc)
        detail = f" Details: {exc}" if settings.DEBUG else ""
        messages.error(request, f"Could not connect LinkedIn. Please try again.{detail}")
        return redirect("core:connections")

    _after_connect(request, account)
    return redirect("core:connections")


@login_required
def linkedin_disconnect(request):
    """Disconnect LinkedIn, keeping (pausing) its scheduled posts."""
    if request.method == "POST":
        _disconnect_account(request, "linkedin")
    return redirect("core:connections")


def healthz(request):
    """Return 200 if the app and database are reachable, else 503."""
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception as exc:
        return JsonResponse({"status": "error", "detail": str(exc)}, status=503)
    return JsonResponse({"status": "ok"})


@csrf_exempt
def run_publisher(request):
    """Publish due posts on demand. Pinged by an external cron (cron-job.org) every
    minute so posts go out on time, unlike the throttled GitHub Actions schedule.

    Guarded by a shared secret in settings.CRON_KEY (env CRON_KEY); pass it as
    ?key=... or the X-Cron-Key header. Same logic the management command runs.
    """
    expected = getattr(settings, "CRON_KEY", "")
    provided = request.GET.get("key") or request.headers.get("X-Cron-Key", "")
    if not expected or provided != expected:
        return JsonResponse({"status": "forbidden"}, status=403)
    summary = publishing.run()
    # Also pull fresh engagement stats so the dashboard stays current without
    # anyone clicking Refresh. Only re-fetches posts past their staleness window.
    stats_result = analytics.refresh_all()
    return JsonResponse({"status": "ok", "summary": summary, "stats": stats_result})
