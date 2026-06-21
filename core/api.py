"""
JSON API for the Next.js frontend.

These endpoints return camelCase JSON shaped exactly like the TypeScript types in
`frontend/src/lib/types.ts`, so the frontend's data layer is a thin fetch wrapper
with zero shape-mapping. Auth is Django's session (same cookie the templates use);
the Next dev server proxies `/api/*` here so the browser stays same-origin, which
sidesteps CORS. Auth is the session; mutations are CSRF-protected via the standard
double-submit token (GET /api/auth/csrf/ sets the cookie, the client echoes it in
the X-CSRFToken header).
"""

import functools
import json
import logging
import threading

from django.conf import settings
from django.contrib.auth import authenticate, login, logout
from django.db.models import Count
from django.http import JsonResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone
from django.utils.crypto import get_random_string
from django.views.decorators.csrf import ensure_csrf_cookie

from . import ai, analytics, instagram, linkedin, stats, storage as storage_mod, youtube
from .forms import media_type_for
from .models import AIContent, ScheduledPost, SocialAccount, Video
from .storage import get_usage

logger = logging.getLogger("scheduler")


# ── helpers ──────────────────────────────────────────────────────────────────

def _iso(value):
    return value.isoformat() if value else None


def _poster_url(v: Video):
    """A preview image for a media asset.

    Images preview themselves; videos get a Cloudinary start-frame poster
    (`so_0` = offset 0 = the first frame), so the UI shows the opening shot
    instead of a blank tile. Falls back to a stored thumbnail, else None.
    """
    if v.thumbnail_url:
        return v.thumbnail_url
    url = v.file_url or ""
    if v.media_type == Video.MediaType.IMAGE:
        return url or None
    if "res.cloudinary.com" in url and "/upload/" in url:
        base = url.rsplit(".", 1)[0]  # drop the .mp4 extension
        return base.replace("/upload/", "/upload/so_0,w_480,h_270,c_fill,q_auto/") + ".jpg"
    return None


def api_login_required(view):
    """Like @login_required but returns JSON 401 instead of an HTML redirect."""

    @functools.wraps(view)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return JsonResponse({"detail": "Authentication required."}, status=401)
        return view(request, *args, **kwargs)

    return wrapper


def _body(request):
    if not request.body:
        return {}
    try:
        return json.loads(request.body)
    except (ValueError, TypeError):
        return {}


# ── serializers ────────────────────────────────────────────────────────────

# How a SocialAccount.health_level() maps onto the frontend's AccountStatus.
_ACCOUNT_STATUS = {
    "auto": "connected",
    "good": "connected",
    "warning": "expiring",
    "urgent": "expiring",
    "expired": "expired",
}


def draft_dict(c: AIContent) -> dict:
    return {
        "id": c.id,
        "platform": c.platform,
        "title": c.generated_title,
        "description": c.generated_description,
        "hashtags": c.generated_hashtags,
        "genStatus": c.generation_status,
    }


def post_dict(p: ScheduledPost) -> dict:
    out = {
        "id": p.id,
        "platform": p.social_account.platform,
        "status": p.status,
        "visibility": p.visibility,
        "scheduledTime": _iso(p.scheduled_time_utc),
        "caption": p.final_caption,
        "stats": {
            "views": p.stat_views,
            "likes": p.stat_likes,
            "comments": p.stat_comments,
        },
    }
    if p.last_error:
        out["lastError"] = p.last_error
    return out


def video_dict(v: Video) -> dict:
    return {
        "id": v.id,
        "filename": v.original_filename or f"video-{v.id}",
        "mediaType": v.media_type,
        "title": v.user_title or v.original_filename or f"Upload #{v.id}",
        "description": v.user_description,
        "category": v.category,
        "thumbnailUrl": _poster_url(v),
        "sizeBytes": v.source_size_bytes,
        "sourceArchived": v.source_deleted,
        "uploadedAt": _iso(v.uploaded_at),
        "drafts": [draft_dict(c) for c in v.ai_contents.all()],
        "posts": [post_dict(p) for p in v.scheduled_posts.all()],
    }


def account_dict(a: SocialAccount) -> dict:
    handle = a.platform_account_id or a.get_platform_display()
    return {
        "platform": a.platform,
        "handle": handle,
        "status": _ACCOUNT_STATUS.get(a.health_level(), "connected"),
        "tokenExpiresAt": _iso(a.token_expires_at) if not a.auto_refreshes() else None,
        "autoRefreshes": a.auto_refreshes(),
    }


def _user_videos(request):
    return (
        Video.objects.filter(user=request.user)
        .prefetch_related("ai_contents", "scheduled_posts__social_account")
    )


# ── auth ──────────────────────────────────────────────────────────────────

def auth_login(request):
    if request.method != "POST":
        return JsonResponse({"detail": "Method not allowed."}, status=405)
    data = _body(request)
    user = authenticate(
        request,
        username=(data.get("username") or "").strip(),
        password=data.get("password") or "",
    )
    if user is None:
        return JsonResponse({"detail": "Invalid username or password."}, status=400)
    login(request, user)
    return JsonResponse({"username": user.username})


def auth_logout(request):
    logout(request)
    return JsonResponse({"ok": True})


def auth_me(request):
    if not request.user.is_authenticated:
        return JsonResponse({"detail": "Not authenticated."}, status=401)
    return JsonResponse({"username": request.user.username})


@ensure_csrf_cookie
def auth_csrf(request):
    """Set the csrftoken cookie. The client reads it and echoes it back in the
    X-CSRFToken header on every mutating request, satisfying Django's CSRF check."""
    return JsonResponse({"ok": True})


# ── reads ────────────────────────────────────────────────────────────────

@api_login_required
def videos(request):
    return JsonResponse(
        [video_dict(v) for v in _user_videos(request)], safe=False
    )


@api_login_required
def video(request, pk):
    v = _user_videos(request).filter(pk=pk).first()
    if not v:
        return JsonResponse({"detail": "Not found."}, status=404)
    return JsonResponse(video_dict(v))


@api_login_required
def accounts(request):
    accs = SocialAccount.objects.filter(user=request.user)
    return JsonResponse([account_dict(a) for a in accs], safe=False)


@api_login_required
def posts(request):
    qs = (
        ScheduledPost.objects.filter(video__user=request.user)
        .select_related("video", "social_account")
        .order_by("-scheduled_time_utc")
    )
    out = []
    for p in qs:
        d = post_dict(p)
        d["video"] = {
            "id": p.video.id,
            "title": p.video.user_title or p.video.original_filename or f"Upload #{p.video.id}",
            "mediaType": p.video.media_type,
            "filename": p.video.original_filename or f"video-{p.video.id}",
            "thumbnailUrl": _poster_url(p.video),
        }
        out.append(d)
    return JsonResponse(out, safe=False)


@api_login_required
def stats_view(request):
    qs = ScheduledPost.objects.filter(video__user=request.user)
    counts = {
        row["status"]: row["count"]
        for row in qs.values("status").annotate(count=Count("id"))
    }
    summary = stats.summarize(counts)
    rate = stats.success_rate(counts)
    total_views = sum(
        p.stat_views or 0
        for p in qs.filter(status=ScheduledPost.Status.PUBLISHED)
    )
    return JsonResponse({
        "videos": Video.objects.filter(user=request.user).count(),
        "published": summary["published"],
        "scheduled": summary["queued"],
        "attention": summary["attention"],
        "successRate": None if rate is None else round(rate * 100),
        "totalViews": total_views,
    })


@api_login_required
def storage_view(request):
    videos_qs = list(
        Video.objects.filter(user=request.user).prefetch_related("scheduled_posts")
    )
    user_active = sum(
        v.source_size_bytes or 0 for v in videos_qs if not v.source_deleted
    )
    cleanable = sum(
        1 for v in videos_qs
        if not v.source_deleted and v.cloudinary_public_id and v.is_fully_published()
    )
    usage = get_usage()
    if usage:
        used = usage["storage_bytes"]
        assets = usage["assets"]
        plan = usage["plan"]
        percent = round(usage.get("credits_used_percent") or 0, 1)
    else:
        used = user_active
        assets = len(videos_qs)
        plan = "Free"
        percent = 0.0
    return JsonResponse({
        "usedBytes": used,
        "assets": assets,
        "plan": plan,
        "creditsPercent": percent,
        "userActiveBytes": user_active,
        "cleanable": cleanable,
    })


# ── mutations ──────────────────────────────────────────────────────────────

@api_login_required
def refresh_all_stats(request):
    if request.method != "POST":
        return JsonResponse({"detail": "Method not allowed."}, status=405)
    result = analytics.refresh_for_user(request.user, force=True)
    return JsonResponse({"ok": True, **(result or {})})


@api_login_required
def post_refresh(request, pk):
    if request.method != "POST":
        return JsonResponse({"detail": "Method not allowed."}, status=405)
    p = (
        ScheduledPost.objects.filter(video__user=request.user, pk=pk)
        .select_related("social_account", "video")
        .first()
    )
    if not p:
        return JsonResponse({"detail": "Not found."}, status=404)
    try:
        analytics.refresh_post(p, force=True)
    except Exception as exc:  # external API may be unconfigured/unreachable
        return JsonResponse({"detail": str(exc)}, status=502)
    p.refresh_from_db()
    return JsonResponse(post_dict(p))


@api_login_required
def post_publish_now(request, pk):
    """Publish one post immediately, ignoring its scheduled time.

    Claims the post (PROCESSING) and runs the real publisher in a background
    thread — a video publish can take a while, so we return right away and let
    the queue poll for the result.
    """
    if request.method != "POST":
        return JsonResponse({"detail": "Method not allowed."}, status=405)
    p = (
        ScheduledPost.objects.filter(video__user=request.user, pk=pk)
        .select_related("social_account", "video")
        .first()
    )
    if not p:
        return JsonResponse({"detail": "Not found."}, status=404)
    if p.status == ScheduledPost.Status.PUBLISHED:
        return JsonResponse({"detail": "Already published."}, status=409)
    if p.status == ScheduledPost.Status.PROCESSING:
        return JsonResponse({"detail": "Already publishing."}, status=409)

    p.status = ScheduledPost.Status.PROCESSING
    p.save(update_fields=["status", "updated_at"])

    def run(post_id):
        from django.db import close_old_connections
        from . import publishing
        close_old_connections()
        post = (
            ScheduledPost.objects.select_related("social_account", "video")
            .filter(pk=post_id).first()
        )
        if post:
            try:
                publishing.process_post(post)
            except Exception as exc:
                logger.error("Publish-now failed for post %s: %s", post_id, exc)
                ScheduledPost.objects.filter(pk=post_id).update(
                    status=ScheduledPost.Status.FAILED, last_error=str(exc)[:1000],
                )
        close_old_connections()

    threading.Thread(target=run, args=(p.id,), daemon=True).start()
    p.refresh_from_db()
    return JsonResponse(post_dict(p))


@api_login_required
def post_cancel(request, pk):
    if request.method != "POST":
        return JsonResponse({"detail": "Method not allowed."}, status=405)
    p = ScheduledPost.objects.filter(video__user=request.user, pk=pk).first()
    if not p:
        return JsonResponse({"detail": "Not found."}, status=404)
    if not p.is_editable():
        return JsonResponse({"detail": "This post can no longer be cancelled."}, status=409)
    p.delete()
    return JsonResponse({"ok": True})


@api_login_required
def video_delete(request, pk):
    if request.method != "POST":
        return JsonResponse({"detail": "Method not allowed."}, status=405)
    v = Video.objects.filter(user=request.user, pk=pk).first()
    if not v:
        return JsonResponse({"detail": "Not found."}, status=404)
    v.delete()
    return JsonResponse({"ok": True})


@api_login_required
def video_archive(request, pk):
    """Archive a fully-published source: drop bytes, keep the row + thumbnail."""
    if request.method != "POST":
        return JsonResponse({"detail": "Method not allowed."}, status=405)
    v = Video.objects.filter(user=request.user, pk=pk).first()
    if not v:
        return JsonResponse({"detail": "Not found."}, status=404)
    if not v.is_fully_published():
        return JsonResponse({"detail": "Not all posts have published yet."}, status=409)
    v.source_deleted = True
    v.source_size_bytes = 0
    v.cloudinary_public_id = ""
    v.save(update_fields=["source_deleted", "source_size_bytes", "cloudinary_public_id"])
    return JsonResponse({"ok": True})


@api_login_required
def storage_cleanup(request):
    """Archive every cleanable source in one call."""
    if request.method != "POST":
        return JsonResponse({"detail": "Method not allowed."}, status=405)
    n = 0
    for v in Video.objects.filter(user=request.user, source_deleted=False):
        if v.cloudinary_public_id and v.is_fully_published():
            v.source_deleted = True
            v.source_size_bytes = 0
            v.cloudinary_public_id = ""
            v.save(update_fields=["source_deleted", "source_size_bytes", "cloudinary_public_id"])
            n += 1
    return JsonResponse({"ok": True, "archived": n})


@api_login_required
def storage_delete_all(request):
    if request.method != "POST":
        return JsonResponse({"detail": "Method not allowed."}, status=405)
    deleted, _ = Video.objects.filter(user=request.user).delete()
    return JsonResponse({"ok": True, "deleted": deleted})


@api_login_required
def schedule_draft(request, pk):
    """Schedule a draft (AIContent) to its platform at a given UTC time."""
    if request.method != "POST":
        return JsonResponse({"detail": "Method not allowed."}, status=405)
    content = (
        AIContent.objects.filter(video__user=request.user, pk=pk)
        .select_related("video")
        .first()
    )
    if not content:
        return JsonResponse({"detail": "Not found."}, status=404)
    data = _body(request)
    account = SocialAccount.objects.filter(
        user=request.user, platform=content.platform
    ).first()
    if not account:
        return JsonResponse(
            {"detail": f"Connect your {content.get_platform_display()} account first."},
            status=409,
        )
    when = data.get("scheduledTime")
    dt = timezone.datetime.fromisoformat(when) if when else None
    if dt is None:
        return JsonResponse({"detail": "A scheduled time is required."}, status=400)
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt, timezone.get_current_timezone())

    visibility = data.get("visibility") or ScheduledPost.Visibility.PUBLIC
    caption = "\n\n".join(
        part for part in (content.generated_description, content.generated_hashtags) if part
    ).strip()
    post = ScheduledPost.objects.create(
        video=content.video,
        social_account=account,
        ai_content=content,
        final_caption=caption,
        scheduled_time_utc=dt,
        visibility=visibility,
    )
    return JsonResponse(post_dict(post), status=201)


def _get_draft(request, pk):
    return (
        AIContent.objects.filter(video__user=request.user, pk=pk)
        .select_related("video")
        .first()
    )


@api_login_required
def save_draft(request, pk):
    """Persist user edits to a draft's title / description / hashtags."""
    if request.method != "POST":
        return JsonResponse({"detail": "Method not allowed."}, status=405)
    content = _get_draft(request, pk)
    if not content:
        return JsonResponse({"detail": "Not found."}, status=404)
    data = _body(request)
    content.generated_title = data.get("title", content.generated_title)
    content.generated_description = data.get("description", content.generated_description)
    content.generated_hashtags = data.get("hashtags", content.generated_hashtags)
    content.save(update_fields=[
        "generated_title", "generated_description", "generated_hashtags",
    ])
    return JsonResponse(draft_dict(content))


@api_login_required
def regenerate_draft(request, pk):
    """Re-run Gemini for one platform draft using the video's own brief."""
    if request.method != "POST":
        return JsonResponse({"detail": "Method not allowed."}, status=405)
    content = _get_draft(request, pk)
    if not content:
        return JsonResponse({"detail": "Not found."}, status=404)
    if not ai.is_configured():
        return JsonResponse(
            {"detail": "AI isn't configured (set GEMINI_API_KEY)."}, status=409
        )
    v = content.video
    try:
        meta = ai.generate_metadata(
            content.platform,
            title=v.user_title,
            description=v.user_description,
            filename=v.original_filename,
            media_type=v.media_type,
            category=v.category,
        )
    except Exception as exc:
        content.generation_status = AIContent.GenStatus.FAILED
        content.save(update_fields=["generation_status"])
        return JsonResponse({"detail": f"Generation failed: {exc}"}, status=502)
    content.generated_title = meta.get("title", "")
    content.generated_description = meta.get("description", "")
    content.generated_hashtags = meta.get("hashtags", "")
    content.ai_model_used = meta.get("model", "")
    content.generation_status = AIContent.GenStatus.DONE
    content.save(update_fields=[
        "generated_title", "generated_description",
        "generated_hashtags", "ai_model_used", "generation_status",
    ])
    return JsonResponse(draft_dict(content))


def _generate_drafts_async(video_id: int):
    """Fill in a video's PENDING drafts with Gemini, off the request thread.

    A daemon thread is enough for local/dev and a single-process deploy; a real
    multi-worker deploy should swap this for a task queue (Celery/RQ). Each
    draft is updated independently so a partial failure still saves the rest.
    """
    from django.db import close_old_connections

    def run():
        close_old_connections()
        v = Video.objects.filter(id=video_id).first()
        if not v:
            return
        for c in v.ai_contents.filter(generation_status=AIContent.GenStatus.PENDING):
            try:
                meta = ai.generate_metadata(
                    c.platform,
                    title=v.user_title,
                    description=v.user_description,
                    filename=v.original_filename,
                    media_type=v.media_type,
                    category=v.category,
                )
                c.generated_title = meta.get("title", "")
                c.generated_description = meta.get("description", "")
                c.generated_hashtags = meta.get("hashtags", "")
                c.ai_model_used = meta.get("model", "")
                c.generation_status = AIContent.GenStatus.DONE
            except Exception as exc:
                logger.error("Async draft gen failed (%s): %s", c.platform, exc)
                c.generation_status = AIContent.GenStatus.FAILED
            c.save()
        close_old_connections()

    threading.Thread(target=run, daemon=True).start()


@api_login_required
def upload(request):
    """Create a Video from an uploaded file + draft AI content per platform.

    If Cloudinary is configured the file is stored there; otherwise (the common
    local case) we keep the metadata only so the upload → review → schedule flow
    still works end-to-end. Drafts are written by Gemini when GEMINI_API_KEY is
    set, else a sensible placeholder is seeded from the user's own title/notes.
    """
    if request.method != "POST":
        return JsonResponse({"detail": "Method not allowed."}, status=405)
    f = request.FILES.get("file")
    if not f:
        return JsonResponse({"detail": "A file is required."}, status=400)
    media_type = media_type_for(f.name)
    if media_type is None:
        return JsonResponse({"detail": "Unsupported file type."}, status=400)

    title = (request.POST.get("title") or "").strip()
    description = (request.POST.get("description") or "").strip()
    platforms = request.POST.getlist("platforms") or ["youtube", "instagram", "linkedin"]

    file_url = ""
    public_id = ""
    thumb_url = ""
    if storage_mod.is_configured():
        try:
            res = storage_mod.upload_media(f, media_type=media_type)
            file_url = res.get("file_url") or res.get("url", "")
            public_id = res.get("public_id", "")
            # Persist the Cloudinary poster (first frame for video) up front so
            # the UI never has to derive it and a thumbnail survives archival.
            thumb_url = res.get("thumbnail_url", "") or ""
        except Exception as exc:
            return JsonResponse({"detail": f"Upload failed: {exc}"}, status=502)

    video = Video.objects.create(
        user=request.user,
        media_type=media_type,
        file_url=file_url,
        thumbnail_url=thumb_url,
        cloudinary_public_id=public_id,
        thumbnail_public_id=public_id,
        original_filename=f.name,
        source_size_bytes=getattr(f, "size", 0) or 0,
        user_title=title,
        user_description=description,
    )

    # Create one draft per platform. With AI on, they start PENDING and a
    # background thread fills them in — so the request returns immediately
    # instead of blocking ~6s/platform on Gemini (which would time out the
    # proxy). The review page shows "Generating…" and auto-refreshes as each
    # draft lands. Without AI, seed a placeholder from the user's own brief.
    ai_ready = ai.is_configured()
    for platform in platforms:
        if platform not in {"youtube", "instagram", "linkedin"}:
            continue
        AIContent.objects.create(
            video=video, platform=platform,
            generated_title="" if ai_ready else (title or video.original_filename),
            generated_description="" if ai_ready else description,
            generated_hashtags="",
            generation_status=(
                AIContent.GenStatus.PENDING if ai_ready else AIContent.GenStatus.DONE
            ),
        )

    if ai_ready:
        _generate_drafts_async(video.id)

    return JsonResponse({"id": video.id}, status=201)


# ── Real OAuth (runs through the /api/* proxy so the session cookie is shared
#    between the SPA origin and Django; callbacks bounce back to the frontend) ──

_OAUTH_MODULES = {"youtube": youtube, "instagram": instagram, "linkedin": linkedin}


def _frontend_connections(status: str, platform: str):
    base = settings.FRONTEND_ORIGIN.rstrip("/")
    return redirect(f"{base}/connections?{status}={platform}")


def _resume_after_connect(account: SocialAccount):
    """Mark healthy + resume any posts paused awaiting this platform."""
    account.status = SocialAccount.Status.CONNECTED
    account.last_reminder_sent_at = None
    account.save(update_fields=["status", "last_reminder_sent_at"])
    ScheduledPost.objects.filter(
        social_account=account, status=ScheduledPost.Status.NEEDS_RECONNECT
    ).update(status=ScheduledPost.Status.PENDING, last_error="")


@api_login_required
def oauth_start(request, platform):
    """Begin a provider OAuth flow. Top-level navigation → 302 to the provider."""
    mod = _OAUTH_MODULES.get(platform)
    if mod is None:
        return JsonResponse({"detail": "Unknown platform."}, status=404)
    if not mod.is_configured():
        return _frontend_connections("error", platform)
    redirect_uri = request.build_absolute_uri(
        reverse("api:oauth_callback", args=[platform])
    )
    if platform == "youtube":
        auth_url, state, code_verifier = youtube.build_auth_url(redirect_uri)
        request.session["youtube_oauth_state"] = state
        request.session["youtube_code_verifier"] = code_verifier
    else:
        state = get_random_string(32)
        request.session[f"{platform}_oauth_state"] = state
        auth_url = mod.build_auth_url(redirect_uri, state)
    return redirect(auth_url)


@api_login_required
def oauth_callback(request, platform):
    """Provider redirect target: exchange the code, store tokens, bounce to SPA."""
    mod = _OAUTH_MODULES.get(platform)
    if mod is None:
        return JsonResponse({"detail": "Unknown platform."}, status=404)
    if request.GET.get("error"):
        return _frontend_connections("error", platform)
    redirect_uri = request.build_absolute_uri(
        reverse("api:oauth_callback", args=[platform])
    )
    try:
        if platform == "youtube":
            state = request.session.pop("youtube_oauth_state", None)
            verifier = request.session.pop("youtube_code_verifier", None)
            creds = youtube.exchange_code(
                redirect_uri, request.build_absolute_uri(), state, verifier
            )
            account = youtube.save_account(request.user, creds)
        else:
            expected = request.session.pop(f"{platform}_oauth_state", None)
            if not expected or request.GET.get("state") != expected:
                logger.warning("%s OAuth state check failed", platform)
                return _frontend_connections("error", platform)
            code = request.GET.get("code", "")
            if platform == "instagram":
                short_token, ig_user_id = instagram.exchange_code(redirect_uri, code)
                long_token, expires_in = instagram.long_lived_token(short_token)
                account = instagram.save_account(
                    request.user, long_token, ig_user_id, expires_in
                )
            else:  # linkedin
                token, expires_in = linkedin.exchange_code(redirect_uri, code)
                account = linkedin.save_account(request.user, token, expires_in)
    except Exception as exc:
        logger.error("%s OAuth callback failed: %s", platform, exc)
        return _frontend_connections("error", platform)
    _resume_after_connect(account)
    return _frontend_connections("connected", platform)


@api_login_required
def account_disconnect(request, platform):
    if request.method != "POST":
        return JsonResponse({"detail": "Method not allowed."}, status=405)
    SocialAccount.objects.filter(user=request.user, platform=platform).delete()
    return JsonResponse({"ok": True})


@api_login_required
def account_connect(request, platform):
    """Connect / reconnect an account.

    Real OAuth lives in the Django views (core:<platform>_connect) and activates
    once the platform's client id/secret are set. Without creds — the common
    local case — this records a healthy demo connection so the full UX works.
    """
    if request.method != "POST":
        return JsonResponse({"detail": "Method not allowed."}, status=405)
    valid = {c[0] for c in SocialAccount._meta.get_field("platform").choices}
    if platform not in valid:
        return JsonResponse({"detail": "Unknown platform."}, status=400)
    handles = {
        "youtube": "@codewithvennela",
        "instagram": "@vennelas_tech_life",
        "linkedin": "Vennela A.",
    }
    expires = None if platform == "youtube" else timezone.now() + timezone.timedelta(days=60)
    acc, _ = SocialAccount.objects.update_or_create(
        user=request.user,
        platform=platform,
        defaults={
            "status": SocialAccount.Status.CONNECTED,
            "platform_account_id": handles.get(platform, ""),
            "token_expires_at": expires,
            "access_token": "demo",
        },
    )
    return JsonResponse(account_dict(acc))
