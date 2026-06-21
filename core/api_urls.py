"""JSON API routes, mounted under /api/ for the Next.js frontend."""

from django.urls import path

from . import api

app_name = "api"

urlpatterns = [
    # auth
    path("auth/csrf/", api.auth_csrf, name="csrf"),
    path("auth/login/", api.auth_login, name="login"),
    path("auth/logout/", api.auth_logout, name="logout"),
    path("auth/me/", api.auth_me, name="me"),
    # reads
    path("videos/", api.videos, name="videos"),
    path("videos/<int:pk>/", api.video, name="video"),
    path("accounts/", api.accounts, name="accounts"),
    path("posts/", api.posts, name="posts"),
    path("stats/", api.stats_view, name="stats"),
    path("storage/", api.storage_view, name="storage"),
    # mutations
    path("analytics/refresh/", api.refresh_all_stats, name="refresh_all"),
    path("posts/<int:pk>/refresh/", api.post_refresh, name="post_refresh"),
    path("posts/<int:pk>/publish-now/", api.post_publish_now, name="post_publish_now"),
    path("posts/<int:pk>/cancel/", api.post_cancel, name="post_cancel"),
    path("videos/<int:pk>/delete/", api.video_delete, name="video_delete"),
    path("videos/<int:pk>/archive/", api.video_archive, name="video_archive"),
    path("storage/cleanup/", api.storage_cleanup, name="storage_cleanup"),
    path("storage/delete-all/", api.storage_delete_all, name="storage_delete_all"),
    path("upload/", api.upload, name="upload"),
    path("drafts/<int:pk>/schedule/", api.schedule_draft, name="schedule_draft"),
    path("drafts/<int:pk>/save/", api.save_draft, name="save_draft"),
    path("drafts/<int:pk>/regenerate/", api.regenerate_draft, name="regenerate_draft"),
    path("connections/<str:platform>/connect/", api.account_connect, name="account_connect"),
    path("connections/<str:platform>/disconnect/", api.account_disconnect, name="account_disconnect"),
    # Real OAuth (same-origin via the proxy). Callback bounces back to the SPA.
    path("oauth/<str:platform>/start/", api.oauth_start, name="oauth_start"),
    path("oauth/<str:platform>/callback/", api.oauth_callback, name="oauth_callback"),
]
