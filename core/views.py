"""Core views.

Phase 1: a login-protected dashboard listing the user's videos, plus an upload
flow (browser -> Cloudinary -> Video row). Real features build on this.
"""

import datetime as dt
import logging

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ImproperlyConfigured
from django.db import connection
from django.db.models import Count
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.crypto import get_random_string
from django.views.decorators.http import require_POST
from django.views.decorators.csrf import ensure_csrf_cookie

from . import ai, instagram, linkedin, stats, youtube
from .forms import (
    AIContentForm,
    GenerateMetadataForm,
    VideoUploadForm,
    media_type_for,
)
from .models import AIContent, ScheduledPost, SocialAccount, Video
from .storage import delete_media, is_configured, upload_media

logger = logging.getLogger("scheduler")


def home(request):
    """Front door. Logged in -> the app; logged out -> the login page.

    This is a private single-user tool, so there's no public marketing landing:
    the root simply routes to the right place.
    """
    if request.user.is_authenticated:
        return redirect("core:dashboard")
    return redirect("login")


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

    return render(
        request,
        "dashboard.html",
        {
            "videos": videos,
            "posts": posts,
            "summary": summary,
            "breakdown": breakdown,
            "current_tz": timezone.get_current_timezone_name(),
        },
    )


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
                cloudinary_public_id=result.get("public_id", ""),
                user_title=form.cleaned_data["title"],
                user_description=form.cleaned_data["description"],
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
    # Bundle each draft with its platform limits + whether that account is
    # connected, so the template can render hints and gate the schedule button.
    drafts = [
        {
            "content": c,
            "limits": ai.PLATFORM_LIMITS.get(c.platform, {}),
            "connected": c.platform in connected,
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
    video = get_object_or_404(Video, pk=pk, user=request.user)
    if request.method != "POST":
        return redirect("core:video_detail", pk=video.pk)
    try:
        delete_media(video.cloudinary_public_id, media_type=video.media_type)
    except Exception as exc:  # Cloudinary hiccup shouldn't block removing the row
        logger.warning("Cloudinary delete failed for video %s: %s", video.pk, exc)
    video.delete()
    messages.success(request, "Video deleted, along with its drafts and scheduled posts.")
    return redirect("core:dashboard")


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


@login_required
def schedule_content(request, pk):
    """Save the user's edits to a draft, then schedule it as a pending post."""
    content = get_object_or_404(AIContent, pk=pk, video__user=request.user)
    if request.method != "POST":
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

    # Visibility the user chose, then normalized to what the platform can ACTUALLY
    # do — so the stored row, dashboard, and toast never claim something the
    # publisher won't honour (and a tampered form can't store a bogus value):
    #   Instagram → always public (reels can't be private)
    #   LinkedIn  → only PUBLIC or CONNECTIONS, so "unlisted" collapses to private
    V = ScheduledPost.Visibility
    visibility = request.POST.get("visibility", V.PUBLIC)
    if visibility not in V.values:
        visibility = V.PUBLIC
    if content.platform == "instagram":
        visibility = V.PUBLIC
    elif content.platform == "linkedin" and visibility == V.UNLISTED:
        visibility = V.PRIVATE

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
        youtube.save_account(request.user, creds)
    except Exception as exc:
        logger.error("YouTube OAuth callback failed: %s", exc)
        detail = f" Details: {exc}" if settings.DEBUG else ""
        messages.error(request, f"Could not connect YouTube. Please try again.{detail}")
        return redirect("core:connections")

    messages.success(request, "YouTube connected.")
    return redirect("core:connections")


@login_required
def youtube_disconnect(request):
    """Remove the stored YouTube connection."""
    if request.method == "POST":
        SocialAccount.objects.filter(user=request.user, platform="youtube").delete()
        messages.success(request, "YouTube disconnected.")
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
    if not expected or request.GET.get("state") != expected:
        messages.error(request, "Instagram connection failed: state mismatch. Try again.")
        return redirect("core:connections")

    redirect_uri = request.build_absolute_uri(reverse("core:instagram_callback"))
    try:
        short_token, ig_user_id = instagram.exchange_code(redirect_uri, request.GET["code"])
        long_token, expires_in = instagram.long_lived_token(short_token)
        instagram.save_account(request.user, long_token, ig_user_id, expires_in)
    except Exception as exc:
        logger.error("Instagram OAuth callback failed: %s", exc)
        detail = f" Details: {exc}" if settings.DEBUG else ""
        messages.error(request, f"Could not connect Instagram. Please try again.{detail}")
        return redirect("core:connections")

    messages.success(request, "Instagram connected.")
    return redirect("core:connections")


@login_required
def instagram_disconnect(request):
    """Remove the stored Instagram connection."""
    if request.method == "POST":
        SocialAccount.objects.filter(user=request.user, platform="instagram").delete()
        messages.success(request, "Instagram disconnected.")
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
        linkedin.save_account(request.user, token, expires_in)
    except Exception as exc:
        logger.error("LinkedIn OAuth callback failed: %s", exc)
        detail = f" Details: {exc}" if settings.DEBUG else ""
        messages.error(request, f"Could not connect LinkedIn. Please try again.{detail}")
        return redirect("core:connections")

    messages.success(request, "LinkedIn connected.")
    return redirect("core:connections")


@login_required
def linkedin_disconnect(request):
    """Remove the stored LinkedIn connection."""
    if request.method == "POST":
        SocialAccount.objects.filter(user=request.user, platform="linkedin").delete()
        messages.success(request, "LinkedIn disconnected.")
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
