"""
Seed a rich demo workspace so the frontend looks alive on first run.

Idempotent: wipes the demo user's content and rebuilds it. Mirrors the mock data
the frontend was designed against (studio rebuild, NPTEL cert, 5am routine, AI
tools roundup) so every dashboard state — published, scheduled, failed,
needs-reconnect — is represented.

    python manage.py seed_demo
    login: vennela / password
"""

import datetime as dt

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.utils import timezone

from core.models import AIContent, ScheduledPost, SocialAccount, Video

User = get_user_model()

USERNAME = "vennela"
PASSWORD = "password"


def hours(h):
    return timezone.now() + dt.timedelta(hours=h)


def days(d):
    return timezone.now() + dt.timedelta(days=d)


class Command(BaseCommand):
    help = "Create/refresh the demo creator workspace (user: vennela / password)."

    def handle(self, *args, **opts):
        user, created = User.objects.get_or_create(
            username=USERNAME, defaults={"email": "vennela@example.com"}
        )
        user.set_password(PASSWORD)
        user.save()

        # Clean slate for the demo user.
        Video.objects.filter(user=user).delete()
        SocialAccount.objects.filter(user=user).delete()

        # ── Connected channels (one of each health state) ───────────────────
        SocialAccount.objects.create(
            user=user, platform="youtube",
            platform_account_id="@codewithvennela",
            access_token="demo", token_expires_at=None,
            status=SocialAccount.Status.CONNECTED,
        )
        SocialAccount.objects.create(
            user=user, platform="instagram",
            platform_account_id="@vennelas_tech_life",
            access_token="demo", token_expires_at=days(-1),
            status=SocialAccount.Status.NEEDS_RECONNECT,
        )
        SocialAccount.objects.create(
            user=user, platform="linkedin",
            platform_account_id="Vennela A.",
            access_token="demo", token_expires_at=days(6),
            status=SocialAccount.Status.CONNECTED,
        )
        accounts = {a.platform: a for a in SocialAccount.objects.filter(user=user)}

        S = ScheduledPost.Status
        V = ScheduledPost.Visibility

        # ── Video 1: studio rebuild (scheduled) ─────────────────────────────
        v1 = Video.objects.create(
            user=user, media_type="video",
            file_url="https://example.com/studio-tour-final.mp4",
            original_filename="studio-tour-final.mp4",
            source_size_bytes=184_320_000,
            cloudinary_public_id="demo/studio",
            user_title="I rebuilt my home studio in a weekend",
            user_description="A fast walkthrough of the new desk setup, lighting, and the cable management nobody asked for.",
            category="Tech",
        )
        yt1 = AIContent.objects.create(
            video=v1, platform="youtube",
            generated_title="I Rebuilt My Home Studio in a Weekend (Budget Setup)",
            generated_description="Full breakdown of my new creator desk — lighting, audio, and the $40 cable trick that changed everything. Timestamps below.",
            generated_hashtags="studio, desksetup, creator, tech",
        )
        ig1 = AIContent.objects.create(
            video=v1, platform="instagram",
            generated_title="the weekend the desk won",
            generated_description="POV: you said 'quick refresh' and lost a whole weekend to cable management. Worth it though 🔌✨",
            generated_hashtags="#desksetup #studio #creatorlife #behindthescenes #aesthetic",
        )
        AIContent.objects.create(
            video=v1, platform="linkedin",
            generated_title="What a weekend studio rebuild taught me about focus",
            generated_description="I spent the weekend rebuilding my recording space. The real upgrade wasn't the gear — it was removing friction. Three takeaways for anyone building in public.",
            generated_hashtags="#creators #productivity #buildinpublic",
        )
        ScheduledPost.objects.create(
            video=v1, social_account=accounts["youtube"], ai_content=yt1,
            final_caption="Full studio breakdown — link in description.",
            scheduled_time_utc=hours(20), visibility=V.PUBLIC, status=S.PENDING,
        )
        ScheduledPost.objects.create(
            video=v1, social_account=accounts["instagram"], ai_content=ig1,
            final_caption="the weekend the desk won 🔌",
            scheduled_time_utc=days(1), visibility=V.PUBLIC, status=S.PENDING,
        )

        # ── Video 2: NPTEL certificate image (published) ────────────────────
        v2 = Video.objects.create(
            user=user, media_type="image",
            file_url="https://example.com/nptel-certificate.png",
            thumbnail_url="https://example.com/nptel-thumb.png",
            original_filename="nptel-certificate.png",
            source_size_bytes=0, source_deleted=True,
            user_title="Completed: Cloud Computing (NPTEL)",
            user_description="Sharing my certificate from the NPTEL cloud computing course.",
            category="Education",
        )
        li2 = AIContent.objects.create(
            video=v2, platform="linkedin",
            generated_title="Completed the NPTEL Cloud Computing certification",
            generated_description="Excited to share that I've completed the NPTEL Cloud Computing course. Grateful for the structured curriculum on distributed systems and virtualization. On to applying it.",
            generated_hashtags="#cloudcomputing #nptel #learning",
        )
        # Published but stats left null — real engagement is only ever pulled
        # back from the platform after a real publish, never fabricated.
        ScheduledPost.objects.create(
            video=v2, social_account=accounts["linkedin"], ai_content=li2,
            final_caption="Completed the NPTEL Cloud Computing certification 🎓",
            scheduled_time_utc=days(-3), visibility=V.PUBLIC, status=S.PUBLISHED,
        )

        # ── Video 3: 5am routine (failed + needs_reconnect) ─────────────────
        v3 = Video.objects.create(
            user=user, media_type="video",
            file_url="https://example.com/morning-routine.mp4",
            original_filename="morning-routine-b-roll.mp4",
            source_size_bytes=240_500_000,
            cloudinary_public_id="demo/morning",
            user_title="5am routine (real one)",
            user_description="B-roll heavy morning routine — coffee, journal, gym, deep work block.",
            category="Lifestyle",
        )
        yt3 = AIContent.objects.create(
            video=v3, platform="youtube",
            generated_title="My Realistic 5AM Morning Routine",
            generated_description="No fluff. The exact blocks that make my mornings work.",
            generated_hashtags="morningroutine, productivity, 5am, vlog",
        )
        AIContent.objects.create(
            video=v3, platform="instagram",
            generated_title="5am but make it sustainable",
            generated_description="", generated_hashtags="",
            generation_status=AIContent.GenStatus.PENDING,
        )
        ScheduledPost.objects.create(
            video=v3, social_account=accounts["youtube"], ai_content=yt3,
            final_caption="My realistic 5AM routine.",
            scheduled_time_utc=hours(-6), visibility=V.PUBLIC, status=S.FAILED,
            last_error="Upload quota exceeded — will retry on next run.",
            retry_count=2,
        )
        ScheduledPost.objects.create(
            video=v3, social_account=accounts["instagram"],
            final_caption="5am but make it sustainable",
            scheduled_time_utc=hours(-2), visibility=V.PUBLIC,
            status=S.NEEDS_RECONNECT,
            last_error="Instagram token expired. Reconnect to resume.",
        )

        # ── Video 4: AI tools roundup (published, archived) ─────────────────
        v4 = Video.objects.create(
            user=user, media_type="video",
            file_url="https://example.com/ai-tools.mp4",
            thumbnail_url="https://example.com/ai-tools-thumb.png",
            original_filename="ai-tools-roundup.mp4",
            source_size_bytes=0, source_deleted=True,
            user_title="7 AI tools I actually use",
            user_description="Honest roundup of the AI tools that survived past week one.",
            category="Tech",
        )
        yt4 = AIContent.objects.create(
            video=v4, platform="youtube",
            generated_title="7 AI Tools I Actually Use Every Day",
            generated_description="The ones that stuck. Ranked by how often I open them.",
            generated_hashtags="ai, tools, productivity, tech",
        )
        li4 = AIContent.objects.create(
            video=v4, platform="linkedin",
            generated_title="The 7 AI tools that survived past week one",
            generated_description="A short, honest list of the AI tools I keep coming back to — and the ones I dropped.",
            generated_hashtags="#ai #productivity #tools",
        )
        ScheduledPost.objects.create(
            video=v4, social_account=accounts["youtube"], ai_content=yt4,
            final_caption="7 AI tools I actually use every day.",
            scheduled_time_utc=days(-8), visibility=V.PUBLIC, status=S.PUBLISHED,
        )
        ScheduledPost.objects.create(
            video=v4, social_account=accounts["linkedin"], ai_content=li4,
            final_caption="The 7 AI tools that survived past week one.",
            scheduled_time_utc=days(-8), visibility=V.PUBLIC, status=S.PUBLISHED,
        )

        self.stdout.write(self.style.SUCCESS(
            f"Seeded demo workspace for '{USERNAME}' (password: {PASSWORD}) — "
            f"{Video.objects.filter(user=user).count()} videos, "
            f"{ScheduledPost.objects.filter(video__user=user).count()} posts."
        ))
