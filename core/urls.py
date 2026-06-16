"""URL routes for the core app."""

from django.urls import path

from . import views

app_name = "core"

urlpatterns = [
    path("", views.home, name="home"),
    path("dashboard/", views.dashboard, name="dashboard"),
    path("upload/", views.upload, name="upload"),
    path("video/<int:pk>/", views.video_detail, name="video_detail"),
    path("video/<int:pk>/delete/", views.video_delete, name="video_delete"),
    path("video/<int:pk>/generate/", views.generate, name="generate"),
    path("ai/<int:pk>/edit/", views.aicontent_edit, name="aicontent_edit"),
    path("ai/<int:pk>/schedule/", views.schedule_content, name="schedule_content"),
    # Connections (OAuth)
    path("connections/", views.connections, name="connections"),
    path("connections/youtube/connect/", views.youtube_connect, name="youtube_connect"),
    path("connections/youtube/callback/", views.youtube_callback, name="youtube_callback"),
    path("connections/youtube/disconnect/", views.youtube_disconnect, name="youtube_disconnect"),
    path("connections/instagram/connect/", views.instagram_connect, name="instagram_connect"),
    path("connections/instagram/callback/", views.instagram_callback, name="instagram_callback"),
    path("connections/instagram/disconnect/", views.instagram_disconnect, name="instagram_disconnect"),
    path("connections/linkedin/connect/", views.linkedin_connect, name="linkedin_connect"),
    path("connections/linkedin/callback/", views.linkedin_callback, name="linkedin_callback"),
    path("connections/linkedin/disconnect/", views.linkedin_disconnect, name="linkedin_disconnect"),
    # Scheduling
    path("schedule/", views.schedule_post, name="schedule_post"),
    # Liveness check: 200 only if a trivial DB query succeeds.
    path("healthz/", views.healthz, name="healthz"),
]
