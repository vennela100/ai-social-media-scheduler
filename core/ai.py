"""
Gemini metadata generation (Phase 2).

Given a short brief about a video and a target platform, produce a title,
description, and hashtags tuned to that platform's culture and limits. This is
the only module that talks to Gemini; views call generate_metadata().

Gated on settings.GEMINI_API_KEY: until the user provides it, calls raise a
clear ImproperlyConfigured rather than failing deep inside the SDK.
"""

import json
import logging
import os
import tempfile
import time

import requests
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

logger = logging.getLogger("scheduler")

GEMINI_MODEL = "gemini-2.5-flash"

# Caps for the (optional) video-analysis step. Downloading + uploading + waiting
# for Gemini to process a clip is the slow part, so we bound each piece.
VIDEO_DOWNLOAD_TIMEOUT = 120       # seconds to pull bytes from Cloudinary
VIDEO_PROCESS_TIMEOUT = 90         # seconds to wait for Gemini to make it ACTIVE
VIDEO_POLL_INTERVAL = 3            # seconds between state checks

# Per-platform hard limits we generate within and validate against. These are
# the public API/UX caps; tune if a platform changes them.
PLATFORM_LIMITS = {
    "youtube": {"title": 100, "description": 5000, "max_hashtags": 15},
    "instagram": {"title": 125, "description": 2200, "max_hashtags": 30},
    # LinkedIn rewards restraint — keep hashtags to a handful, not a wall.
    "linkedin": {"title": 150, "description": 3000, "max_hashtags": 5},
}

# Detailed, per-platform authoring rules. These encode the playbook for each
# network so the same brief reads natively in each place. The JSON shape stays
# the same (title/description/hashtags); only the guidance changes.
PLATFORM_RULES = {
    "youtube": (
        "Platform: YouTube.\n"
        "- title: a click-worthy, search-friendly title (curiosity + keywords), "
        "at most 100 characters.\n"
        "- description: keyword-rich and skimmable. Open with a 1-2 line hook, then "
        "a few short paragraphs of detail, then a call to subscribe. Include a line "
        "exactly like 'Timestamps:' followed by '00:00 Intro' as a placeholder the "
        "creator can fill in. At most 5000 characters.\n"
        "- hashtags: 10-15 relevant tags."
    ),
    "instagram": (
        "Platform: Instagram Reels.\n"
        "- title: a punchy opening line/hook — these are the first ~125 characters "
        "that show before the 'more' fold, so make them count.\n"
        "- description: a full caption in a warm storytelling tone, with line breaks "
        "for readability and tasteful emoji. At most 2200 characters.\n"
        "- hashtags: 20-30 hashtags ordered broad -> niche -> micro."
    ),
    "linkedin": (
        "Platform: LinkedIn.\n"
        "- title: a strong first-person hook line.\n"
        "- description: a professional post in a first-person narrative voice. Lead "
        "with the hook line, then 3-4 short insight paragraphs, then a closing "
        "question or call to action that invites comments. Do NOT put any links in "
        "the body (LinkedIn suppresses reach on posts with links). At most 3000 "
        "characters.\n"
        "- hashtags: at most 5 relevant, professional hashtags. No hashtag spam."
    ),
}


def is_configured() -> bool:
    return bool(settings.GEMINI_API_KEY)


def _client():
    if not is_configured():
        raise ImproperlyConfigured(
            "GEMINI_API_KEY is not set. Get a free key at https://aistudio.google.com/ "
            "and add it to your .env so AI metadata generation can work."
        )
    from google import genai  # imported lazily so the app loads without a key

    return genai.Client(api_key=settings.GEMINI_API_KEY)


def _build_prompt(platform: str, brief: str, filename: str, analyzed: bool = False) -> str:
    rules = PLATFORM_RULES[platform]
    source = (
        "Watch the attached video and base everything you write on what actually "
        "happens in it — the visuals, on-screen text, actions, and mood. Use the "
        "brief only as extra context."
        if analyzed
        else "Write from the brief and filename below."
    )
    return (
        "You are an expert social media copywriter. "
        f"{source}\n\n"
        f"Video brief: {brief or '(no brief provided — infer the topic from the video/filename)'}\n"
        f"Original filename: {filename or '(unknown)'}\n\n"
        f"{rules}\n\n"
        "Every hashtag must start with '#' and contain no spaces.\n\n"
        "Respond ONLY with a JSON object of this exact shape:\n"
        '{"title": "...", "description": "...", "hashtags": ["#tag1", "#tag2"]}'
    )


def upload_for_analysis(video_url: str):
    """Download a video from its URL and upload it to the Gemini Files API.

    Returns a Gemini file handle once it's ACTIVE (ready to be referenced in a
    prompt), or None if anything goes wrong — callers fall back to brief-only
    generation, so this never raises into the request.
    """
    if not video_url:
        return None
    try:
        client = _client()
        resp = requests.get(video_url, timeout=VIDEO_DOWNLOAD_TIMEOUT, stream=True)
        resp.raise_for_status()

        suffix = os.path.splitext(video_url.split("?")[0])[1] or ".mp4"
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                for chunk in resp.iter_content(chunk_size=1 << 20):
                    tmp.write(chunk)
                tmp_path = tmp.name

            gfile = client.files.upload(file=tmp_path)
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)

        # Gemini processes video asynchronously; wait for it to become ACTIVE.
        deadline = time.time() + VIDEO_PROCESS_TIMEOUT
        while gfile.state.name == "PROCESSING" and time.time() < deadline:
            time.sleep(VIDEO_POLL_INTERVAL)
            gfile = client.files.get(name=gfile.name)

        if gfile.state.name != "ACTIVE":
            logger.warning("Gemini video not ready (state=%s); using brief only", gfile.state.name)
            return None
        logger.info("Video uploaded to Gemini for analysis: %s", gfile.name)
        return gfile
    except Exception as exc:  # download / SDK / timeout — degrade gracefully
        logger.warning("Video analysis unavailable, falling back to brief: %s", exc)
        return None


def cleanup_analysis(gfile) -> None:
    """Best-effort delete of an uploaded Gemini file (they also auto-expire ~48h)."""
    if not gfile:
        return
    try:
        _client().files.delete(name=gfile.name)
    except Exception as exc:  # pragma: no cover - cleanup is non-critical
        logger.debug("Could not delete Gemini file %s: %s", getattr(gfile, "name", "?"), exc)


def generate_metadata(platform: str, brief: str = "", filename: str = "", video_file=None) -> dict:
    """
    Generate platform-specific metadata.

    If `video_file` (a Gemini file handle from upload_for_analysis) is given, the
    model watches the actual video; otherwise it works from the brief + filename.

    Returns {"title": str, "description": str, "hashtags": str} where hashtags is
    a single space-separated string (how the Video/AIContent model stores it).
    """
    if platform not in PLATFORM_LIMITS:
        raise ValueError(f"Unknown platform: {platform}")

    from google.genai import types

    client = _client()
    prompt = _build_prompt(platform, brief, filename, analyzed=bool(video_file))
    contents = [video_file, prompt] if video_file else prompt
    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=contents,
        config=types.GenerateContentConfig(response_mime_type="application/json"),
    )

    try:
        data = json.loads(response.text)
    except (json.JSONDecodeError, TypeError) as exc:
        logger.error("Gemini returned non-JSON for %s: %s", platform, exc)
        raise ValueError("AI response could not be parsed. Try regenerating.") from exc

    hashtags = data.get("hashtags", [])
    if isinstance(hashtags, list):
        hashtags = " ".join(str(h).strip() for h in hashtags if str(h).strip())

    logger.info("Generated %s metadata via %s", platform, GEMINI_MODEL)
    return {
        "title": (data.get("title") or "").strip(),
        "description": (data.get("description") or "").strip(),
        "hashtags": hashtags.strip(),
        "model": GEMINI_MODEL,
    }


def validate_metadata(platform: str, title: str, description: str, hashtags: str) -> list[str]:
    """
    Return a list of human-readable limit violations (empty list = all good).

    We validate but do NOT silently truncate — the user reviews and edits before
    scheduling, so we surface problems and let them decide how to fix them.
    """
    limits = PLATFORM_LIMITS.get(platform)
    if not limits:
        return [f"Unknown platform: {platform}"]

    violations = []
    if len(title) > limits["title"]:
        violations.append(f"Title is {len(title)} chars; max {limits['title']}.")
    if len(description) > limits["description"]:
        violations.append(f"Description is {len(description)} chars; max {limits['description']}.")

    tag_count = len([t for t in hashtags.split() if t.startswith("#")])
    if tag_count > limits["max_hashtags"]:
        violations.append(f"{tag_count} hashtags; max {limits['max_hashtags']}.")

    return violations
