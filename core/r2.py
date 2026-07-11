"""Cloudflare R2 storage for large video sources.

Cloudinary's free plan caps video files at 100 MB, and routing uploads through
the Django process caps them again (Render free tier: 512 MB RAM, 120 s
requests). R2 removes both walls: the browser asks us for a presigned PUT URL
and uploads the file DIRECTLY to R2 — the bytes never touch this server — and
R2's free tier has no comparable per-file limit and zero egress fees (the
platforms pulling the video for publishing costs nothing).

Gated on env config like every other integration: R2_ACCOUNT_ID,
R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET, R2_PUBLIC_BASE_URL (the
bucket's public r2.dev or custom-domain URL — publishing and Gemini analysis
both fetch the plain public URL). Images and thumbnails stay on Cloudinary.
"""

import logging
import os
import re

from django.core.exceptions import ImproperlyConfigured
from django.utils.crypto import get_random_string

logger = logging.getLogger("scheduler")

# Our own ceiling for direct-to-R2 videos. R2 itself allows far larger objects;
# this keeps a stray multi-GB upload from eating the 10 GB free tier in one go.
R2_VIDEO_MAX_MB = int(os.environ.get("R2_VIDEO_MAX_MB", "2048"))

# Presigned URLs are single-purpose and short-lived; long enough to push a
# couple of GB on a slow home uplink, useless to replay afterwards.
PRESIGN_EXPIRY_SECONDS = int(os.environ.get("R2_PRESIGN_EXPIRY_SECONDS", "3600"))

_REQUIRED_ENV = (
    "R2_ACCOUNT_ID",
    "R2_ACCESS_KEY_ID",
    "R2_SECRET_ACCESS_KEY",
    "R2_BUCKET",
    "R2_PUBLIC_BASE_URL",
)


def is_configured() -> bool:
    return all(os.environ.get(k) for k in _REQUIRED_ENV)


def _ensure_configured() -> None:
    missing = [k for k in _REQUIRED_ENV if not os.environ.get(k)]
    if missing:
        raise ImproperlyConfigured(f"R2 not configured (missing {', '.join(missing)}).")


def _client():
    _ensure_configured()
    import boto3  # deferred so the app runs without boto3 until R2 is used

    return boto3.client(
        "s3",
        endpoint_url=f"https://{os.environ['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )


def build_object_key(user_id: int, filename: str) -> str:
    """A collision-proof, user-scoped key: videos/<user>/<random>/<safe-name>.

    The user id prefix lets the upload view verify a submitted key really came
    from this user's own presign call, so nobody can attach someone else's
    object (or a guessed key) to their Video row.
    """
    base, dot, ext = (filename or "file").rpartition(".")
    if not dot:
        base, ext = ext, ""
    safe_base = re.sub(r"[^A-Za-z0-9_-]+", "-", base).strip("-") or "video"
    safe_ext = re.sub(r"[^A-Za-z0-9]", "", ext).lower()
    name = f"{safe_base[:80]}.{safe_ext}" if safe_ext else safe_base[:80]
    return f"videos/{user_id}/{get_random_string(12)}/{name}"


def key_belongs_to(key: str, user_id: int) -> bool:
    return bool(key) and key.startswith(f"videos/{user_id}/")


def presign_put(key: str, content_type: str, size: int) -> str:
    """A one-hour URL the browser can PUT the file to, directly into R2.

    ContentLength/ContentType are part of the signature, so the upload is
    bound to the declared size — a client can't presign 10 MB and push 2 GB.
    """
    return _client().generate_presigned_url(
        "put_object",
        Params={
            "Bucket": os.environ["R2_BUCKET"],
            "Key": key,
            "ContentType": content_type or "application/octet-stream",
            "ContentLength": int(size),
        },
        ExpiresIn=PRESIGN_EXPIRY_SECONDS,
    )


def public_url(key: str) -> str:
    return f"{os.environ['R2_PUBLIC_BASE_URL'].rstrip('/')}/{key}"


def object_size(key: str) -> int | None:
    """The stored object's true size via HEAD, or None if it isn't there.

    Called when the browser reports its direct upload finished — the size we
    trust is what R2 says landed, not what the client claimed.
    """
    try:
        head = _client().head_object(Bucket=os.environ["R2_BUCKET"], Key=key)
        return int(head["ContentLength"])
    except Exception as exc:
        logger.warning("R2 HEAD failed for %s: %s", key, exc)
        return None


def delete_object(key: str) -> None:
    """Delete an object. No-op without a key; raises on transport errors so
    callers decide whether a failed delete matters (video_delete logs and
    moves on; archiving aborts and retries next tick)."""
    if not key:
        return
    _client().delete_object(Bucket=os.environ["R2_BUCKET"], Key=key)
    logger.info("Deleted R2 object %s", key)
