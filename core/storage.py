"""
Cloudinary storage service (Phase 1).

Why Cloudinary: Instagram's Graph API can't receive a file upload — it needs a
*public URL* it can fetch the video from. Cloudinary gives us that URL for free,
which is the linchpin that makes the whole $0 Instagram path work later.

This module is the ONLY place that talks to Cloudinary, so views never import the
SDK directly. It's gated on settings.CLOUDINARY_URL: until the user provides the
key, calls raise a clear ImproperlyConfigured instead of failing cryptically.
"""

import logging

import cloudinary
import cloudinary.api
import cloudinary.uploader
import cloudinary.utils
from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured

logger = logging.getLogger("scheduler")

# Folder inside your Cloudinary account where uploads land. Keeps the media
# library tidy and makes it easy to find/clean app uploads.
CLOUDINARY_FOLDER = "scheduler_videos"
# Tiny preserved thumbnails live here after a source is archived.
CLOUDINARY_THUMB_FOLDER = "scheduler_thumbnails"
# Max width (px) for a preserved thumbnail — keeps it a few KB vs the source MBs.
THUMBNAIL_MAX_WIDTH = 480

# Usage is an external API call, so cache it briefly: the dashboard can render
# many times a minute, but Cloudinary's numbers barely move minute-to-minute.
_USAGE_CACHE_KEY = "cloudinary_usage"
_USAGE_TTL_SECONDS = 300

# When to warn the user that storage/credits are running out. Tune to taste:
# below WARN is "ok", WARN–FULL shows a heads-up, at/above FULL is "act now".
STORAGE_WARN_PERCENT = 80
STORAGE_FULL_PERCENT = 95


def usage_level(percent) -> str:
    """Map a used-percentage to a severity: "ok" | "warn" | "full"."""
    if percent is None:
        return "ok"
    if percent >= STORAGE_FULL_PERCENT:
        return "full"
    if percent >= STORAGE_WARN_PERCENT:
        return "warn"
    return "ok"


def is_configured() -> bool:
    """True once the Cloudinary credentials are available."""
    return bool(settings.CLOUDINARY_URL)


def _ensure_configured() -> None:
    if not is_configured():
        raise ImproperlyConfigured(
            "CLOUDINARY_URL is not set. Add it to your .env (format: "
            "cloudinary://<api_key>:<api_secret>@<cloud_name>) so uploads can work."
        )
    # The SDK auto-reads CLOUDINARY_URL from the environment (load_dotenv put it
    # there). This call is a no-op safeguard that also validates the format.
    cloudinary.config()


def upload_media(file, *, media_type: str = "video", folder: str = CLOUDINARY_FOLDER) -> dict:
    """
    Upload a video or image file object to Cloudinary.

    `media_type` is "video" or "image"; it selects Cloudinary's resource_type
    (they have different transformation pipelines and delivery URLs). Returns a
    dict with the public URL, a thumbnail URL, the Cloudinary public_id, and the
    original filename — exactly the fields the Video model stores.
    """
    _ensure_configured()

    resource_type = "image" if media_type == "image" else "video"
    result = cloudinary.uploader.upload(
        file,
        resource_type=resource_type,
        folder=folder,
        use_filename=True,
        unique_filename=True,
    )
    public_id = result["public_id"]

    if resource_type == "video":
        # Derive a JPG thumbnail from the first frame — generated on the fly.
        thumbnail_url, _ = cloudinary.utils.cloudinary_url(
            public_id, resource_type="video", format="jpg"
        )
    else:
        # A still image is its own thumbnail.
        thumbnail_url = result["secure_url"]

    logger.info("Uploaded %s to Cloudinary: %s", resource_type, public_id)
    return {
        "file_url": result["secure_url"],
        "thumbnail_url": thumbnail_url,
        "public_id": public_id,
        "original_filename": getattr(file, "name", "") or "",
    }


def delete_media(public_id: str, *, media_type: str = "video") -> None:
    """Delete an asset from Cloudinary. No-op if we have no public_id."""
    if not public_id:
        return
    _ensure_configured()
    resource_type = "image" if media_type == "image" else "video"
    cloudinary.uploader.destroy(public_id, resource_type=resource_type, invalidate=True)
    logger.info("Deleted %s from Cloudinary: %s", resource_type, public_id)
    cache.delete(_USAGE_CACHE_KEY)  # numbers just changed — force a fresh lookup


def preserve_thumbnail(image_url: str) -> dict | None:
    """Re-upload a small, standalone copy of `image_url` as a permanent thumbnail.

    Before we delete a video/image source, we copy its preview frame into a tiny
    independent image asset (a few KB). That survives the source deletion, so the
    dashboard keeps showing a thumbnail. Cloudinary fetches the remote URL itself.
    Returns {"url", "public_id"} or None on any failure (caller keeps the source).
    """
    if not image_url:
        return None
    try:
        _ensure_configured()
        result = cloudinary.uploader.upload(
            image_url,
            folder=CLOUDINARY_THUMB_FOLDER,
            resource_type="image",
            transformation=[{"width": THUMBNAIL_MAX_WIDTH, "crop": "limit", "quality": "auto"}],
        )
    except Exception as exc:
        logger.warning("Thumbnail preserve failed for %s: %s", image_url, exc)
        return None
    return {"url": result["secure_url"], "public_id": result["public_id"]}


def human_bytes(n: float) -> str:
    """Format a byte count as a short human string, e.g. 12345678 -> '11.8 MB'."""
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def get_usage() -> dict | None:
    """Return the Cloudinary account's usage, or None if unavailable.

    A usage lookup must never break the dashboard, so this swallows every error
    (not configured, network, auth) and returns None — the template then just
    omits the storage card. Cached for a few minutes to avoid an API call on
    every page load. Cloudinary's free tier is credit-based, so `storage.limit`
    may be absent; we surface whatever the account reports.
    """
    if not is_configured():
        return None
    cached = cache.get(_USAGE_CACHE_KEY)
    if cached is not None:
        return cached
    try:
        _ensure_configured()
        data = cloudinary.api.usage()
    except Exception as exc:
        logger.warning("Cloudinary usage lookup failed: %s", exc)
        return None

    storage = data.get("storage") or {}
    credits = data.get("credits") or {}
    result = {
        "plan": data.get("plan", ""),
        "storage_bytes": storage.get("usage", 0),
        "storage_limit_bytes": storage.get("limit"),  # may be None on credit plans
        "assets": (data.get("resources") if isinstance(data.get("resources"), int)
                   else (data.get("objects") or {}).get("usage", 0)),
        "credits_used_percent": credits.get("used_percent"),
    }
    cache.set(_USAGE_CACHE_KEY, result, _USAGE_TTL_SECONDS)
    return result
