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


def upload_video(file, *, folder: str = CLOUDINARY_FOLDER) -> dict:
    """
    Upload a video file object to Cloudinary.

    Returns a dict with the public URL, a generated thumbnail URL, the
    Cloudinary public_id, and the original filename — exactly the fields the
    Video model stores.
    """
    _ensure_configured()

    result = cloudinary.uploader.upload(
        file,
        resource_type="video",
        folder=folder,
        use_filename=True,
        unique_filename=True,
    )
    public_id = result["public_id"]

    # Derive a JPG thumbnail URL from the first frame of the video. Cloudinary
    # generates it on the fly — no second upload needed.
    thumbnail_url, _ = cloudinary.utils.cloudinary_url(
        public_id, resource_type="video", format="jpg"
    )

    logger.info("Uploaded video to Cloudinary: %s", public_id)
    return {
        "file_url": result["secure_url"],
        "thumbnail_url": thumbnail_url,
        "public_id": public_id,
        "original_filename": getattr(file, "name", "") or "",
    }
