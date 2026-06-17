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
import cloudinary.uploader
import cloudinary.utils
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

logger = logging.getLogger("scheduler")

# Folder inside your Cloudinary account where uploads land. Keeps the media
# library tidy and makes it easy to find/clean app uploads.
CLOUDINARY_FOLDER = "scheduler_videos"


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
