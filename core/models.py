"""
Core data models for the AI Social Media Scheduler.

The chain of ownership is:
    User -> Video -> AIContent (per platform)
    User -> SocialAccount (per platform, holds encrypted OAuth tokens)
    ScheduledPost ties a Video + a SocialAccount (+ optional AIContent) together
    and is the row the GitHub Actions scheduler polls.
"""

from django.conf import settings
from django.db import models

from .fields import EncryptedTextField


class Platform(models.TextChoices):
    """Supported destinations. Value is what we store; label is for the UI."""
    YOUTUBE = "youtube", "YouTube"
    INSTAGRAM = "instagram", "Instagram"
    LINKEDIN = "linkedin", "LinkedIn"


class SocialAccount(models.Model):
    """A user's authenticated connection to one platform. Tokens are encrypted."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="social_accounts",
    )
    platform = models.CharField(max_length=20, choices=Platform.choices)

    # These two columns hold Fernet ciphertext on disk; plaintext in Python.
    access_token = EncryptedTextField(blank=True, default="")
    refresh_token = EncryptedTextField(blank=True, default="")
    token_expires_at = models.DateTimeField(null=True, blank=True)

    platform_account_id = models.CharField(max_length=255, blank=True, default="")
    connected_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        # One account per platform per user keeps OAuth bookkeeping simple.
        constraints = [
            models.UniqueConstraint(
                fields=["user", "platform"], name="unique_user_platform"
            )
        ]

    def __str__(self) -> str:
        return f"{self.user} · {self.get_platform_display()}"


class Video(models.Model):
    """An uploaded source video, stored on Cloudinary (public URL)."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="videos",
    )
    file_url = models.URLField(max_length=500)
    thumbnail_url = models.URLField(max_length=500, blank=True, default="")
    original_filename = models.CharField(max_length=255, blank=True, default="")
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-uploaded_at"]

    def __str__(self) -> str:
        return self.original_filename or f"Video #{self.pk}"


class AIContent(models.Model):
    """AI-generated, platform-specific metadata for a video."""

    video = models.ForeignKey(
        Video, on_delete=models.CASCADE, related_name="ai_contents"
    )
    platform = models.CharField(max_length=20, choices=Platform.choices)
    generated_title = models.CharField(max_length=255, blank=True, default="")
    generated_description = models.TextField(blank=True, default="")
    generated_hashtags = models.TextField(blank=True, default="")  # space/comma separated
    ai_model_used = models.CharField(max_length=100, blank=True, default="")
    generated_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return f"AIContent[{self.get_platform_display()}] for {self.video}"


class ScheduledPost(models.Model):
    """A single scheduled publish job. This is the scheduler's unit of work."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        PROCESSING = "processing", "Processing"
        PUBLISHED = "published", "Published"
        FAILED = "failed", "Failed"
        NEEDS_RECONNECT = "needs_reconnect", "Needs reconnect"

    # Publishing is attempted this many times before we give up and mark FAILED.
    MAX_RETRIES = 3

    video = models.ForeignKey(
        Video, on_delete=models.CASCADE, related_name="scheduled_posts"
    )
    social_account = models.ForeignKey(
        SocialAccount, on_delete=models.CASCADE, related_name="scheduled_posts"
    )
    ai_content = models.ForeignKey(
        AIContent,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="scheduled_posts",
    )

    final_caption = models.TextField(blank=True, default="")
    scheduled_time_utc = models.DateTimeField()
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING
    )
    platform_post_id = models.CharField(max_length=255, blank=True, default="")
    retry_count = models.PositiveIntegerField(default=0)
    last_error = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["scheduled_time_utc"]
        indexes = [
            # The scheduler queries on (status, scheduled_time_utc) every 5 min;
            # this composite index makes that lookup cheap as the table grows.
            models.Index(fields=["status", "scheduled_time_utc"]),
        ]

    def __str__(self) -> str:
        return f"{self.get_status_display()} → {self.social_account} @ {self.scheduled_time_utc:%Y-%m-%d %H:%M}Z"

    def can_retry(self) -> bool:
        """True while we still have retry attempts left for this post."""
        return self.retry_count < self.MAX_RETRIES

    def next_retry_delay_seconds(self) -> int:
        """
        Return how many seconds to wait before the NEXT publish attempt, based
        on self.retry_count (0 = before the 2nd attempt, 1 = before the 3rd...).

        This shapes how the scheduler reschedules a transiently-failed post.
        Design choices worth weighing:
          - Base delay & growth factor: a 5-min cron tick means sub-minute
            delays get rounded up anyway; pick values that span useful spreads.
          - A cap: without one, exponential growth can push retries hours out.
          - Jitter: if many posts fail at once (e.g. an API blip), identical
            backoff makes them all retry in lockstep — jitter spreads the load.

        TODO(you): implement the backoff. A common shape is:
            base * (factor ** retry_count), clamped to a max, optionally + jitter.
        Replace the placeholder below.
        """
        # Placeholder so the rest of the project imports cleanly; replace with
        # your chosen backoff strategy.
        raise NotImplementedError("Implement next_retry_delay_seconds()")
