"""Core views.

Phase 1: a login-protected dashboard listing the user's videos, plus an upload
flow (browser -> Cloudinary -> Video row). Real features build on this.
"""

import logging

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ImproperlyConfigured
from django.db import connection
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.crypto import get_random_string

from . import ai, instagram, linkedin, youtube
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
    """Front door. Logged in -> the app; logged out -> the login page.

    This is a private single-user tool, so there's no public marketing landing:
    the root simply routes to the right place.
    """
    if request.user.is_authenticated:
        return redirect("core:dashboard")
    return redirect("login")


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
    """Kick off the Facebook/Instagram OAuth flow."""
    if not instagram.is_configured():
        messages.error(request, "Instagram OAuth isn't configured (META_APP_ID/SECRET).")
        return redirect("core:connections")

    state = get_random_string(32)
    request.session["instagram_oauth_state"] = state
    redirect_uri = request.build_absolute_uri(reverse("core:instagram_callback"))
    return redirect(instagram.build_auth_url(redirect_uri, state))


@login_required
def instagram_callback(request):
    """Handle Facebook's redirect: exchange code, store the long-lived token."""
    if request.GET.get("error"):
        messages.error(request, f"Instagram authorization was denied: {request.GET.get('error_description', request.GET['error'])}.")
        return redirect("core:connections")

    expected = request.session.pop("instagram_oauth_state", None)
    if not expected or request.GET.get("state") != expected:
        messages.error(request, "Instagram connection failed: state mismatch. Try again.")
        return redirect("core:connections")

    redirect_uri = request.build_absolute_uri(reverse("core:instagram_callback"))
    try:
        short_token = instagram.exchange_code(redirect_uri, request.GET["code"])
        long_token, expires_in = instagram.long_lived_token(short_token)
        instagram.save_account(request.user, long_token, expires_in)
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
