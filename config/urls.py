"""Root URL configuration."""

from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    # Built-in auth: gives us /accounts/login/ and /accounts/logout/ for free.
    path("accounts/", include("django.contrib.auth.urls")),
    # JSON API consumed by the Next.js frontend (proxied at /api/* in dev).
    path("api/", include("core.api_urls")),
    path("", include("core.urls")),
]
