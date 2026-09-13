import subprocess
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

from django.test import SimpleTestCase

from . import ai, analysis_proxy


class SegmentAnalysisTests(SimpleTestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.paths = [self.directory / f"part-{i}.mp4" for i in range(3)]
        for path in self.paths:
            path.write_bytes(b"small video")
        self.tick = Mock()

        @contextmanager
        def prepared(*args, **kwargs):
            yield [(path, i * 240, (i + 1) * 240) for i, path in enumerate(self.paths)], self.tick

        patcher = patch("core.analysis_proxy.prepared_segments", prepared)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = Mock()

    @patch("core.ai.cleanup_analysis")
    @patch("core.ai.upload_for_analysis")
    def test_every_segment_including_ending_is_combined(self, upload, cleanup):
        self.client.models.generate_content.side_effect = [
            SimpleNamespace(text=text) for text in ("Introduction.", "Demonstration.", "Final result.")
        ]
        text = ai._analyze_video(self.client, "https://example.com/original.mp4")
        self.assertLess(text.index("Introduction."), text.index("Demonstration."))
        self.assertIn("[480.0–720.0s] Final result.", text)
        self.assertEqual(upload.call_count, 3)
        self.assertEqual(cleanup.call_count, 3)
        self.assertEqual(upload.call_args.kwargs, {"local_path": self.paths[-1]})
        self.assertTrue(all(not path.exists() for path in self.paths))

    @patch("core.ai.cleanup_analysis")
    @patch("core.ai.upload_for_analysis")
    def test_empty_middle_segment_does_not_return_partial_success(self, upload, cleanup):
        self.client.models.generate_content.side_effect = [
            SimpleNamespace(text="Introduction."), SimpleNamespace(text=""),
        ]
        self.assertEqual(ai._analyze_video(self.client, "https://example.com/original.mp4"), "")
        self.assertEqual(cleanup.call_count, 2)

    @patch("core.ai.cleanup_analysis")
    @patch("core.ai.upload_for_analysis")
    def test_quota_failure_cleans_remote_segment(self, upload, cleanup):
        self.client.models.generate_content.side_effect = ai.QuotaError("daily_quota")
        with self.assertRaises(ai.QuotaError):
            ai._analyze_video(self.client, "https://example.com/original.mp4")
        cleanup.assert_called_once()


class ProxyPreparationTests(SimpleTestCase):
    def response(self, chunks):
        response = MagicMock()
        response.__enter__.return_value = response
        response.iter_content.return_value = chunks
        return response

    @patch("core.analysis_proxy.requests.get")
    def test_interrupted_download_cleans_source(self, get):
        paths = []

        def broken(*args, **kwargs):
            yield b"partial"
            raise OSError("disconnected")

        response = self.response([])
        response.iter_content.side_effect = broken
        get.return_value = response
        original = analysis_proxy._download

        def download(url, destination, progress):
            paths.append(destination.parent)
            return original(url, destination, progress)

        with patch("core.analysis_proxy._download", side_effect=download):
            with self.assertRaises(OSError):
                with analysis_proxy.prepared_segments("https://example.com/video", max_bytes=1000, max_seconds=300):
                    self.fail("Partial download must not reach analysis")
        self.assertFalse(paths[0].exists())
        response.__exit__.assert_called_once()

    @patch("core.analysis_proxy.requests.get")
    def test_streamed_size_limit_ignores_untrusted_metadata(self, get):
        get.return_value = self.response([b"x" * (1024 * 1024), b"x"])
        with patch.dict("os.environ", {"R2_VIDEO_MAX_MB": "1"}):
            with self.assertRaisesRegex(ValueError, "upload size limit"):
                with analysis_proxy.prepared_segments("https://example.com/video", max_bytes=1000, max_seconds=300):
                    self.fail("Oversized download must not reach analysis")

    def test_oversized_proxy_is_rejected_and_all_temporary_files_removed(self):
        directories = []

        def download(url, destination, progress):
            destination.write_bytes(b"original")

        def transcode(source, directory, seconds, progress):
            directories.append(directory)
            part = directory / "part.mp4"
            part.write_bytes(b"x" * 101)
            return [(part, 0, 1)]

        with patch("core.analysis_proxy._download", download), patch("core.analysis_proxy.transcode", transcode):
            with self.assertRaisesRegex(ValueError, "byte cap"):
                with analysis_proxy.prepared_segments("https://example.com/video", max_bytes=100, max_seconds=300):
                    self.fail("Oversized segment must not reach Gemini")
        self.assertFalse(directories[0].exists())

    @patch("core.analysis_proxy.ffmpeg_executable", return_value="ffmpeg")
    @patch("core.analysis_proxy.subprocess.Popen")
    def test_cancelled_transcode_stops_encoder_before_cleanup(self, popen, executable):
        process = popen.return_value
        process.poll.return_value = None
        with TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(RuntimeError, "ownership changed"):
                analysis_proxy.transcode(
                    Path(temporary) / "source.video", temporary, 240,
                    Mock(side_effect=RuntimeError("ownership changed")),
                )
        process.kill.assert_called_once()
        process.wait.assert_called_once()


class RealEncoderTests(SimpleTestCase):
    def test_full_duration_audio_and_silent_portrait_video_survive_segmentation(self):
        # Real encoding verifies FFmpeg flags, cuts, orientation, and final segment.
        executable = analysis_proxy.ffmpeg_executable()
        for size, audio in [("1920x1080", True), ("720x1280", False)]:
            with self.subTest(size=size, audio=audio), TemporaryDirectory() as temporary:
                directory = Path(temporary)
                source = directory / "original.mp4"
                command = [executable, "-v", "error", "-y", "-f", "lavfi", "-i",
                           f"testsrc2=size={size}:rate=24:duration=5"]
                if audio:
                    command += ["-f", "lavfi", "-i", "sine=frequency=440:duration=5", "-c:a", "aac"]
                command += ["-c:v", "libx264", "-preset", "ultrafast", "-threads", "1", str(source)]
                subprocess.run(command, check=True, capture_output=True, timeout=60)
                original_size = source.stat().st_size
                segments = analysis_proxy.transcode(source, directory, 2, lambda: None)
                self.assertEqual(len(segments), 3)
                self.assertAlmostEqual(segments[0][1], 0, delta=0.1)
                self.assertAlmostEqual(segments[-1][2], 5, delta=0.2)
                self.assertLess(sum(path.stat().st_size for path, _, _ in segments), original_size)
                self.assertEqual(source.stat().st_size, original_size)
                for path, _, _ in segments:
                    probe = subprocess.run([executable, "-hide_banner", "-i", str(path),
                                            "-f", "null", "-"], capture_output=True, text=True, timeout=30)
                    self.assertEqual(probe.returncode, 0, probe.stderr)
                    self.assertEqual("Audio:" in probe.stderr, audio)
                    self.assertIn("12 fps", probe.stderr)
                    self.assertRegex(probe.stderr, "1280x720" if audio else r"40[46]x720")
