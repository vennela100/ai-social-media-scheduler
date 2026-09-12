import json
import threading
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from google.genai.errors import ClientError

from . import ai, media_analysis, publishing
from .models import AIContent, Platform, Video
from .views import _final_caption


def quota_response(quota_id):
    return ClientError(429, {"error": {
        "code": 429, "status": "RESOURCE_EXHAUSTED", "message": "Quota exceeded",
        "details": [{"@type": "type.googleapis.com/google.rpc.QuotaFailure",
                     "violations": [{"quotaId": quota_id}]}],
    }})


class QuotaAndHashtagTests(SimpleTestCase):
    def test_daily_and_minute_limits_have_distinct_messages(self):
        daily = ai.quota_error(quota_response("GenerateRequestsPerDayPerProjectPerModel-FreeTier"))
        minute = ai.quota_error(quota_response("GenerateContentInputTokensPerModelPerMinute-FreeTier"))
        self.assertEqual(daily.code, "daily_quota")
        self.assertIn("Today's AI quota is completed", str(daily))
        self.assertEqual(minute.code, "rate_limit")
        self.assertIn("wait a minute", str(minute))

    def test_unknown_429_does_not_claim_daily_quota(self):
        self.assertEqual(ai.quota_error(ClientError(429, {"message": "Resource exhausted"})).code, "quota")
        self.assertIsNone(ai.quota_error(ClientError(503, {"message": "Service unavailable"})))

    @patch("core.ai.time.sleep")
    def test_daily_limit_is_not_retried(self, sleep):
        client = Mock()
        client.models.generate_content.side_effect = quota_response("GenerateRequestsPerDayPerProjectPerModel")
        with self.assertRaises(ai.QuotaError):
            ai._generate_with_retry(client, model="test", contents="test")
        client.models.generate_content.assert_called_once()
        sleep.assert_not_called()

    def test_youtube_hashtags_are_visible_and_not_duplicated(self):
        description = ai.youtube_description("Learn Python. #Python", "Python, Django, web development, tutorials")
        self.assertEqual(description, "Learn Python. #Python\n\n#Django #webdevelopment")
        self.assertEqual(ai.youtube_description(description, "Python, Django, web development"), description)

    def test_description_limit_preserves_hashtags_and_is_idempotent(self):
        description = ai.youtube_description("x" * 5000, "python, django, coding", limit=5000)
        self.assertLessEqual(len(description), 5000)
        self.assertTrue(description.endswith("#python #django #coding"))
        self.assertEqual(ai.youtube_description(description, "python, django, coding", limit=5000), description)
        self.assertTrue(ai.validate_metadata("youtube", "Title", "x" * 5000, "python"))

    @patch("core.ai._client")
    def test_generated_youtube_description_includes_hashtags(self, client):
        client.return_value.models.generate_content.return_value = SimpleNamespace(text=json.dumps({
            "title": "Learn Python", "description": "A Python tutorial.", "tags": ["Python", "Django"],
        }))
        result = ai.generate_metadata("youtube", description="A Python tutorial.")
        self.assertEqual(result["description"], "A Python tutorial.\n\n#Python #Django")
        self.assertEqual(result["hashtags"], "Python, Django")

    def test_fallback_youtube_description_includes_hashtags(self):
        result = ai.fallback_metadata("youtube", title="Python Django", description="A tutorial.")
        self.assertEqual(result["description"], "A tutorial.\n\n#python #django")

    @patch("core.publishing.youtube.publish", return_value="youtube-test-id")
    def test_published_description_contains_hashtags_and_keyword_tags(self, publish):
        content = AIContent(platform="youtube", generated_title="Python", generated_description="A tutorial.",
                            generated_hashtags="Python, Django")
        post = SimpleNamespace(ai_content=content, social_account=object(), final_caption=_final_caption(content),
                               video=SimpleNamespace(file_url="https://example.com/video.mp4", thumbnail_url=""),
                               visibility="private")
        publishing._publish_youtube(post)
        self.assertEqual(publish.call_args.kwargs["description"], "A tutorial.\n\n#Python #Django")
        self.assertEqual(publish.call_args.kwargs["tags"], ["Python", "Django"])


class MediaResourceCleanupTests(SimpleTestCase):
    def setUp(self):
        self.response = MagicMock()
        self.response.__enter__.return_value = self.response
        self.response.iter_content.return_value = [b"test video"]
        client_patch = patch("core.ai._client")
        self.client = client_patch.start().return_value
        self.addCleanup(client_patch.stop)
        get_patch = patch("core.ai.requests.get", return_value=self.response)
        get_patch.start()
        self.addCleanup(get_patch.stop)
        temp_dir = TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.temp_path = Path(temp_dir.name)
        temp_patch = patch("core.ai.tempfile.NamedTemporaryFile", side_effect=lambda **kwargs: NamedTemporaryFile(dir=temp_dir.name, **kwargs))
        temp_patch.start()
        self.addCleanup(temp_patch.stop)

    def test_interrupted_download_removes_partial_file_and_closes_response(self):
        def broken_download(*args, **kwargs):
            yield b"partial video"
            raise OSError("download interrupted")

        self.response.iter_content.side_effect = broken_download
        self.assertIsNone(ai.upload_for_analysis("https://example.com/video.mp4"))
        self.assertEqual(list(self.temp_path.iterdir()), [])
        self.response.__exit__.assert_called_once()
        self.client.files.upload.assert_not_called()

    @patch("core.ai.cleanup_analysis")
    @patch("core.ai.VIDEO_PROCESS_TIMEOUT", 0)
    def test_processing_timeout_removes_unused_gemini_file(self, cleanup):
        gfile = SimpleNamespace(name="files/test", state=SimpleNamespace(name="PROCESSING"))
        self.client.files.upload.return_value = gfile
        self.assertIsNone(ai.upload_for_analysis("https://example.com/video.mp4"))
        cleanup.assert_called_once_with(gfile)
        self.assertEqual(list(self.temp_path.iterdir()), [])

    @patch("core.ai.cleanup_analysis")
    def test_successful_analysis_keeps_remote_file_until_used(self, cleanup):
        gfile = SimpleNamespace(name="files/test", state=SimpleNamespace(name="ACTIVE"))
        self.client.files.upload.return_value = gfile
        def generate(**kwargs):
            cleanup.assert_not_called()
            return SimpleNamespace(text="The video shows a tutorial.")
        self.client.models.generate_content.side_effect = generate
        self.assertEqual(ai._analyze_video(self.client, "https://example.com/video.mp4"), "The video shows a tutorial.")
        cleanup.assert_called_once_with(gfile)
        self.assertEqual(list(self.temp_path.iterdir()), [])


@override_settings(
    ALLOWED_HOSTS=["testserver"], GEMINI_API_KEY="test-key", CLOUDINARY_URL="",
    STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    },
)
class QuotaMessageTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="quota-test")
        self.client.force_login(self.user)
        self.video = Video.objects.create(user=self.user, file_url="https://example.com/video.mp4",
                                          user_description="A Python tutorial.")
        self.draft = AIContent.objects.create(video=self.video, platform=Platform.YOUTUBE,
                                              generated_title="Existing title", generated_description="Existing description.",
                                              generated_hashtags="Python, Django")

    @patch("core.ai.generate_metadata", side_effect=quota_response("GenerateRequestsPerDayPerProjectPerModel"))
    def test_quota_message_preserves_draft_and_survives_reload(self, generate):
        response = self.client.post(reverse("core:generate_ai", args=[self.draft.pk]))
        self.assertFalse(response.json()["ok"])
        self.assertEqual(response.json()["error_code"], "daily_quota")
        self.draft.refresh_from_db()
        self.assertEqual(self.draft.generated_title, "Existing title")
        self.assertEqual(self.draft.generated_description, "Existing description.")
        self.assertEqual(self.draft.generation_status, AIContent.GenStatus.FAILED)
        response = self.client.get(reverse("core:video_detail", args=[self.video.pk]))
        self.assertContains(response, "Today&#x27;s AI quota is completed")
        self.assertContains(response, "#Python #Django")

    @patch("core.ai.generate_metadata", side_effect=quota_response("GenerateRequestsPerDayPerProjectPerModel"))
    def test_api_reports_daily_limit(self, generate):
        response = self.client.post(f"/api/drafts/{self.draft.pk}/regenerate/")
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json()["errorCode"], "daily_quota")

    @patch("core.ai.suggest_post_times", side_effect=quota_response("GenerateRequestsPerDayPerProjectPerModel"))
    def test_suggestions_report_daily_limit(self, suggest):
        response = self.client.post(reverse("core:suggest_times", args=[self.draft.pk]))
        self.assertFalse(response.json()["ok"])
        self.assertEqual(response.json()["error_code"], "daily_quota")

    def test_background_analysis_persists_daily_quota_message(self):
        started = timezone.now()
        AIContent.objects.filter(pk=self.draft.pk).update(generation_status=AIContent.GenStatus.PENDING)
        Video.objects.filter(pk=self.video.pk).update(ai_analysis_started_at=started, ai_analysis_status="processing")
        slot = threading.BoundedSemaphore(1)
        slot.acquire()
        with patch("core.media_analysis._slot", slot), \
                patch("core.media_analysis.close_old_connections"), \
                patch("core.media_analysis.connections.close_all"), \
                patch("core.ai.analyze_media", side_effect=ai.QuotaError("daily_quota")):
            media_analysis._run(self.video.pk, started)
        response = self.client.post(reverse("core:analyze_media", args=[self.video.pk]))
        self.assertEqual(response.json()["error_code"], "daily_quota")
        self.assertIn("Today's AI quota is completed", response.json()["error"])
        self.draft.refresh_from_db()
        self.assertEqual(self.draft.generation_error_code, "daily_quota")
        self.assertEqual(self.draft.generation_status, AIContent.GenStatus.FAILED)
        self.assertTrue(slot.acquire(blocking=False))

    @patch("core.ai._client")
    def test_analysis_does_not_swallow_quota_errors(self, client):
        self.video.media_type = "image"
        with patch("core.ai._analyze_image", side_effect=quota_response("GenerateRequestsPerDayPerProjectPerModel")):
            with self.assertRaises(ai.QuotaError):
                ai.analyze_media(self.video)

    @patch("core.ai._client")
    def test_successful_retry_clears_old_quota_message(self, client):
        self.draft.generation_error_code = "daily_quota"
        self.draft.save()
        client.return_value.models.generate_content.return_value = SimpleNamespace(text=json.dumps({
            "title": "Python", "description": "Tutorial.", "tags": ["Python", "Django"],
        }))
        response = self.client.post(reverse("core:generate_ai", args=[self.draft.pk]))
        self.assertTrue(response.json()["ok"])
        self.assertIn("#Python #Django", response.json()["description"])
        self.draft.refresh_from_db()
        self.assertEqual(self.draft.generation_error_code, "")
        self.assertEqual(self.draft.generation_status, AIContent.GenStatus.DONE)
