"""Run media analysis outside HTTP requests, with a database lease per video."""

import datetime as dt
import logging
import threading

from django.db import close_old_connections, connections
from django.db.models import Q
from django.utils import timezone

from . import ai
from .models import AIContent, Video

logger = logging.getLogger("scheduler")
LEASE_DURATION = dt.timedelta(minutes=20)
# Bound downloads and Gemini uploads on small web instances. Other videos poll
# until a slot is available; the database lease also excludes other processes.
_slot = threading.BoundedSemaphore(1)


def _run(video_id, started_at):
    def heartbeat():
        nonlocal started_at
        now = timezone.now()
        if now - started_at < dt.timedelta(seconds=30):
            return
        # Renew ownership so another worker cannot reclaim a healthy long job.
        close_old_connections()
        renewed = Video.objects.filter(
            pk=video_id, ai_analysis_started_at=started_at, source_deleted=False,
        ).update(ai_analysis_started_at=now)
        if not renewed:
            raise RuntimeError("Analysis source was removed or job ownership changed.")
        started_at = now

    try:
        close_old_connections()
        video = Video.objects.filter(pk=video_id, source_deleted=False).first()
        if video is None:
            return
        text = ai.analyze_media(video, on_progress=heartbeat)
        Video.objects.filter(pk=video_id, ai_analysis_started_at=started_at).update(
            ai_analysis_status=Video.AnalysisStatus.DONE if text else Video.AnalysisStatus.FAILED,
            ai_analysis_started_at=None,
            ai_analysis_error_code="",
        )
    except ai.QuotaError as exc:
        Video.objects.filter(pk=video_id, ai_analysis_started_at=started_at).update(
            ai_analysis_status=Video.AnalysisStatus.FAILED, ai_analysis_started_at=None,
            ai_analysis_error_code=exc.code,
        )
        AIContent.objects.filter(video_id=video_id, generation_status=AIContent.GenStatus.PENDING).update(
            generation_status=AIContent.GenStatus.FAILED, generation_error_code=exc.code,
        )
    except Exception:
        logger.exception("Background media analysis failed for video %s", video_id)
        close_old_connections()
        Video.objects.filter(pk=video_id, ai_analysis_started_at=started_at).update(
            ai_analysis_status=Video.AnalysisStatus.FAILED, ai_analysis_started_at=None,
        )
    finally:
        connections.close_all()
        _slot.release()


def start(video, *, background=True):
    """Claim a job for the browser or run it synchronously in the cron worker."""
    Status = Video.AnalysisStatus
    now = timezone.now()
    if (video.ai_analysis_status == Status.PROCESSING
            and video.ai_analysis_started_at
            and video.ai_analysis_started_at > now - LEASE_DURATION):
        return Status.PROCESSING
    if not _slot.acquire(blocking=False):
        return Status.PENDING

    try:
        eligible = (
            ~Q(ai_analysis_status=Status.PROCESSING)
            | Q(ai_analysis_started_at__lte=now - LEASE_DURATION)
            | Q(ai_analysis_started_at__isnull=True)
        )
        claimed = Video.objects.filter(pk=video.pk, source_deleted=False, ai_media_analysis="").filter(eligible).update(
            ai_analysis_status=Status.PROCESSING, ai_analysis_started_at=now, ai_analysis_error_code="",
        )
        if not claimed:
            _slot.release()
            return Status.PROCESSING
        if background:
            threading.Thread(target=_run, args=(video.pk, now), daemon=True).start()
    except Exception:
        _slot.release()
        Video.objects.filter(pk=video.pk, ai_analysis_started_at=now).update(
            ai_analysis_status=Status.FAILED, ai_analysis_started_at=None,
        )
        raise
    if not background:
        _run(video.pk, now)
        return Video.objects.filter(pk=video.pk).values_list("ai_analysis_status", flat=True).first()
    return Status.PROCESSING
