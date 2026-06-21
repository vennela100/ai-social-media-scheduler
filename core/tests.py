import datetime as dt
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from . import instagram
from .models import AIContent, Platform, ScheduledPost, SocialAccount, Video


TEST_FERNET_KEY = "wUzBpLYlGyqfcbtNFHoTcL5Txj4YYllXatTpvFg84bo="


@override_settings(
    ALLOWED_HOSTS=["testserver"],
    TOKEN_ENCRYPTION_KEY=TEST_FERNET_KEY,
    ROOT_URLCONF="config.urls",
    STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    },
)
class ScheduledPostDashboardTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="creator",
            password="pass12345",
        )
        self.client.force_login(self.user)
        self.video = Video.objects.create(
            user=self.user,
            file_url="https://example.com/video.mp4",
            original_filename="demo.mp4",
        )
        self.content = AIContent.objects.create(
            video=self.video,
            platform=Platform.INSTAGRAM,
            generated_title="Hook",
            generated_description="Caption",
            generated_hashtags="#demo",
        )
        self.account = SocialAccount.objects.create(
            user=self.user,
            platform=Platform.INSTAGRAM,
            access_token="token",
            platform_account_id="ig-user",
            token_expires_at=timezone.now() + dt.timedelta(days=20),
        )

    def _post(self, **overrides):
        data = {
            "video": self.video,
            "social_account": self.account,
            "ai_content": self.content,
            "final_caption": "Caption\n\n#demo",
            "scheduled_time_utc": timezone.now() + dt.timedelta(days=1),
            "visibility": ScheduledPost.Visibility.PUBLIC,
            "status": ScheduledPost.Status.PENDING,
        }
        data.update(overrides)
        return ScheduledPost.objects.create(**data)

    def test_instagram_edit_reschedules_and_resets_failed_status(self):
        post = self._post(
            status=ScheduledPost.Status.FAILED,
            retry_count=2,
            last_error="temporary failure",
        )
        target = timezone.localtime(timezone.now() + dt.timedelta(days=2))

        response = self.client.post(
            reverse("core:post_edit", args=[post.pk]),
            {
                "visibility": "private",  # Instagram should be clamped public.
                "sched_date": target.strftime("%Y-%m-%d"),
                "sched_hour": target.strftime("%I").lstrip("0"),
                "sched_minute": "00",
                "sched_ampm": target.strftime("%p"),
            },
        )

        self.assertRedirects(response, reverse("core:dashboard"))
        post.refresh_from_db()
        self.assertEqual(post.status, ScheduledPost.Status.PENDING)
        self.assertEqual(post.retry_count, 0)
        self.assertEqual(post.last_error, "")
        self.assertEqual(post.visibility, ScheduledPost.Visibility.PUBLIC)

    @patch("core.views.analytics.refresh_post")
    def test_refresh_stats_updates_only_selected_post(self, refresh_post):
        post = self._post(
            status=ScheduledPost.Status.PUBLISHED,
            platform_post_id="ig-media-1",
        )
        other = self._post(
            status=ScheduledPost.Status.PUBLISHED,
            platform_post_id="ig-media-2",
        )

        def fake_refresh(p, *, force):
            self.assertTrue(force)
            p.stat_likes = 12
            p.stat_comments = 3
            p.stats_updated_at = timezone.now()
            p.save(update_fields=["stat_likes", "stat_comments", "stats_updated_at"])
            return True

        refresh_post.side_effect = fake_refresh

        response = self.client.post(reverse("core:post_refresh_stats", args=[post.pk]))

        self.assertRedirects(response, reverse("core:dashboard"))
        refresh_post.assert_called_once()
        self.assertEqual(refresh_post.call_args.args[0].pk, post.pk)
        post.refresh_from_db()
        other.refresh_from_db()
        self.assertEqual(post.stat_likes, 12)
        self.assertIsNone(other.stat_likes)

    @patch("core.instagram.requests.get")
    def test_instagram_fetch_stats_reads_views_metric(self, get):
        class Response:
            ok = True
            content = b"{}"

            def __init__(self, data):
                self._data = data

            def json(self):
                return self._data

        get.side_effect = [
            Response({"like_count": 7, "comments_count": 0}),
            Response({"data": [{"name": "views", "values": [{"value": 162}]}]}),
        ]

        stats = instagram.fetch_stats(self.account, "ig-media-1")

        self.assertEqual(stats, {"views": 162, "likes": 7, "comments": 0})
        self.assertEqual(get.call_args_list[1].kwargs["params"]["metric"], "views")

    @patch("core.views.ai.is_configured", return_value=True)
    @patch("core.views.ai.suggest_post_times", side_effect=RuntimeError("quota exceeded"))
    def test_suggest_times_falls_back_when_gemini_fails(self, suggest_post_times, is_configured):
        response = self.client.post(reverse("core:suggest_times", args=[self.content.pk]))

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["ok"])
        self.assertEqual(len(data["slots"]), 3)
        self.assertEqual(data["slots"][0]["day"], "Tuesday")
        self.assertIn("temporarily unavailable", data["notice"])
