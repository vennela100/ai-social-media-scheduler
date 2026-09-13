"""Temporary, bounded video copies for AI. The stored publishing file is untouched."""

import csv
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
from contextlib import contextmanager

import requests


def _integer(name, default):
    return int(os.environ.get(name) or default)


JOB_TIMEOUT = 4 * 60 * 60
DOWNLOAD_TIMEOUT = 15 * 60
TRANSCODE_TIMEOUT = 60 * 60
DISK_RESERVE = 128 * 1024 * 1024
MAX_PROXY_BYTES = 1024 * 1024 * 1024

# Analysis encoding is deliberately conservative on small Render instances,
# but allowing this to be tuned avoids forcing every deployment to use a
# single CPU thread.  Two threads is a good default for the current service.
FFMPEG_THREADS = max(1, _integer("FFMPEG_THREADS", 2))


def ffmpeg_executable():
    # Wheels include FFmpeg on Windows/Linux, including Render's Python runtime.
    from imageio_ffmpeg import get_ffmpeg_exe

    return get_ffmpeg_exe()


def _download(url, destination, progress):
    limit = _integer("R2_VIDEO_MAX_MB", 2048) * 1024 * 1024
    started = time.monotonic()
    size = 0
    with requests.get(url, stream=True, timeout=(15, 120)) as response:
        response.raise_for_status()
        with destination.open("wb") as target:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                size += len(chunk)
                if size > limit:
                    raise ValueError("Video exceeds the configured upload size limit.")
                if time.monotonic() - started > DOWNLOAD_TIMEOUT:
                    raise TimeoutError("Downloading the analysis source took too long.")
                if shutil.disk_usage(destination.parent).free < DISK_RESERVE + len(chunk):
                    raise OSError("Insufficient temporary disk space for video analysis.")
                target.write(chunk)
                progress()


def transcode(source, directory, seconds, progress):
    """Encode the full timeline in one pass, with keyframes at segment boundaries."""
    directory = Path(directory)
    manifest = directory / "segments.csv"
    command = [
        ffmpeg_executable(), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-threads", str(FFMPEG_THREADS), "-filter_threads", str(FFMPEG_THREADS),
        "-protocol_whitelist", "file,pipe", "-i", str(source),
        "-map", "0:v:0", "-map", "0:a:0?", "-map_metadata", "-1",
        "-vf", "fps=12,scale=w='min(1280,iw)':h='min(720,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2,setsar=1",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
        "-maxrate", "700k", "-bufsize", "1400k", "-pix_fmt", "yuv420p",
        "-threads", str(FFMPEG_THREADS), "-force_key_frames", f"expr:gte(t,n_forced*{seconds})",
        "-c:a", "aac", "-b:a", "64k", "-ac", "1",
        "-f", "segment", "-segment_time", str(seconds),
        "-segment_time_delta", "0.05", "-reset_timestamps", "1",
        "-segment_list", str(manifest), "-segment_list_type", "csv",
        str(directory / "part-%05d.mp4"),
    ]
    started = time.monotonic()
    # Keep encoder diagnostics off pipes (no deadlock) and out of user responses.
    with (directory / "encoder.log").open("wb") as errors:
        process = subprocess.Popen(
            command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=errors,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        try:
            while True:
                progress()
                if time.monotonic() - started > TRANSCODE_TIMEOUT:
                    raise TimeoutError("Preparing the analysis copy took too long.")
                if (shutil.disk_usage(directory).free < DISK_RESERVE
                        or sum(p.stat().st_size for p in directory.glob("part-*.mp4")) > MAX_PROXY_BYTES):
                    raise OSError("Analysis copies exceeded the temporary disk budget.")
                try:
                    code = process.wait(timeout=10)
                    break
                except subprocess.TimeoutExpired:
                    continue
            if code:
                raise ValueError("Could not prepare this video for analysis. Check that it is a playable video.")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
    with manifest.open(newline="", encoding="utf-8") as stream:
        segments = [(directory / Path(name).name, float(start), float(end))
                    for name, start, end in csv.reader(stream)]
    if not segments:
        raise ValueError("The video contains no analyzable video frames.")
    return segments


@contextmanager
def prepared_segments(url, *, max_bytes, max_seconds, progress=lambda: None):
    """Download to disk, compress/split, and remove everything on success/failure."""
    started = time.monotonic()

    def tick():
        if time.monotonic() - started > JOB_TIMEOUT:
            raise TimeoutError("Video analysis exceeded its processing time budget.")
        progress()

    # Four-minute inputs reduce Gemini round trips while remaining safely below
    # the default five-minute per-segment cap. Deployments can tune this down
    # with MEDIA_ANALYSIS_SEGMENT_SECONDS if their model/quota needs it.
    segment_seconds = _integer("MEDIA_ANALYSIS_SEGMENT_SECONDS", 240)
    seconds = max(1, min(segment_seconds, max_seconds - 1)) if max_seconds else segment_seconds
    if max_bytes:
        # Reserve headroom for encoder bursts and container overhead (~100 KB/s).
        seconds = min(seconds, max(1, max_bytes // 120000 - 2))
    with tempfile.TemporaryDirectory(prefix="video-analysis-") as temporary:
        directory = Path(temporary)
        source = directory / "source.video"
        _download(url, source, tick)
        segments = transcode(source, directory, seconds, tick)
        source.unlink()  # only our temporary download, never the R2 object
        for path, start, end in segments:
            if not path.is_file() or not path.stat().st_size:
                raise ValueError("An analysis segment is missing or empty.")
            if max_bytes and path.stat().st_size > max_bytes:
                raise ValueError("A compressed analysis segment exceeds the configured byte cap.")
            if max_seconds and end - start > max_seconds:
                raise ValueError("An analysis segment exceeds the configured duration cap.")
        yield segments, tick
