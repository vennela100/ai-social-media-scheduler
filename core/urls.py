"""URL routes for the core app."""

from django.urls import path

from . import views

app_name = "core"

urlpatterns = [
    path("", views.home, name="home"),
    path("dashboard/", views.dashboard, name="dashboard"),
    path("upload/", views.upload, name="upload"),
    path("video/<int:pk>/", views.video_detail, name="video_detail"),
    path("video/<int:pk>/generate/", views.generate, name="generate"),
    path("ai/<int:pk>/edit/", views.aicontent_edit, name="aicontent_edit"),
    # Liveness check: 200 only if a trivial DB query succeeds.
    path("healthz/", views.healthz, name="healthz"),
]
