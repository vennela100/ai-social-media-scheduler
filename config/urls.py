"""Root URL configuration."""

from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path

from core import views as core_views
from core.forms import EmailLoginForm

urlpatterns = [
    path("admin/", admin.site.urls),
    # Create-account flow (must precede the auth include so it owns this path).
    path("accounts/signup/", core_views.signup, name="signup"),
    # Login with our email-based form (must precede the auth include to win).
    path(
        "accounts/login/",
        auth_views.LoginView.as_view(authentication_form=EmailLoginForm),
        name="login",
    ),
    # Built-in auth: gives us /accounts/logout/ (and password views) for free.
    path("accounts/", include("django.contrib.auth.urls")),
    # JSON API consumed by the Next.js frontend (proxied at /api/* in dev).
    path("api/", include("core.api_urls")),
    path("", include("core.urls")),
]
