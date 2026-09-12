import datetime as dt
import threading
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from . import instagram, youtube
from .models import AIContent, Platform, ScheduledPost, SocialAccount, Video


TEST_FERNET_KEY = "wUzBpLYlGyqfcbtNFHoTcL5Txj4YYllXatTpvFg84bo="


@override_settings(
    DEBUG=False,
    ALLOWED_HOSTS=["testserver"],
    CLOUDINARY_URL="",
    GEMINI_API_KEY="",
)
class UploadFlowTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        static_dir = TemporaryDirectory()
        cls.addClassCleanup(static_dir.cleanup)
        settings_override = override_settings(STATIC_ROOT=static_dir.name)
        settings_override.enable()
        cls.addClassCleanup(settings_override.disable)
        call_command("collectstatic", interactive=False, verbosity=0)

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="creator@example.com", password="test-password-123"
        )
        self.client.force_login(self.user)

    def upload(self, **overrides):
        data = {
            "video": SimpleUploadedFile("demo.mp4", b"test video", content_type="video/mp4"),
            "title": "Demo",
            "category": "Education",
            "description": "A tutorial about testing uploads.",
            "platforms": [Platform.YOUTUBE],
        }
        data.update(overrides)
        return self.client.post(reverse("core:upload"), data, follow=True)

    def test_login_with_csrf_redirects_to_dashboard(self):
        client = Client(enforce_csrf_checks=True)
        self.assertEqual(client.get(reverse("login")).status_code, 200)
        response = client.post(reverse("login"), {
            "username": "CREATOR@example.com",
            "password": "test-password-123",
            "csrfmiddlewaretoken": client.cookies["csrftoken"].value,
        }, follow=True)
        self.assertRedirects(response, reverse("core:dashboard"))

    @patch("core.views.upload_media")
    def test_overlong_fields_are_rejected_before_upload(self, upload_media):
        for field, limit in (("title", 255), ("category", 100)):
            with self.subTest(field=field):
                response = self.upload(**{field: "x" * (limit + 1)})
                self.assertEqual(response.status_code, 200)
                self.assertIn(field, response.context["form"].errors)
                self.assertContains(response, f"at most {limit} characters")
        upload_media.assert_not_called()
        self.assertFalse(Video.objects.exists())

    @patch("core.views.upload_media", side_effect=RuntimeError("storage unavailable"))
    def test_storage_failure_renders_error_without_creating_video(self, upload_media):
        response = self.upload()
        self.assertContains(response, "Upload failed. Please try again.")
        self.assertFalse(Video.objects.exists())

    @patch("core.views.upload_media")
    def test_upload_review_and_generation(self, upload_media):
        upload_media.return_value = {
            "file_url": "https://example.com/demo.mp4",
            "thumbnail_url": "https://example.com/demo.jpg",
            "original_filename": "demo.mp4",
            "public_id": "demo",
        }
        response = self.upload(title="x" * 255, category="x" * 100)
        video = Video.objects.get(user=self.user)
        self.assertRedirects(response, reverse("core:video_detail", args=[video.pk]))
        draft = video.ai_contents.get()
        self.assertEqual(draft.generation_status, AIContent.GenStatus.PENDING)

        metadata = {"title": "Generated title", "description": "Generated caption",
                    "hashtags": "education", "model": "test-model"}
        with override_settings(GEMINI_API_KEY="test-key"):
            with patch("core.ai.generate_metadata", return_value=metadata):
                response = self.client.post(reverse("core:generate_ai", args=[draft.pk]))
            self.assertTrue(response.json()["ok"])
            self.assertEqual(response.json()["title"], "Generated title")
            with patch("core.ai.generate_metadata", side_effect=RuntimeError("quota exceeded")):
                response = self.client.post(reverse("core:generate_ai", args=[draft.pk]))
            self.assertTrue(response.json()["ok"])
            self.assertIn("temporarily unavailable", response.json()["notice"])
        draft.refresh_from_db()
        self.assertEqual(draft.generation_status, AIContent.GenStatus.DONE)
        self.assertEqual(draft.ai_model_used, "fallback")


@override_settings(ALLOWED_HOSTS=["testserver"], GEMINI_API_KEY="test-key")
class MediaAnalysisFlowTests(TestCase):
    def setUp(self):
        slot_patch = patch("core.media_analysis._slot", threading.BoundedSemaphore(1))
        slot_patch.start()
        self.addCleanup(slot_patch.stop)
        self.user = get_user_model().objects.create_user(username="analysis-test")
        self.client.force_login(self.user)
        self.video = Video.objects.create(
            user=self.user, file_url="https://example.com/clip.mp4",
            original_filename="clip.mp4", source_size_bytes=1000,
        )
        self.draft = AIContent.objects.create(video=self.video, platform=Platform.YOUTUBE)
        self.url = reverse("core:analyze_media", args=[self.video.pk])

    @patch("core.media_analysis.threading.Thread")
    def test_analysis_starts_once_and_polls_cached_result(self, thread):
        from . import media_analysis

        response = self.client.post(self.url, {"retry": "1"})
        self.assertEqual(response.json(), {"ok": True, "status": "processing"})
        self.assertEqual(self.client.post(self.url).json()["status"], "processing")
        thread.assert_called_once()
        self.video.refresh_from_db()
        self.assertIsNotNone(self.video.ai_analysis_started_at)

        observed = "A chef slices tomatoes and adds them to a pan."
        def analyze(video):
            Video.objects.filter(pk=video.pk).update(ai_media_analysis=observed)
            return observed

        with patch("core.media_analysis.ai.analyze_media", side_effect=analyze), \
                patch("core.media_analysis.close_old_connections"), \
                patch("core.media_analysis.connections.close_all"):
            media_analysis._run(*thread.call_args.kwargs["args"])
        response = self.client.post(self.url)
        self.assertEqual(response.json()["analysis"], observed)
        self.assertEqual(response.json()["status"], "done")
        with patch("core.ai.generate_metadata", return_value={
            "title": "Cooking tomatoes", "description": "A chef prepares tomatoes.",
            "hashtags": "cooking", "model": "test-model",
        }) as generate:
            response = self.client.post(reverse("core:generate_ai", args=[self.draft.pk]))
        self.assertTrue(response.json()["ok"])
        self.assertEqual(response.json()["notice"], "")
        self.assertEqual(generate.call_args.kwargs["media_analysis"], observed)

    @patch("core.ai.generate_metadata")
    def test_filename_alone_does_not_generate_a_draft(self, generate):
        response = self.client.post(reverse("core:generate_ai", args=[self.draft.pk]))
        self.assertFalse(response.json()["ok"])
        self.assertIn("analysis is required", response.json()["error"])
        generate.assert_not_called()

    @patch("core.media_analysis.start", return_value="processing")
    def test_failed_analysis_waits_for_explicit_retry(self, start):
        self.video.ai_analysis_status = Video.AnalysisStatus.FAILED
        self.video.save()
        self.assertFalse(self.client.post(self.url).json()["ok"])
        start.assert_not_called()
        self.assertEqual(self.client.post(self.url, {"retry": "1"}).json()["status"], "processing")
        start.assert_called_once()

    @patch("core.media_analysis.start")
    def test_analysis_limit_is_reported_instead_of_silent_fallback(self, start):
        with patch("core.ai.MEDIA_ANALYSIS_MAX_BYTES", 100):
            response = self.client.post(self.url)
        self.assertEqual(response.json()["status"], "skipped")
        self.assertFalse(response.json()["ok"])
        self.assertIn("cap", response.json()["error"])
        start.assert_not_called()

    @patch("core.media_analysis.threading.Thread")
    def test_interrupted_job_can_be_reclaimed(self, thread):
        from . import media_analysis

        self.video.ai_analysis_status = Video.AnalysisStatus.PROCESSING
        self.video.ai_analysis_started_at = timezone.now() - dt.timedelta(hours=1)
        self.video.save()
        try:
            self.assertEqual(self.client.post(self.url).json()["status"], "processing")
            thread.assert_called_once()
            self.video.refresh_from_db()
            self.assertGreater(self.video.ai_analysis_started_at, timezone.now() - dt.timedelta(minutes=1))
        finally:
            if thread.called:
                media_analysis._slot.release()

    @patch("core.media_analysis.start")
    def test_other_users_cannot_analyze_video(self, start):
        other = get_user_model().objects.create_user(username="other-analysis-test")
        self.client.force_login(other)
        self.assertEqual(self.client.post(self.url).status_code, 404)
        start.assert_not_called()


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

    @patch("core.instagram.time.sleep")
    @patch("core.instagram._resolve_media_id", return_value="ig-media-1")
    @patch("core.instagram.requests.get")
    @patch("core.instagram.requests.post")
    def test_instagram_publish_retries_without_cover_url(self, post, get, resolve_media_id, sleep):
        class Response:
            content = b"{}"

            def __init__(self, ok=True, data=None, status_code=200):
                self.ok = ok
                self._data = data or {}
                self.status_code = status_code
                self.text = "bad cover"

            def json(self):
                return self._data

        responses = [
            Response(False, {"error": {"message": "Unsupported cover_url"}}, 400),
            Response(True, {"id": "container-1"}),
            Response(True, {"id": "published-1"}),
        ]
        posted_payloads = []

        def capture_post(*args, **kwargs):
            posted_payloads.append(dict(kwargs["data"]))
            return responses.pop(0)

        post.side_effect = capture_post
        get.return_value = Response(True, {"status_code": "FINISHED"})

        media_id = instagram.publish(
            self.account,
            video_url="https://example.com/video.mp4",
            caption="Caption",
            cover_url="https://example.com/thumb.jpg",
        )

        self.assertEqual(media_id, "ig-media-1")
        self.assertIn("cover_url", posted_payloads[0])
        self.assertNotIn("cover_url", posted_payloads[1])

    @patch("core.youtube.requests.get")
    def test_youtube_set_thumbnail_uploads_image(self, get):
        class Response:
            content = b"jpg-bytes"
            headers = {"Content-Type": "image/jpeg"}

            def raise_for_status(self):
                return None

        class ThumbSet:
            def __init__(self):
                self.called = False

            def set(self, **kwargs):
                self.kwargs = kwargs
                return self

            def execute(self):
                self.called = True
                return {}

        class Service:
            def __init__(self):
                self.thumb = ThumbSet()

            def thumbnails(self):
                return self.thumb

        get.return_value = Response()
        service = Service()

        youtube.set_thumbnail(service, "yt123", "https://example.com/thumb.jpg")

        get.assert_called_once_with("https://example.com/thumb.jpg", timeout=60)
        self.assertEqual(service.thumb.kwargs["videoId"], "yt123")
        self.assertTrue(service.thumb.called)

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


R2_TEST_ENV = {
    "R2_ACCOUNT_ID": "test-acct",
    "R2_ACCESS_KEY_ID": "test-key",
    "R2_SECRET_ACCESS_KEY": "test-secret",
    "R2_BUCKET": "test-bucket",
    "R2_PUBLIC_BASE_URL": "https://pub-test.r2.dev",
}


@override_settings(
    ALLOWED_HOSTS=["testserver"],
    TOKEN_ENCRYPTION_KEY=TEST_FERNET_KEY,
    ROOT_URLCONF="config.urls",
    STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    },
)
class R2DirectUploadTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="creator", password="pass12345"
        )
        self.client.force_login(self.user)

    def test_presign_says_unconfigured_without_env(self):
        response = self.client.post(
            reverse("core:upload_presign"),
            {"filename": "big.mp4", "size": "1000", "content_type": "video/mp4"},
        )
        data = response.json()
        self.assertFalse(data["ok"])
        self.assertIn("isn't configured", data["error"])

    @patch.dict("os.environ", R2_TEST_ENV)
    @patch("core.r2.presign_put", return_value="https://signed.example/put")
    def test_presign_returns_scoped_key_and_url(self, presign_put):
        response = self.client.post(
            reverse("core:upload_presign"),
            {"filename": "my video!.mp4", "size": "1000", "content_type": "video/mp4"},
        )
        data = response.json()
        self.assertTrue(data["ok"])
        self.assertTrue(data["key"].startswith(f"videos/{self.user.id}/"))
        self.assertTrue(data["key"].endswith(".mp4"))
        self.assertEqual(data["upload_url"], "https://signed.example/put")

    @patch.dict("os.environ", R2_TEST_ENV)
    def test_presign_rejects_images_and_oversize(self):
        for payload in (
            {"filename": "cert.png", "size": "1000"},                       # not a video
            {"filename": "huge.mp4", "size": str(3000 * 1024 * 1024)},      # over cap
        ):
            data = self.client.post(reverse("core:upload_presign"), payload).json()
            self.assertFalse(data["ok"], payload)

    @patch.dict("os.environ", R2_TEST_ENV)
    @patch("core.r2.object_size", return_value=123456789)
    def test_upload_with_r2_key_creates_video(self, object_size):
        key = f"videos/{self.user.id}/abc123/long.mp4"
        response = self.client.post(
            reverse("core:upload"),
            {"r2_key": key, "r2_filename": "long.mp4", "r2_duration": "725",
             "title": "", "category": "", "description": ""},
        )
        video = Video.objects.get(user=self.user)
        self.assertRedirects(
            response, reverse("core:video_detail", args=[video.pk]),
            fetch_redirect_response=False,
        )
        self.assertEqual(video.r2_object_key, key)
        self.assertEqual(video.file_url, f"https://pub-test.r2.dev/{key}")
        self.assertEqual(video.source_size_bytes, 123456789)
        self.assertEqual(video.duration_seconds, 725)
        self.assertEqual(video.cloudinary_public_id, "")

    @patch.dict("os.environ", R2_TEST_ENV)
    @patch("core.r2.object_size", return_value=1000)
    def test_upload_rejects_foreign_r2_key(self, object_size):
        response = self.client.post(
            reverse("core:upload"),
            {"r2_key": "videos/99999/abc123/x.mp4", "r2_filename": "x.mp4",
             "title": "", "category": "", "description": ""},
        )
        self.assertEqual(response.status_code, 200)  # re-rendered with an error
        self.assertEqual(Video.objects.count(), 0)

    @patch.dict("os.environ", R2_TEST_ENV)
    @patch("core.views.delete_media")
    @patch("core.r2.delete_object")
    def test_delete_routes_r2_video_to_r2(self, delete_object, delete_media):
        video = Video.objects.create(
            user=self.user,
            file_url="https://pub-test.r2.dev/videos/1/k/x.mp4",
            r2_object_key=f"videos/{self.user.id}/k/x.mp4",
            original_filename="x.mp4",
        )
        self.client.post(reverse("core:video_delete", args=[video.pk]))
        delete_object.assert_called_once_with(video.r2_object_key)
        self.assertEqual(Video.objects.count(), 0)

    @patch.dict("os.environ", R2_TEST_ENV)
    @patch("core.publishing.r2.delete_object")
    def test_archive_deletes_r2_source_and_keeps_thumbnail(self, delete_object):
        from core import publishing

        video = Video.objects.create(
            user=self.user,
            file_url="https://pub-test.r2.dev/videos/1/k/x.mp4",
            r2_object_key=f"videos/{self.user.id}/k/x.mp4",
            thumbnail_url="https://res.cloudinary.com/thumb.jpg",
            thumbnail_public_id="thumbs/abc",
            original_filename="x.mp4",
            source_size_bytes=500,
        )
        self.assertTrue(publishing._archive_source(video))
        delete_object.assert_called_once_with(video.r2_object_key)
        video.refresh_from_db()
        self.assertTrue(video.source_deleted)
        self.assertEqual(video.source_size_bytes, 0)
        self.assertEqual(video.thumbnail_url, "https://res.cloudinary.com/thumb.jpg")
