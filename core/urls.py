"""URL routes for the core app."""

from django.urls import path

from . import views

app_name = "core"

urlpatterns = [
    path("", views.home, name="home"),
    # Lightweight liveness check Render (and you) can hit to confirm the app +
    # database are reachable. Returns 200 only if a trivial DB query succeeds.
    path("healthz/", views.healthz, name="healthz"),
]
