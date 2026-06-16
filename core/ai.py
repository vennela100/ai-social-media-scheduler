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
import re
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

# Per-platform instruction blocks. Each asks for the platform's own JSON shape;
# _normalize() maps those keys back onto our (title, description, hashtags) trio.
PLATFORM_PROMPTS = {
    "youtube": (
        "Generate for YouTube:\n"
        "- A click-worthy title (max 100 chars)\n"
        "- A detailed SEO-optimized description with keyword-rich paragraphs "
        "(max 5000 chars). Include a 'Timestamps:' section with '00:00 Intro' "
        "as a placeholder.\n"
        "- 10-15 relevant tags as a comma-separated list\n"
        'Return as JSON: {"title": "", "description": "", "tags": ""}'
    ),
    "instagram": (
        "Generate for Instagram:\n"
        "- A punchy opening line (max 125 chars)\n"
        "- Full caption with a storytelling tone and line breaks for readability\n"
        "- 20-30 hashtags grouped broad, niche, micro — placed at the end\n"
        'Return as JSON: {"caption": "", "hashtags": ""}'
    ),
    "linkedin": (
        "Generate a professional LinkedIn post:\n"
        "- Strong hook line first\n"
        "- 3-4 short insight paragraphs in a first-person tone\n"
        "- Closing question or call to action to drive comments\n"
        "- Max 5 relevant hashtags only, no link spam\n"
        'Return as JSON: {"post": "", "hashtags": ""}'
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


def _build_prompt(platform: str, title: str, description: str, filename: str, analyzed: bool = False) -> str:
    # The user's description is the source of truth; title/filename fill gaps.
    desc = description.strip() or f"(no description provided — infer the topic from the title/filename: {filename or 'unknown'})"
    ctx_title = title.strip() or filename or "(untitled)"
    watch = (
        "Also watch the attached video and let what actually happens in it inform "
        "the content.\n"
        if analyzed
        else ""
    )
    return (
        "You are an expert social media copywriter.\n"
        f'Based on this video description: "{desc}"\n'
        f"Video title/context: {ctx_title}\n"
        f"{watch}\n"
        f"{PLATFORM_PROMPTS[platform]}"
    )


def _split_tags(raw) -> list[str]:
    """Tokenize tags/hashtags from a list or a comma/space string; strip '#'."""
    if isinstance(raw, (list, tuple)):
        items = [str(x) for x in raw]
    else:
        items = re.split(r"[,\n\s]+", str(raw or ""))
    return [t.strip().lstrip("#") for t in items if t.strip().strip("#")]


def _format_tags(platform: str, tokens: list[str]) -> str:
    """YouTube stores plain comma-separated tags; IG/LI store space-separated #tags."""
    if platform == "youtube":
        return ", ".join(tokens)
    return " ".join(f"#{t}" for t in tokens)


def _normalize(platform: str, data: dict) -> tuple[str, str, str]:
    """Map a platform's JSON keys onto (title, description, hashtags)."""
    if platform == "youtube":
        return data.get("title", ""), data.get("description", ""), data.get("tags", "")
    if platform == "instagram":
        caption = data.get("caption", "")
        return caption.split("\n", 1)[0][:125], caption, data.get("hashtags", "")
    # linkedin
    post = data.get("post", "")
    return post.split("\n", 1)[0][:150], post, data.get("hashtags", "")


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


def generate_metadata(platform: str, title: str = "", description: str = "", filename: str = "", video_file=None) -> dict:
    """
    Generate platform-specific metadata, driven by the user's title + description.

    The user's words are the primary context; `filename` is only a fallback when
    they leave the description blank. If `video_file` (a Gemini handle) is given,
    the model also watches the footage.

    Returns {"title", "description", "hashtags", "model"} already trimmed to the
    platform's limits, where hashtags is one string (comma-separated tags for
    YouTube, space-separated #tags for Instagram/LinkedIn).
    """
    if platform not in PLATFORM_LIMITS:
        raise ValueError(f"Unknown platform: {platform}")

    from google.genai import types

    client = _client()
    prompt = _build_prompt(platform, title, description, filename, analyzed=bool(video_file))
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

    out_title, out_desc, raw_tags = _normalize(platform, data)
    limits = PLATFORM_LIMITS[platform]
    # Belt-and-braces: trim to platform limits even though the prompt asks within
    # them, so a slightly over-eager model can't produce an unschedulable draft.
    tokens = _split_tags(raw_tags)[: limits["max_hashtags"]]

    logger.info("Generated %s metadata via %s", platform, GEMINI_MODEL)
    return {
        "title": (out_title or "").strip()[: limits["title"]],
        "description": (out_desc or "").strip()[: limits["description"]],
        "hashtags": _format_tags(platform, tokens),
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

    tag_count = len(_split_tags(hashtags))
    if tag_count > limits["max_hashtags"]:
        violations.append(f"{tag_count} hashtags/tags; max {limits['max_hashtags']}.")

    return violations
