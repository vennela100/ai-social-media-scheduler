"""Root URL configuration."""

from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    # Built-in auth: gives us /accounts/login/ and /accounts/logout/ for free.
    path("accounts/", include("django.contrib.auth.urls")),
    path("", include("core.urls")),
]
