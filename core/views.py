"""Core views.

Phase 1: a login-protected dashboard listing the user's videos, plus an upload
flow (browser -> Cloudinary -> Video row). Real features build on this.
"""

import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ImproperlyConfigured
from django.db import connection
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from . import ai, youtube
from .forms import (
    AIContentForm,
    GenerateMetadataForm,
    ScheduledPostForm,
    VideoUploadForm,
)
from .models import AIContent, ScheduledPost, SocialAccount, Video
from .storage import is_configured, upload_video

logger = logging.getLogger("scheduler")


def home(request):
    """Public landing page. Authenticated users are sent to the dashboard."""
    if request.user.is_authenticated:
        return redirect("core:dashboard")
    context = {
        "video_count": Video.objects.count(),
        "post_count": ScheduledPost.objects.count(),
    }
    return render(request, "home.html", context)


@login_required
def dashboard(request):
    """List the signed-in user's uploaded videos and scheduled posts."""
    videos = Video.objects.filter(user=request.user)
    posts = (
        ScheduledPost.objects.filter(video__user=request.user)
        .select_related("video", "social_account")
    )
    return render(request, "dashboard.html", {"videos": videos, "posts": posts})


@login_required
def upload(request):
    """Handle a video upload: validate -> Cloudinary -> create Video row."""
    if request.method == "POST":
        form = VideoUploadForm(request.POST, request.FILES)
        if form.is_valid():
            try:
                result = upload_video(form.cleaned_data["video"])
            except ImproperlyConfigured as exc:
                # Cloudinary key not set yet — tell the user plainly, don't 500.
                messages.error(request, f"Upload service not ready: {exc}")
                return render(request, "upload.html", {"form": form})
            except Exception as exc:  # network / Cloudinary error
                logger.error("Video upload failed: %s", exc)
                messages.error(request, "Upload failed. Please try again.")
                return render(request, "upload.html", {"form": form})

            Video.objects.create(
                user=request.user,
                file_url=result["file_url"],
                thumbnail_url=result["thumbnail_url"],
                original_filename=result["original_filename"],
            )
            messages.success(request, "Video uploaded.")
            return redirect("core:dashboard")
    else:
        form = VideoUploadForm()

    return render(request, "upload.html", {"form": form, "upload_ready": is_configured()})


@login_required
def video_detail(request, pk):
    """Show one video, its generated metadata, and a form to generate more."""
    video = get_object_or_404(Video, pk=pk, user=request.user)
    return render(
        request,
        "video_detail.html",
        {
            "video": video,
            "ai_contents": video.ai_contents.all(),
            "form": GenerateMetadataForm(),
            "ai_ready": ai.is_configured(),
        },
    )


@login_required
def generate(request, pk):
    """Generate AI metadata for a video on a chosen platform, then go to review."""
    video = get_object_or_404(Video, pk=pk, user=request.user)
    if request.method != "POST":
        return redirect("core:video_detail", pk=video.pk)

    form = GenerateMetadataForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Pick a platform first.")
        return redirect("core:video_detail", pk=video.pk)

    platform = form.cleaned_data["platform"]
    try:
        result = ai.generate_metadata(
            platform, brief=form.cleaned_data["brief"], filename=video.original_filename
        )
    except ImproperlyConfigured as exc:
        messages.error(request, f"AI not ready: {exc}")
        return redirect("core:video_detail", pk=video.pk)
    except Exception as exc:  # SDK/network/parse error
        logger.error("Metadata generation failed: %s", exc)
        messages.error(request, "Generation failed. Please try again.")
        return redirect("core:video_detail", pk=video.pk)

    content = AIContent.objects.create(
        video=video,
        platform=platform,
        generated_title=result["title"],
        generated_description=result["description"],
        generated_hashtags=result["hashtags"],
        ai_model_used=result["model"],
    )
    messages.success(request, f"Generated {platform} metadata — review and edit below.")
    return redirect("core:aicontent_edit", pk=content.pk)


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
        {"accounts": accounts, "youtube_ready": youtube.is_configured()},
    )


@login_required
def youtube_connect(request):
    """Kick off the Google OAuth flow."""
    if not youtube.is_configured():
        messages.error(request, "YouTube OAuth isn't configured (GOOGLE_OAUTH_CLIENT_ID/SECRET).")
        return redirect("core:connections")

    redirect_uri = request.build_absolute_uri(reverse("core:youtube_callback"))
    auth_url, state = youtube.build_auth_url(redirect_uri)
    request.session["youtube_oauth_state"] = state
    return redirect(auth_url)


@login_required
def youtube_callback(request):
    """Handle Google's redirect: exchange the code and store tokens."""
    if request.GET.get("error"):
        messages.error(request, f"YouTube authorization was denied: {request.GET['error']}.")
        return redirect("core:connections")

    state = request.session.pop("youtube_oauth_state", None)
    redirect_uri = request.build_absolute_uri(reverse("core:youtube_callback"))
    try:
        creds = youtube.exchange_code(redirect_uri, request.build_absolute_uri(), state)
        youtube.save_account(request.user, creds)
    except Exception as exc:
        logger.error("YouTube OAuth callback failed: %s", exc)
        messages.error(request, "Could not connect YouTube. Please try again.")
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
def schedule_post(request):
    """Create a ScheduledPost (status=pending) for the cron to publish later."""
    if request.method == "POST":
        form = ScheduledPostForm(request.POST, user=request.user)
        if form.is_valid():
            post = form.save(commit=False)
            post.status = ScheduledPost.Status.PENDING
            post.save()
            messages.success(request, "Post scheduled.")
            return redirect("core:dashboard")
    else:
        form = ScheduledPostForm(user=request.user)
    return render(request, "schedule.html", {"form": form})


def healthz(request):
    """Return 200 if the app and database are reachable, else 503."""
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception as exc:
        return JsonResponse({"status": "error", "detail": str(exc)}, status=503)
    return JsonResponse({"status": "ok"})
