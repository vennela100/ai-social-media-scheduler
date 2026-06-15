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

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

logger = logging.getLogger("scheduler")

GEMINI_MODEL = "gemini-2.5-flash"

# Per-platform hard limits we generate within and validate against. These are
# the public API/UX caps; tune if a platform changes them.
PLATFORM_LIMITS = {
    "youtube": {"title": 100, "description": 5000, "max_hashtags": 15},
    "instagram": {"title": 125, "description": 2200, "max_hashtags": 30},
    "linkedin": {"title": 150, "description": 3000, "max_hashtags": 10},
}

# How each platform "feels" — steers tone so the same brief reads natively.
PLATFORM_STYLE = {
    "youtube": "an engaging, search-friendly YouTube title and a description that front-loads value and includes a call to subscribe",
    "instagram": "a punchy first-line hook and a warm, emoji-friendly caption made for the feed",
    "linkedin": "a professional, insight-led post with a credible, no-hype tone",
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


def _build_prompt(platform: str, brief: str, filename: str) -> str:
    limits = PLATFORM_LIMITS[platform]
    style = PLATFORM_STYLE[platform]
    return (
        f"You are a social media copywriter. Write {style}.\n\n"
        f"Video brief: {brief or '(no brief provided)'}\n"
        f"Original filename: {filename or '(unknown)'}\n\n"
        f"Constraints:\n"
        f"- title: at most {limits['title']} characters\n"
        f"- description: at most {limits['description']} characters\n"
        f"- hashtags: at most {limits['max_hashtags']}, each starting with '#', no spaces inside a tag\n\n"
        f"Respond ONLY with a JSON object of this exact shape:\n"
        f'{{"title": "...", "description": "...", "hashtags": ["#tag1", "#tag2"]}}'
    )


def generate_metadata(platform: str, brief: str = "", filename: str = "") -> dict:
    """
    Generate platform-specific metadata.

    Returns {"title": str, "description": str, "hashtags": str} where hashtags is
    a single space-separated string (how the Video/AIContent model stores it).
    """
    if platform not in PLATFORM_LIMITS:
        raise ValueError(f"Unknown platform: {platform}")

    from google.genai import types

    client = _client()
    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=_build_prompt(platform, brief, filename),
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
