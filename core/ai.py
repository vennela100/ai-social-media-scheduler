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

# Skip real media analysis for files bigger/longer than these — downloading a
# huge clip and waiting on Gemini to process it risks OOM/timeouts on small
# runners, so such videos degrade to text-only generation instead of crashing.
# Env-overridable; set either to 0 to disable that particular check.
def _env_int(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    return int(raw) if raw else default


MEDIA_ANALYSIS_MAX_BYTES = _env_int("MEDIA_ANALYSIS_MAX_BYTES", 512 * 1024 * 1024)
MEDIA_ANALYSIS_MAX_SECONDS = _env_int("MEDIA_ANALYSIS_MAX_SECONDS", 300)

# Per-platform hard limits we generate within and validate against. These are
# the public API/UX caps; tune if a platform changes them.
PLATFORM_LIMITS = {
    "youtube": {"title": 100, "description": 5000, "max_hashtags": 15},
    "instagram": {"title": 125, "description": 2200, "max_hashtags": 30},
    # LinkedIn rewards restraint — keep hashtags to a handful, not a wall.
    "linkedin": {"title": 150, "description": 3000, "max_hashtags": 5},
}

# Rules every platform prompt ends with: platform-NATIVE output, strict
# grounding in the analysis + user text, and exactly one call-to-action.
SHARED_PROMPT_RULES = (
    "Rules that always apply:\n"
    "- Write platform-NATIVE content for THIS platform only: its own hook, structure and "
    "tone — never text that could be lightly reworded onto another platform.\n"
    "- Ground every claim strictly in the content details above (the media analysis and "
    "the creator's own words). Never invent facts, numbers, names or events not present in them.\n"
    "- Exactly ONE call-to-action in the whole output.\n"
    "- Stay comfortably within the character limits — trimming is a safety net, not the plan.\n\n"
)

# Reach-optimised, media-type-aware prompts (always on — no toggle), tuned to
# each platform's 2026 ranking signals: YouTube = satisfaction + session
# contribution, Instagram = watch time + DM shares + saves, LinkedIn = dwell
# time + comment depth. Each embeds [[MEDIA_TYPE]] / [[DESC]] / [[CATEGORY]]
# placeholders that _build_prompt fills, and asks for the platform's own JSON
# shape; _normalize() maps those keys back onto our (title, description,
# hashtags) trio.
PLATFORM_PROMPTS = {
    "youtube": (
        "You are a YouTube strategist optimizing for the 2026 algorithm, which ranks by viewer "
        "SATISFACTION and SESSION CONTRIBUTION (how much good watching happens after the click) — "
        "not raw clicks. Tone: clear and searchable.\n\n"
        'Media type: "[[MEDIA_TYPE]]"\n'
        'What the content contains: "[[DESC]]"\n'
        'Category: "[[CATEGORY]]"\n\n'
        "TITLE (max 100 chars): main keyword in the first 3-4 words, then an HONEST promise of "
        "the outcome the viewer gets. The title must match what the content actually delivers — "
        'over-promising and clickbait are forbidden. Prefer the pattern "[Outcome] in '
        '[method/time] | [Series]".\n\n'
        "DESCRIPTION (max 5000 chars) as blocks separated by blank lines, in this order:\n"
        "1. First 2 lines: repeat the promise with the main keyword — this is the search snippet.\n"
        "2. A 2-3 sentence overview using keywords naturally; if the content is part of a series, "
        'say so ("Episode X of the <series> series").\n'
        '3. A "⏱ Timestamps" section ONLY if the content details show distinct segments — '
        "otherwise omit this block entirely.\n"
        '4. A "▶ Watch next" block with placeholder links to the next episode and the playlist '
        "(session-contribution signal).\n"
        '5. A "🔗 Links" block with a GitHub repo placeholder.\n'
        '6. A subscribe line with a SPECIFIC promise (e.g. "New Django episode every Tuesday in '
        'Telugu") — never a generic "please subscribe".\n\n'
        "TAGS: 10-15 comma-separated tags mixing broad and specific terms.\n\n"
        + SHARED_PROMPT_RULES +
        "Return as JSON only:\n"
        '{"title": "", "description": "", "tags": ""}'
    ),
    "instagram": (
        "You are an Instagram strategist optimizing for the 2026 algorithm, which ranks by WATCH "
        "TIME, DM SHARES and SAVES — not likes. Tone: casual, energetic, share-friendly.\n\n"
        'Media type: "[[MEDIA_TYPE]]"\n'
        'What the content contains: "[[DESC]]"\n'
        'Category: "[[CATEGORY]]"\n\n'
        "If media_type is image: line 1 references what the viewer is seeing and why it matters.\n"
        "If media_type is video: write for a Reel.\n\n"
        "CAPTION (max 2200 chars):\n"
        "- Line 1 (under 125 chars): a hook engineered to be either SHARE-worthy "
        '("Send this to a friend who...") or SAVE-worthy ("The X things I use daily 👇"). '
        "1-2 emojis max.\n"
        "- Body: 3-4 short lines, one idea per line. Naturally include ONE searchable keyword "
        'phrase (e.g. "django tutorial for beginners") — captions are indexed for search.\n'
        "- End with exactly ONE CTA and it must be a share or save action "
        '("Share this with your study buddy" / "Save for later 🔖") — NEVER "like this post".\n\n'
        "HASHTAGS: only 5-10 focused, lowercase, niche-relevant hashtags (not 30) — the "
        "keyword-rich caption matters more than hashtags. Never put hashtags in the caption body.\n\n"
        + SHARED_PROMPT_RULES +
        "Return as JSON only:\n"
        '{"caption": "", "hashtags": ""}'
    ),
    "linkedin": (
        "You are a LinkedIn strategist optimizing for the 2026 algorithm, which ranks by DWELL "
        "TIME and COMMENT DEPTH — thin posts die. Tone: professional, first-person, story-driven.\n\n"
        'Media type: "[[MEDIA_TYPE]]"\n'
        'What the content contains: "[[DESC]]"\n'
        'Category: "[[CATEGORY]]"\n\n'
        "If media_type is image: the opening references what the image shows and the post tells "
        "the story or insight it illustrates.\n"
        "If media_type is video: write a first-person narrative about the insight or story from "
        "the video.\n\n"
        "POST (max 3000 chars):\n"
        "- First 150 chars: a hook with a concrete result, a number, or a contrarian take. "
        'No greetings, no "excited to share".\n'
        "- Body: 4-6 short paragraphs (1-2 lines each) following a story arc: the Problem → what "
        "was built → the ONE interesting decision along the way → what was learned. Make it "
        "substantial enough to create real dwell time — never thin.\n"
        "- End with exactly ONE genuine, specific question that invites opinions — never a lazy "
        '"thoughts?".\n'
        "- NO URLs anywhere in the post body.\n\n"
        "HASHTAGS: exactly 3-5, PascalCase, professional.\n\n"
        + SHARED_PROMPT_RULES +
        "Return as JSON only:\n"
        '{"post": "", "hashtags": "", "first_comment_reminder": "Paste your video or image link in the first comment '
        '— never in the post body, it kills reach by 50%"}'
    ),
}

# Rule appended to every prompt for image posts so the copy never implies motion.
IMAGE_RULE = (
    "Since this is an image not a video, do not use the words watching or in this "
    "video anywhere. Write as if the viewer is looking at a photo or graphic."
)

# Prompt for the one-time, platform-agnostic media analysis. We want raw factual
# observation here — NOT marketing copy — so the per-platform prompts can each
# turn the same neutral facts into their own tuned title/description/hashtags.
MEDIA_ANALYSIS_PROMPT = (
    "You are a neutral media analyst. Describe factually and specifically what is "
    "actually shown and heard in this {media}. Cover, where visible: the main "
    "subjects and objects, the actions or events taking place, the setting or "
    "location, the overall mood or tone, and any on-screen text or clearly spoken "
    "words. Be concrete and grounded in what you can actually observe — do NOT "
    "invent details, and do NOT add marketing language, hashtags, emojis, or "
    "platform-specific phrasing. This is raw source material for later copywriting. "
    "Return 4-8 sentences of plain prose, no headings or lists."
)


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


def _build_prompt(platform: str, title: str, description: str, filename: str,
                  media_type: str = "video", category: str = "", analyzed: bool = False,
                  media_analysis: str = "") -> str:
    # Grounding priority: what's actually in the media (analysis) + the creator's
    # own note are the strongest signal; title, then filename, are only fallbacks.
    analysis = (media_analysis or "").strip()
    user_desc = description.strip()
    if analysis and user_desc:
        desc = (
            f"What is actually in the {media_type} (from analysis of the file): {analysis}\n\n"
            f"The creator's own note about it: {user_desc}"
        )
    elif analysis:
        desc = f"What is actually in the {media_type} (from analysis of the file): {analysis}"
    elif user_desc:
        desc = user_desc
    else:
        desc = f"infer the topic from the title/filename: {(title.strip() or filename) or 'unknown'}"
    cat = category.strip() or "general"
    prompt = (
        PLATFORM_PROMPTS[platform]
        .replace("[[MEDIA_TYPE]]", media_type)
        .replace("[[DESC]]", desc)
        .replace("[[CATEGORY]]", cat)
    )
    if media_type == "image":
        prompt += "\n\n" + IMAGE_RULE
    if analyzed:
        prompt += "\n\nAlso watch the attached video and let what actually happens in it inform the content."
    return prompt


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


# Gemini intermittently returns 503 UNAVAILABLE ("this model is experiencing
# high demand") — a transient, server-side overload, not a problem with our
# request. Retry a couple of times with exponential backoff so a momentary spike
# recovers automatically instead of surfacing to the user as a hard failure.
# On Render's free tier (2-3 workers) we keep this SHORT so a blocked worker
# doesn't starve the health check — worst-case total ≈ base_delay × 2 = 3s.
GENERATE_MAX_ATTEMPTS = _env_int("GENERATE_MAX_ATTEMPTS", 2)
GENERATE_RETRY_BASE_DELAY = 1.5  # seconds; doubles each retry (1.5s, 3s, ...)

# Hard wall-clock cap on a single generate_content call (including retries).
# Prevents a hanging Gemini response from tying up a gunicorn worker forever,
# which would eventually starve Render's /healthz/ check and trigger alerts.
GENERATE_TIMEOUT = _env_int("GENERATE_TIMEOUT", 25)  # seconds


def _is_transient_overload(exc) -> bool:
    """True only for retryable Gemini overloads (503), not genuine failures.

    A 503 means "try again later"; a bad API key, quota exhaustion (429), or an
    unparseable response will not fix themselves on retry, so we don't waste time
    looping on those — they fail fast and reach the user as an actionable error.
    """
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    if code == 503:
        return True
    text = str(exc).lower()
    return "503" in text or "unavailable" in text or "overloaded" in text or "high demand" in text


def _generate_with_retry(client, **kwargs):
    """Call generate_content, retrying transient overloads with backoff.

    Also enforces GENERATE_TIMEOUT as a wall-clock cap so a single call can
    never block a worker longer than that (important for health-check liveness).
    """
    deadline = time.time() + GENERATE_TIMEOUT
    for attempt in range(1, GENERATE_MAX_ATTEMPTS + 1):
        try:
            return client.models.generate_content(**kwargs)
        except Exception as exc:
            remaining = deadline - time.time()
            if attempt == GENERATE_MAX_ATTEMPTS or not _is_transient_overload(exc):
                raise
            delay = GENERATE_RETRY_BASE_DELAY * (2 ** (attempt - 1))
            if remaining < delay + 2:  # not enough time for another attempt
                raise
            logger.warning(
                "Gemini overloaded (attempt %d/%d), retrying in %.1fs: %s",
                attempt, GENERATE_MAX_ATTEMPTS, delay, exc,
            )
            time.sleep(delay)


def generate_metadata(platform: str, title: str = "", description: str = "", filename: str = "",
                      media_type: str = "video", category: str = "", video_file=None,
                      media_analysis: str = "") -> dict:
    """
    Generate platform-specific metadata, grounded in the real media when available.

    `media_analysis` is the one-time neutral description of what's actually in the
    file (see analyze_media); when present it becomes the primary grounding context
    alongside the creator's own `description`. `media_type` ("video"/"image") and
    `category` steer the reach-optimised prompts; for images an extra rule forbids
    motion words. `filename`/`title` are only fallbacks when both analysis and
    description are blank. If `video_file` (a Gemini handle) is given, the model
    also watches the footage inline.

    Returns {"title", "description", "hashtags", "model"} already trimmed to the
    platform's limits, where hashtags is one string (comma-separated tags for
    YouTube, space-separated #tags for Instagram/LinkedIn).
    """
    if platform not in PLATFORM_LIMITS:
        raise ValueError(f"Unknown platform: {platform}")

    from google.genai import types

    client = _client()
    prompt = _build_prompt(
        platform, title, description, filename,
        media_type=media_type, category=category, analyzed=bool(video_file),
        media_analysis=media_analysis,
    )
    contents = [video_file, prompt] if video_file else prompt
    response = _generate_with_retry(
        client,
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


def _analyze_image(client, image_url: str) -> str:
    """Fetch an image and ask Gemini to describe it inline (no Files API needed)."""
    from google.genai import types

    resp = requests.get(image_url, timeout=VIDEO_DOWNLOAD_TIMEOUT)
    resp.raise_for_status()
    mime = (resp.headers.get("Content-Type") or "").split(";")[0].strip()
    if not mime.startswith("image/"):
        mime = "image/jpeg"  # sensible default when Cloudinary omits the header
    part = types.Part.from_bytes(data=resp.content, mime_type=mime)
    response = _generate_with_retry(
        client, model=GEMINI_MODEL,
        contents=[part, MEDIA_ANALYSIS_PROMPT.format(media="image")],
    )
    return (response.text or "").strip()


def _analyze_video(client, video_url: str) -> str:
    """Upload a video to the Files API (polls until ACTIVE), describe it, clean up.

    Returns "" if the upload/processing didn't complete — upload_for_analysis
    already swallows its own errors and returns None, so we degrade quietly.
    """
    gfile = upload_for_analysis(video_url)
    if gfile is None:
        return ""
    try:
        response = _generate_with_retry(
            client, model=GEMINI_MODEL,
            contents=[gfile, MEDIA_ANALYSIS_PROMPT.format(media="video")],
        )
        return (response.text or "").strip()
    finally:
        cleanup_analysis(gfile)


def analysis_skip_reason(video) -> str | None:
    """Why this media should NOT be analyzed (too big/long), or None if it's fine.

    Decided from the size/duration captured at upload, so we bail BEFORE spending
    time downloading. A 0 cap disables that check; a 0/unknown value on the video
    means "we don't know" and does not trip the cap.
    """
    size = getattr(video, "source_size_bytes", 0) or 0
    if MEDIA_ANALYSIS_MAX_BYTES and size > MEDIA_ANALYSIS_MAX_BYTES:
        return f"file is {size} bytes (over {MEDIA_ANALYSIS_MAX_BYTES}-byte cap)"
    seconds = getattr(video, "duration_seconds", 0) or 0
    if MEDIA_ANALYSIS_MAX_SECONDS and seconds > MEDIA_ANALYSIS_MAX_SECONDS:
        return f"duration is {seconds}s (over {MEDIA_ANALYSIS_MAX_SECONDS}s cap)"
    return None


def analyze_media(video) -> str:
    """Produce and cache ONE neutral, factual description of what's in the media.

    Computed once from the real file and stored on Video.ai_media_analysis, then
    reused as grounding context for every platform's generation. Idempotent:
    returns the cached value if already present.

    Best-effort by design — on any failure (download, upload, timeout, SDK) it
    logs a warning and returns "", so callers fall straight back to text-only
    generation and the user's request is never hard-failed.
    """
    existing = (getattr(video, "ai_media_analysis", "") or "").strip()
    if existing:
        return existing
    if not is_configured() or not video.file_url:
        return ""

    try:
        client = _client()
        if video.media_type == "image":
            text = _analyze_image(client, video.file_url)
        else:
            text = _analyze_video(client, video.file_url)
    except Exception as exc:  # download / SDK / parse — degrade to text-only
        logger.warning(
            "Media analysis failed for video %s, using text only: %s",
            getattr(video, "pk", "?"), exc,
        )
        return ""

    text = (text or "").strip()
    if text:
        # Persist without touching other columns (a parallel generate request may
        # be writing AIContent rows for the same video at the same moment).
        from .models import Video

        Video.objects.filter(pk=video.pk).update(ai_media_analysis=text)
        video.ai_media_analysis = text  # keep the in-memory instance in sync
        logger.info("Analyzed media for video %s (%d chars)", video.pk, len(text))
    return text


# Days of the week the model is allowed to return, so we can validate its output
# and the front-end can map a name to a real calendar date.
_WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

_FALLBACK_POST_TIMES = {
    "youtube": [
        {"day": "Saturday", "time": "10:00", "reason": "Weekend viewers have longer watch sessions"},
        {"day": "Wednesday", "time": "18:00", "reason": "Evening browsing after work or school"},
        {"day": "Sunday", "time": "11:00", "reason": "Strong discovery window before the week starts"},
    ],
    "instagram": [
        {"day": "Tuesday", "time": "19:00", "reason": "High evening Reels scrolling"},
        {"day": "Thursday", "time": "20:00", "reason": "Strong save and share window"},
        {"day": "Sunday", "time": "18:00", "reason": "Relaxed browsing before the week starts"},
    ],
    "linkedin": [
        {"day": "Tuesday", "time": "09:00", "reason": "Workday feed check-in"},
        {"day": "Wednesday", "time": "12:00", "reason": "Lunch break professional browsing"},
        {"day": "Thursday", "time": "17:00", "reason": "End-of-day reflection window"},
    ],
}


def fallback_post_times(platform: str, count: int = 3) -> list[dict]:
    """Return built-in posting slots when Gemini is unavailable or rate-limited."""
    rows = _FALLBACK_POST_TIMES.get(platform, _FALLBACK_POST_TIMES["instagram"])
    return [dict(row) for row in rows[:count]]


def suggest_post_times(platform: str, category: str = "", count: int = 3) -> list[dict]:
    """Ask Gemini for the best times to post on a platform for a given niche.

    Returns up to `count` slots like
        {"day": "Tuesday", "time": "18:00", "reason": "Evening commute scroll"}
    where time is 24-hour HH:MM in the audience's local time. Raises
    ImproperlyConfigured if no GEMINI_API_KEY (caller surfaces it). Malformed
    rows are dropped; an empty list is a valid (if unhelpful) result.
    """
    if platform not in PLATFORM_LIMITS:
        raise ValueError(f"Unknown platform: {platform}")

    from google.genai import types

    client = _client()
    niche = category.strip() or "general content"
    prompt = (
        f"You are a social media scheduling expert. For a {platform} creator in the "
        f'"{niche}" niche, give the {count} best times to publish for maximum reach '
        "and engagement, based on typical audience behavior for that platform and niche.\n\n"
        "Use the audience's LOCAL time. Spread suggestions across different days.\n"
        "Return JSON only, an array of objects:\n"
        '{"slots": [{"day": "Tuesday", "time": "18:00", "reason": "short why"}]}\n'
        "day must be a full weekday name; time must be 24-hour HH:MM; reason ≤ 70 chars."
    )
    response = _generate_with_retry(
        client,
        model=GEMINI_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(response_mime_type="application/json"),
    )

    try:
        data = json.loads(response.text)
    except (json.JSONDecodeError, TypeError) as exc:
        logger.error("Gemini returned non-JSON for suggest_post_times: %s", exc)
        raise ValueError("AI response could not be parsed. Try again.") from exc

    rows = data.get("slots", data) if isinstance(data, dict) else data
    slots = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        day = str(row.get("day", "")).strip().capitalize()
        time = str(row.get("time", "")).strip()
        if day not in _WEEKDAYS or not re.match(r"^\d{1,2}:\d{2}$", time):
            continue
        hh, mm = (int(x) for x in time.split(":"))
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            continue
        slots.append({
            "day": day,
            "time": f"{hh:02d}:{mm:02d}",
            "reason": str(row.get("reason", "")).strip()[:70],
        })
        if len(slots) >= count:
            break
    logger.info("Suggested %d post time(s) for %s/%s", len(slots), platform, niche)
    return slots


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


def fallback_metadata(
    platform: str,
    title: str = "",
    description: str = "",
    filename: str = "",
    media_type: str = "video",
    category: str = "",
) -> dict:
    """Return a usable draft when Gemini is unavailable or temporarily failing."""
    if platform not in PLATFORM_LIMITS:
        raise ValueError(f"Unknown platform: {platform}")

    base_title = (title or filename or "New post").strip()
    base_description = (description or "").strip()
    topic = (category or "").strip()
    subject = topic or base_title

    if not base_description:
        noun = "image" if media_type == "image" else "video"
        base_description = f"Sharing this {noun}: {base_title}."

    hashtag_seed = re.findall(r"[A-Za-z0-9]+", subject.lower())
    hashtag_seed = [w for w in hashtag_seed if len(w) > 2][:4]
    if not hashtag_seed:
        hashtag_seed = ["content", "creator"]

    if platform == "youtube":
        tags = ", ".join(hashtag_seed[:PLATFORM_LIMITS[platform]["max_hashtags"]])
        return {
            "title": base_title[:PLATFORM_LIMITS[platform]["title"]],
            "description": base_description[:PLATFORM_LIMITS[platform]["description"]],
            "hashtags": tags,
            "model": "fallback",
        }

    tags = " ".join(
        f"#{token}" for token in hashtag_seed[:PLATFORM_LIMITS[platform]["max_hashtags"]]
    )
    if platform == "instagram":
        body = f"{base_title}\n\n{base_description}"
    else:
        body = f"{base_title}\n\n{base_description}\n\nWhat would you add?"

    return {
        "title": base_title[:PLATFORM_LIMITS[platform]["title"]],
        "description": body[:PLATFORM_LIMITS[platform]["description"]],
        "hashtags": tags,
        "model": "fallback",
    }
