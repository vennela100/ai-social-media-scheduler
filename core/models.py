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
from django.utils import timezone

from .fields import EncryptedTextField

# Platforms whose tokens auto-renew via a refresh token (Google/YouTube). We never
# warn about their expiry â€” the publisher refreshes silently before each publish.
AUTO_REFRESH_PLATFORMS = {"youtube"}


class Platform(models.TextChoices):
    """Supported destinations. Value is what we store; label is for the UI."""
    YOUTUBE = "youtube", "YouTube"
    INSTAGRAM = "instagram", "Instagram"
    LINKEDIN = "linkedin", "LinkedIn"


class SocialAccount(models.Model):
    """A user's authenticated connection to one platform. Tokens are encrypted."""

    class Status(models.TextChoices):
        CONNECTED = "connected", "Connected"
        EXPIRED = "expired", "Expired"
        NEEDS_RECONNECT = "needs_reconnect", "Needs reconnect"

    # How many days of remaining token life map to each warning level.
    WARN_DAYS = 14   # good at/above this
    URGENT_DAYS = 7  # urgent below this

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
    # When we last sent an expiry reminder (Telegram), to throttle to once/24h.
    last_reminder_sent_at = models.DateTimeField(null=True, blank=True)
    # Lifecycle flag, separate from the date-derived expiry_status(): set to
    # EXPIRED/NEEDS_RECONNECT when a publish or refresh proves the token is dead.
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.CONNECTED
    )

    class Meta:
        # One account per platform per user keeps OAuth bookkeeping simple.
        constraints = [
            models.UniqueConstraint(
                fields=["user", "platform"], name="unique_user_platform"
            )
        ]

    def __str__(self) -> str:
        return f"{self.user} Â· {self.get_platform_display()}"

    # --- Token expiry helpers ---

    def auto_refreshes(self) -> bool:
        """True for platforms that renew their own token (YouTube)."""
        return self.platform in AUTO_REFRESH_PLATFORMS

    def days_until_expiry(self):
        """Whole days until the token expires (negative if past). None if unknown."""
        if not self.token_expires_at:
            return None
        return (self.token_expires_at - timezone.now()).days

    def is_expired(self) -> bool:
        """True if we know an expiry and it's in the past."""
        return bool(self.token_expires_at and self.token_expires_at <= timezone.now())

    def expiry_status(self) -> str:
        """Date-derived severity: good (14+d) / warning (7-14d) / urgent (<7d) / expired."""
        if self.token_expires_at is None:
            return "good"  # unknown expiry â€” don't nag
        if self.is_expired():
            return "expired"
        days = self.days_until_expiry()
        if days < self.URGENT_DAYS:
            return "urgent"
        if days < self.WARN_DAYS:
            return "warning"
        return "good"

    def health_level(self) -> str:
        """What the UI should show: auto / good / warning / urgent / expired.

        YouTube is always "auto" (silently refreshed) unless its refresh token was
        revoked (status flipped to expired/needs_reconnect). A persisted bad status
        on any platform forces "expired" regardless of dates.
        """
        bad = (self.Status.EXPIRED, self.Status.NEEDS_RECONNECT)
        if self.auto_refreshes():
            return "expired" if self.status in bad else "auto"
        if self.status in bad:
            return "expired"
        return self.expiry_status()


class Video(models.Model):
    """An uploaded source asset (video or image), stored on Cloudinary (public URL).

    The model keeps the name `Video` for historical reasons â€” most uploads are
    videos â€” but `media_type` lets it also hold a still image (e.g. a certificate
    to share on LinkedIn). The type drives the Cloudinary resource_type and the
    LinkedIn publish recipe.
    """

    class MediaType(models.TextChoices):
        VIDEO = "video", "Video"
        IMAGE = "image", "Image"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="videos",
    )
    media_type = models.CharField(
        max_length=10, choices=MediaType.choices, default=MediaType.VIDEO
    )
    file_url = models.URLField(max_length=500)
    thumbnail_url = models.URLField(max_length=500, blank=True, default="")
    original_filename = models.CharField(max_length=255, blank=True, default="")
    source_size_bytes = models.PositiveBigIntegerField(default=0)
    # Cloudinary's public_id for this asset â€” kept so we can delete the remote
    # file when the user deletes the video (the URL alone is awkward to reverse).
    cloudinary_public_id = models.CharField(max_length=300, blank=True, default="")
    # Once every scheduled post for this video has published, the heavy source
    # file is redundant (the platforms host their own copies), so the scheduler
    # archives it: deletes the Cloudinary source, keeps only a small thumbnail.
    source_deleted = models.BooleanField(default=False)
    thumbnail_public_id = models.CharField(max_length=300, blank=True, default="")
    # The user's own words about the video. These are the PRIMARY context the AI
    # writes from (filename is only a fallback), and they're kept so a draft can
    # be regenerated later without re-typing.
    user_title = models.CharField(max_length=255, blank=True, default="")
    user_description = models.TextField(blank=True, default="")
    # A short topic/niche (e.g. "fitness", "tech tutorials") that steers the AI's
    # SEO/keyword choices. Fed into every Gemini prompt as `category`.
    category = models.CharField(max_length=100, blank=True, default="")
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-uploaded_at"]

    def __str__(self) -> str:
        return self.original_filename or f"Video #{self.pk}"

    def is_fully_published(self) -> bool:
        """True if this video has scheduled posts and every one has published.

        Used to decide when the source file is safe to archive: if anything is
        still pending/processing/failed/awaiting-reconnect, the source may yet be
        needed for a (re)publish, so we keep it.
        """
        posts = self.scheduled_posts.all()
        return bool(posts) and all(
            p.status == ScheduledPost.Status.PUBLISHED for p in posts
        )


class AIContent(models.Model):
    """AI-generated, platform-specific metadata for a video."""

    class GenStatus(models.TextChoices):
        PENDING = "pending", "Generating"
        DONE = "done", "Ready"
        FAILED = "failed", "Failed"

    video = models.ForeignKey(
        Video, on_delete=models.CASCADE, related_name="ai_contents"
    )
    platform = models.CharField(max_length=20, choices=Platform.choices)
    generated_title = models.CharField(max_length=255, blank=True, default="")
    generated_description = models.TextField(blank=True, default="")
    generated_hashtags = models.TextField(blank=True, default="")  # space/comma separated
    ai_model_used = models.CharField(max_length=100, blank=True, default="")
    # Lifecycle for the async generation UX: rows start PENDING when a video is
    # uploaded, the browser fires a generate request per platform, and they flip
    # to DONE (or FAILED). Existing rows default to DONE.
    generation_status = models.CharField(
        max_length=10, choices=GenStatus.choices, default=GenStatus.DONE
    )
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

    class Visibility(models.TextChoices):
        # Who can see the post once published. Interpreted per platform:
        #   YouTube  â†’ privacyStatus public / unlisted / private
        #   LinkedIn â†’ PUBLIC, or CONNECTIONS-only for unlisted/private
        #   Instagramâ†’ always public (the API can't publish private reels)
        PUBLIC = "public", "Public"
        UNLISTED = "unlisted", "Unlisted"
        PRIVATE = "private", "Private"

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
    visibility = models.CharField(
        max_length=10, choices=Visibility.choices, default=Visibility.PUBLIC
    )
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING
    )
    platform_post_id = models.CharField(max_length=255, blank=True, default="")
    retry_count = models.PositiveIntegerField(default=0)
    last_error = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # Engagement pulled back from the platform after publishing (analytics). Null
    # means "not fetched yet" OR "this platform can't report this metric" (e.g.
    # LinkedIn member posts expose no view count via the self-serve API), so the
    # UI shows "â€”" rather than a misleading 0. stats_updated_at gates re-fetching.
    stat_views = models.PositiveIntegerField(null=True, blank=True)
    stat_likes = models.PositiveIntegerField(null=True, blank=True)
    stat_comments = models.PositiveIntegerField(null=True, blank=True)
    stats_updated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["scheduled_time_utc"]
        indexes = [
            # The scheduler queries on (status, scheduled_time_utc) every 5 min;
            # this composite index makes that lookup cheap as the table grows.
            models.Index(fields=["status", "scheduled_time_utc"]),
        ]

    def __str__(self) -> str:
        return f"{self.get_status_display()} â†’ {self.social_account} @ {self.scheduled_time_utc:%Y-%m-%d %H:%M}Z"

    def can_retry(self) -> bool:
        """True while we still have retry attempts left for this post."""
        return self.retry_count < self.MAX_RETRIES

    def is_editable(self) -> bool:
        """True if the user can still change or cancel this post.

        Anything not yet handed to a platform is fair game: pending, paused for
        reconnect, or permanently failed. PROCESSING is mid-publish (don't touch)
        and PUBLISHED is already live (can't unsend).
        """
        return self.status in (
            self.Status.PENDING, self.Status.NEEDS_RECONNECT, self.Status.FAILED
        )

    def has_stats(self) -> bool:
        """True once any engagement metric has been pulled back from the platform."""
        return self.stats_updated_at is not None and any(
            v is not None for v in (self.stat_views, self.stat_likes, self.stat_comments)
        )

    def next_retry_delay_seconds(self) -> int:
        """
        Return how many seconds to wait before the NEXT publish attempt.

        self.retry_count here is the number of attempts that have ALREADY failed
        (1 after the first failure, 2 after the second, ...). The scheduler waits
        this long after the last failure before trying again.

        Design choices worth weighing:
          - Base delay & growth factor: a 5-min cron tick means sub-minute
            delays get rounded up anyway; pick values that span useful spreads.
          - A cap: without one, exponential growth can push retries hours out.
          - Jitter: if many posts fail at once (e.g. an API blip), identical
            backoff makes them all retry in lockstep â€” jitter spreads the load.

        TODO(you): implement the backoff. A common shape is:
            base * (factor ** (retry_count - 1)), clamped to a max, +/- jitter.
        Until you do, the scheduler falls back to "retry on the next tick"
        (no backoff) and logs a warning â€” so it works, just not optimally.
        """
        import random

        base = 300        # 5 minutes
        factor = 2
        cap = 3600        # never wait more than an hour
        attempts = max(self.retry_count, 1)
        delay = min(base * (factor ** (attempts - 1)), cap)
        jitter = delay * 0.1
        return int(delay + random.uniform(-jitter, jitter))


class StatSnapshot(models.Model):
    """A point-in-time engagement reading for a published post.

    One row is appended each time stats are refreshed, so the analytics page can
    chart views/likes/comments growth over time (the ScheduledPost row only holds
    the latest values).
    """

    post = models.ForeignKey(
        ScheduledPost, on_delete=models.CASCADE, related_name="stat_snapshots"
    )
    views = models.PositiveIntegerField(null=True, blank=True)
    likes = models.PositiveIntegerField(null=True, blank=True)
    comments = models.PositiveIntegerField(null=True, blank=True)
    captured_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["captured_at"]
        indexes = [models.Index(fields=["post", "captured_at"])]

    def __str__(self) -> str:
        return f"snapshot post={self.post_id} views={self.views} @ {self.captured_at:%Y-%m-%d %H:%M}"
