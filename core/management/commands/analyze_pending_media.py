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

from core import ai
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
            help="Also re-attempt videos previously marked failed.",
        )

    def handle(self, *args, **options):
        if not ai.is_configured():
            self.stdout.write("GEMINI_API_KEY not set — nothing to analyze.")
            return

        Status = Video.AnalysisStatus
        wanted = [Status.PENDING]
        if options["retry_failed"]:
            wanted.append(Status.FAILED)

        # Only videos that still have an analyzable file. Newest first so a fresh
        # upload gets grounded before older backlog.
        videos = list(
            Video.objects.filter(ai_analysis_status__in=wanted, source_deleted=False)
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

            # Best-effort: analyze_media caches on success and returns "" on any
            # failure (it logs the cause). We record the terminal status so the
            # cron doesn't re-attempt this video every tick.
            try:
                text = ai.analyze_media(video)
            except ai.QuotaError as exc:
                Video.objects.filter(pk=video.pk).update(
                    ai_analysis_status=Status.FAILED, ai_analysis_error_code=exc.code,
                )
                self.stdout.write(str(exc))
                failed += 1
                break
            new_status = Status.DONE if text else Status.FAILED
            Video.objects.filter(pk=video.pk).update(ai_analysis_status=new_status, ai_analysis_error_code="")
            if text:
                done += 1
            else:
                failed += 1

        self.stdout.write(self.style.SUCCESS(
            f"Media analysis: {done} analyzed, {skipped} skipped, {failed} failed "
            f"({len(videos)} considered)."
        ))
