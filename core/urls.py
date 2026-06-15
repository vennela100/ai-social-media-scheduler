"""URL routes for the core app."""

from django.urls import path

from . import views

app_name = "core"

urlpatterns = [
    path("", views.home, name="home"),
    path("dashboard/", views.dashboard, name="dashboard"),
    path("upload/", views.upload, name="upload"),
    # Liveness check: 200 only if a trivial DB query succeeds.
    path("healthz/", views.healthz, name="healthz"),
]
