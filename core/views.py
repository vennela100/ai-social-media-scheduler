"""Core views.

Phase 0 only needs a landing page + a health check to prove the web pipeline
(request -> Django -> database -> response) works end to end on Render. Real
features (upload, dashboard, scheduling) arrive in later phases.
"""

from django.db import connection
from django.http import JsonResponse
from django.shortcuts import render

from .models import ScheduledPost, Video


def home(request):
    """Landing page. Shows simple counts so we can see the DB is live."""
    context = {
        "video_count": Video.objects.count(),
        "post_count": ScheduledPost.objects.count(),
    }
    return render(request, "home.html", context)


def healthz(request):
    """Return 200 if the app and database are reachable, else 503."""
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception as exc:  # surface the failure rather than hiding it
        return JsonResponse({"status": "error", "detail": str(exc)}, status=503)
    return JsonResponse({"status": "ok"})
