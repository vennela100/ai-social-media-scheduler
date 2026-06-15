"""
Admin registrations.

Security note: we deliberately do NOT expose access_token / refresh_token in
the admin. They are encrypted at rest and there is no operational reason to
view raw tokens in a browser — keeping them off the admin removes a leak path.
"""

from django.contrib import admin

from .models import AIContent, ScheduledPost, SocialAccount, Video


@admin.register(SocialAccount)
class SocialAccountAdmin(admin.ModelAdmin):
    list_display = ("user", "platform", "platform_account_id", "token_expires_at", "connected_at")
    list_filter = ("platform",)
    # Tokens are intentionally excluded from the form.
    fields = ("user", "platform", "platform_account_id", "token_expires_at", "connected_at")
    readonly_fields = ("connected_at",)


@admin.register(Video)
class VideoAdmin(admin.ModelAdmin):
    list_display = ("__str__", "user", "original_filename", "uploaded_at")
    search_fields = ("original_filename",)


@admin.register(AIContent)
class AIContentAdmin(admin.ModelAdmin):
    list_display = ("__str__", "platform", "ai_model_used", "generated_at")
    list_filter = ("platform", "ai_model_used")


@admin.register(ScheduledPost)
class ScheduledPostAdmin(admin.ModelAdmin):
    list_display = ("__str__", "status", "scheduled_time_utc", "retry_count", "updated_at")
    list_filter = ("status", "social_account__platform")
    readonly_fields = ("created_at", "updated_at", "platform_post_id")
