"""
Analyze uploaded media (video/image) with Gemini so generation is grounded in
what's ACTUALLY in the file, not just the user's typed brief.

This runs OFF the web request path — from the GitHub Actions cron, or manually —
because downloading a clip and waiting for Gemini to process it can take far
longer than a Render web request is allowed to run. Each video is analyzed once;
the result is cached on Video.ai_media_analysis and reused for every platform.

    python manage.py analyze_pending_media
    python manage.py analyze_pending_media --limit 10
    python manage.py analyze_pending_media --retry-failed
"""

import logging

from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone

from core import ai, media_analysis
from core.models import Video

logger = logging.getLogger("scheduler")

# Analyze at most this many per run so a single tick can't run long or spend a
# burst of tokens; any backlog drains a few at a time across ticks.
DEFAULT_BATCH = 3


class Command(BaseCommand):
    help = "Analyze pending uploaded media with Gemini (grounds AI generation)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--limit", type=int, default=DEFAULT_BATCH,
            help=f"Max videos to analyze this run (default {DEFAULT_BATCH}).",
        )
        parser.add_argument(
            "--retry-failed", action="store_true",
            help="Also re-attempt videos previously marked failed or skipped.",
        )

    def handle(self, *args, **options):
        if not ai.is_configured():
            self.stdout.write("GEMINI_API_KEY not set — nothing to analyze.")
            return

        Status = Video.AnalysisStatus
        wanted = [Status.PENDING]
        if options["retry_failed"]:
            wanted.extend([Status.FAILED, Status.SKIPPED])

        # Only videos that still have an analyzable file. Newest first so a fresh
        # upload gets grounded before older backlog.
        videos = list(
            Video.objects.filter(source_deleted=False).filter(
                Q(ai_analysis_status__in=wanted)
                | Q(ai_analysis_status=Status.PROCESSING,
                    ai_analysis_started_at__lte=timezone.now() - media_analysis.LEASE_DURATION)
                | Q(ai_analysis_status=Status.PROCESSING, ai_analysis_started_at__isnull=True)
            )
            .exclude(file_url="")
            .order_by("-uploaded_at")[: options["limit"]]
        )

        done = skipped = failed = 0
        for video in videos:
            reason = ai.analysis_skip_reason(video)
            if reason:
                Video.objects.filter(pk=video.pk).update(ai_analysis_status=Status.SKIPPED)
                logger.info("Skipping analysis of video %s: %s", video.pk, reason)
                skipped += 1
                continue

            # Share the web worker's lease and heartbeat to avoid analyzing the
            # same long video twice when a browser and cron run concurrently.
            if video.ai_media_analysis.strip():
                Video.objects.filter(pk=video.pk).update(ai_analysis_status=Status.DONE)
                done += 1
                continue
            status = media_analysis.start(video, background=False)
            if status == Status.DONE:
                done += 1
            elif status == Status.FAILED:
                failed += 1
                video.refresh_from_db()
                if video.ai_analysis_error_code in ai.QUOTA_MESSAGES:
                    self.stdout.write(ai.QUOTA_MESSAGES[video.ai_analysis_error_code])
                    break

        self.stdout.write(self.style.SUCCESS(
            f"Media analysis: {done} analyzed, {skipped} skipped, {failed} failed "
            f"({len(videos)} considered)."
        ))
