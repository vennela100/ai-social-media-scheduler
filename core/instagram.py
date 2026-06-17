"""
Instagram (Instagram API with Instagram Login) OAuth + publishing — Phase 5.

This uses Meta's NEWER "Instagram API with Instagram login" flow (not the older
Facebook-Page-based Graph API). The user logs in with Instagram directly — no
Facebook Page required — and we talk to graph.instagram.com.

Auth:
  1. Send the user to instagram.com/oauth/authorize (Instagram app id + the
     instagram_business_* scopes).
  2. Exchange the code at api.instagram.com for a SHORT-lived token + user_id.
  3. Swap that for a LONG-lived (~60-day) token at graph.instagram.com.

Publishing is the same 3-step async "container" flow as before, but against
graph.instagram.com/{ig-user-id}:
  1. POST /{ig-user-id}/media   (media_type=REELS, video_url=<cloudinary>, caption)
  2. GET  /{container-id}?fields=status_code  -> poll until FINISHED
  3. POST /{ig-user-id}/media_publish (creation_id=<container-id>)

Gated on settings.INSTAGRAM_APP_ID / INSTAGRAM_APP_SECRET (the *Instagram* app
credentials from the Instagram product, NOT the Facebook app id/secret).
"""

import datetime as dt
import logging
import time
from urllib.parse import urlencode

import requests
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.utils import timezone

from .models import Platform, SocialAccount

logger = logging.getLogger("scheduler")

GRAPH_VERSION = "v21.0"
GRAPH = f"https://graph.instagram.com/{GRAPH_VERSION}"
# Instagram's own OAuth endpoints (Instagram Login, not Facebook Login).
AUTHORIZE_URL = "https://www.instagram.com/oauth/authorize"
TOKEN_URL = "https://api.instagram.com/oauth/access_token"
LONG_LIVED_URL = "https://graph.instagram.com/access_token"

# The only permissions we need: read the account + publish content.
SCOPES = [
    "instagram_business_basic",
    "instagram_business_content_publish",
]

# Container ingestion polling. A Reel usually finishes in seconds; cap the wait
# so a stuck container can't hang the publish job past the cron timeout.
POLL_INTERVAL_SECONDS = 5
MAX_POLLS = 24  # ~2 minutes


def _check(resp, context: str) -> dict:
    """Raise with Instagram's own error message (not a bare HTTP 400) on failure.

    The Graph API returns useful JSON like {"error":{"message":"API access
    blocked.",...}}; raise_for_status() throws that away, leaving an opaque
    "400 Bad Request" in the post's last_error. Surface the real message so a
    failure is diagnosable from the dashboard.
    """
    if resp.ok:
        return resp.json() if resp.content else {}
    try:
        err = resp.json().get("error", {})
        detail = err.get("message") or resp.text
    except ValueError:
        detail = resp.text
    raise RuntimeError(f"Instagram {context} failed ({resp.status_code}): {detail}")


def is_configured() -> bool:
    return bool(settings.INSTAGRAM_APP_ID and settings.INSTAGRAM_APP_SECRET)


def _require_configured() -> None:
    if not is_configured():
        raise ImproperlyConfigured(
            "INSTAGRAM_APP_ID / INSTAGRAM_APP_SECRET are not set. Get them from your "
            "Meta app's Instagram product (API setup with Instagram login) and add "
            "them to your .env."
        )


# --- OAuth ---

def build_auth_url(redirect_uri: str, state: str) -> str:
    """URL to send the user to Instagram's consent dialog."""
    _require_configured()
    params = {
        "client_id": settings.INSTAGRAM_APP_ID,
        "redirect_uri": redirect_uri,
        "state": state,
        "scope": ",".join(SCOPES),
        "response_type": "code",
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


def exchange_code(redirect_uri: str, code: str) -> tuple[str, str]:
    """Exchange the OAuth code for a SHORT-lived token. Returns (token, user_id)."""
    _require_configured()
    # Instagram appends "#_" to the returned code; strip it if present.
    code = code.split("#", 1)[0]
    r = requests.post(
        TOKEN_URL,
        data={
            "client_id": settings.INSTAGRAM_APP_ID,
            "client_secret": settings.INSTAGRAM_APP_SECRET,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
            "code": code,
        },
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    # Some API versions wrap the result in a "data" list; handle both shapes.
    if "access_token" not in data and isinstance(data.get("data"), list):
        data = data["data"][0]
    return data["access_token"], str(data["user_id"])


def long_lived_token(short_token: str) -> tuple[str, int]:
    """Swap a short-lived token for a long-lived (~60-day) one. Returns (token, expires_in)."""
    r = requests.get(
        LONG_LIVED_URL,
        params={
            "grant_type": "ig_exchange_token",
            "client_secret": settings.INSTAGRAM_APP_SECRET,
            "access_token": short_token,
        },
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    return data["access_token"], data.get("expires_in", 60 * 24 * 3600)


def save_account(user, long_token: str, ig_user_id: str, expires_in: int) -> SocialAccount:
    """Upsert the IG SocialAccount: store the long-lived token + IG user id."""
    expiry = timezone.now() + dt.timedelta(seconds=expires_in)
    account, _ = SocialAccount.objects.update_or_create(
        user=user,
        platform=Platform.INSTAGRAM,
        defaults={
            "access_token": long_token,
            "refresh_token": "",  # IG long-lived tokens are refreshed, not paired
            "platform_account_id": ig_user_id,
            "token_expires_at": expiry,
        },
    )
    logger.info("Saved Instagram account %s for user %s", ig_user_id, user)
    return account


# --- Publishing ---

def publish(account: SocialAccount, *, video_url: str, caption: str) -> str:
    """Publish a Reel via the container flow. Returns the IG media id."""
    ig_user_id = account.platform_account_id
    token = account.access_token

    # 1. Create the media container.
    create = requests.post(
        f"{GRAPH}/{ig_user_id}/media",
        data={"media_type": "REELS", "video_url": video_url, "caption": caption, "access_token": token},
        timeout=60,
    )
    creation_id = _check(create, "container create")["id"]
    logger.info("Instagram container created: %s", creation_id)

    # 2. Poll until Instagram has ingested the video.
    for _ in range(MAX_POLLS):
        status = requests.get(
            f"{GRAPH}/{creation_id}",
            params={"fields": "status_code", "access_token": token},
            timeout=30,
        )
        code = _check(status, "container status").get("status_code")
        if code == "FINISHED":
            break
        if code == "ERROR":
            raise RuntimeError("Instagram failed to process the video container.")
        time.sleep(POLL_INTERVAL_SECONDS)
    else:
        raise RuntimeError("Instagram container not ready within the time budget.")

    # 3. Publish the container.
    pub = requests.post(
        f"{GRAPH}/{ig_user_id}/media_publish",
        data={"creation_id": creation_id, "access_token": token},
        timeout=60,
    )
    media_id = _check(pub, "media publish")["id"]
    logger.info("Published to Instagram: %s", media_id)
    return media_id
