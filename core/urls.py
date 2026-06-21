"""URL routes for the core app."""

from django.urls import path

from . import views

app_name = "core"

urlpatterns = [
    path("", views.home, name="home"),
    path("dashboard/", views.dashboard, name="dashboard"),
    path("calendar/", views.calendar_view, name="calendar"),
    path("analytics/", views.analytics_view, name="analytics"),
    path("storage/", views.storage, name="storage"),
    path("upload/", views.upload, name="upload"),
    path("video/<int:pk>/", views.video_detail, name="video_detail"),
    path("video/<int:pk>/delete/", views.video_delete, name="video_delete"),
    path("storage/cleanup/", views.storage_cleanup, name="storage_cleanup"),
    path("storage/delete-all/", views.storage_delete_all, name="storage_delete_all"),
    path("storage/video/<int:pk>/archive/", views.video_archive_source, name="video_archive_source"),
    path("video/<int:pk>/generate/", views.generate, name="generate"),
    path("ai/<int:pk>/edit/", views.aicontent_edit, name="aicontent_edit"),
    path("ai/<int:pk>/generate/", views.generate_ai, name="generate_ai"),
    path("ai/<int:pk>/schedule/", views.schedule_content, name="schedule_content"),
    path("video/<int:pk>/schedule-all/", views.schedule_all, name="schedule_all"),
    path("ai/<int:pk>/suggest-times/", views.suggest_times, name="suggest_times"),
    # Scheduled-post management + analytics
    path("post/<int:pk>/edit/", views.post_edit, name="post_edit"),
    path("post/<int:pk>/cancel/", views.post_cancel, name="post_cancel"),
    path("post/<int:pk>/retry/", views.post_retry, name="post_retry"),
    path("post/<int:pk>/refresh-stats/", views.post_refresh_stats, name="post_refresh_stats"),
    path("analytics/refresh/", views.refresh_stats, name="refresh_stats"),
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
    # Liveness check: 200 only if a trivial DB query succeeds.
    path("healthz/", views.healthz, name="healthz"),
    path("tasks/run-publisher/", views.run_publisher, name="run_publisher"),
]
