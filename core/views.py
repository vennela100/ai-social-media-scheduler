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
from django.shortcuts import redirect, render

from .forms import VideoUploadForm
from .models import ScheduledPost, Video
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
    """List the signed-in user's uploaded videos."""
    videos = Video.objects.filter(user=request.user)
    return render(request, "dashboard.html", {"videos": videos})


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


def healthz(request):
    """Return 200 if the app and database are reachable, else 503."""
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception as exc:
        return JsonResponse({"status": "error", "detail": str(exc)}, status=503)
    return JsonResponse({"status": "ok"})
