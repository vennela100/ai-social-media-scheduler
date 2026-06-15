"""
Instagram (Meta Graph API) OAuth + publishing — Phase 5.

Publishing is a 3-step async "container" flow:
  1. POST /{ig-user-id}/media   (media_type=REELS, video_url=<cloudinary>, caption)
  2. GET  /{container-id}?fields=status_code  -> poll until FINISHED
  3. POST /{ig-user-id}/media_publish (creation_id=<container-id>)

Auth is Facebook Login: we exchange the code for a short-lived user token, swap
it for a long-lived (~60-day) token, find the Page the user manages and its
linked Instagram Business account, and store the page token + IG user id.

Gated on settings.META_APP_ID / META_APP_SECRET. Heavy work is plain `requests`,
so nothing here needs importing until a real publish/connect happens.
"""

import logging
import time
from urllib.parse import urlencode

import requests
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from .models import Platform, SocialAccount

logger = logging.getLogger("scheduler")

GRAPH_VERSION = "v21.0"
GRAPH = f"https://graph.facebook.com/{GRAPH_VERSION}"
OAUTH_DIALOG = f"https://www.facebook.com/{GRAPH_VERSION}/dialog/oauth"

# Permissions needed to read the Page + IG account and publish content.
SCOPES = [
    "instagram_basic",
    "instagram_content_publish",
    "pages_show_list",
    "pages_read_engagement",
    "business_management",
]

# Container ingestion polling. A Reel usually finishes in seconds; cap the wait
# so a stuck container can't hang the publish job past the cron timeout.
POLL_INTERVAL_SECONDS = 5
MAX_POLLS = 24  # ~2 minutes


def is_configured() -> bool:
    return bool(settings.META_APP_ID and settings.META_APP_SECRET)


def _require_configured() -> None:
    if not is_configured():
        raise ImproperlyConfigured(
            "META_APP_ID / META_APP_SECRET are not set. Create a Facebook App, "
            "get instagram_content_publish approved, and add them to your .env."
        )


# --- OAuth ---

def build_auth_url(redirect_uri: str, state: str) -> str:
    """URL to send the user to Facebook's consent dialog."""
    _require_configured()
    params = {
        "client_id": settings.META_APP_ID,
        "redirect_uri": redirect_uri,
        "state": state,
        "scope": ",".join(SCOPES),
        "response_type": "code",
    }
    return f"{OAUTH_DIALOG}?{urlencode(params)}"


def exchange_code(redirect_uri: str, code: str) -> str:
    """Exchange the OAuth code for a SHORT-lived user access token."""
    _require_configured()
    r = requests.get(
        f"{GRAPH}/oauth/access_token",
        params={
            "client_id": settings.META_APP_ID,
            "client_secret": settings.META_APP_SECRET,
            "redirect_uri": redirect_uri,
            "code": code,
        },
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def long_lived_token(short_token: str) -> tuple[str, int]:
    """Swap a short-lived token for a long-lived one. Returns (token, expires_in)."""
    r = requests.get(
        f"{GRAPH}/oauth/access_token",
        params={
            "grant_type": "fb_exchange_token",
            "client_id": settings.META_APP_ID,
            "client_secret": settings.META_APP_SECRET,
            "fb_exchange_token": short_token,
        },
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    return data["access_token"], data.get("expires_in", 60 * 24 * 3600)


def discover_ig_account(user_token: str) -> dict:
    """
    Find the user's first Page and its linked Instagram Business account.
    Returns {page_id, page_token, ig_user_id, ig_username}. Raises if none.
    """
    pages = requests.get(
        f"{GRAPH}/me/accounts",
        params={"fields": "id,name,access_token", "access_token": user_token},
        timeout=30,
    )
    pages.raise_for_status()
    data = pages.json().get("data", [])
    if not data:
        raise ValueError("No Facebook Page found. Instagram publishing needs a Page.")

    for page in data:
        ig = requests.get(
            f"{GRAPH}/{page['id']}",
            params={"fields": "instagram_business_account{id,username}", "access_token": user_token},
            timeout=30,
        )
        ig.raise_for_status()
        iga = ig.json().get("instagram_business_account")
        if iga:
            return {
                "page_id": page["id"],
                "page_token": page["access_token"],
                "ig_user_id": iga["id"],
                "ig_username": iga.get("username", ""),
            }
    raise ValueError("No Instagram Business account linked to your Page(s).")


def save_account(user, user_token: str, expires_in: int) -> SocialAccount:
    """Upsert the IG SocialAccount: store the Page token + IG user id."""
    import datetime as dt

    from django.utils import timezone

    info = discover_ig_account(user_token)
    expiry = timezone.now() + dt.timedelta(seconds=expires_in)
    account, _ = SocialAccount.objects.update_or_create(
        user=user,
        platform=Platform.INSTAGRAM,
        defaults={
            # Page token is what publishes on behalf of the IG account.
            "access_token": info["page_token"],
            "refresh_token": user_token,  # long-lived user token, for re-deriving
            "platform_account_id": info["ig_user_id"],
            "token_expires_at": expiry,
        },
    )
    logger.info("Saved Instagram account @%s for user %s", info["ig_username"], user)
    return account


# --- Publishing ---

def check_publishing_limit(account: SocialAccount) -> int:
    """Return remaining posts in the rolling 24h window (IG caps at ~100/24h)."""
    r = requests.get(
        f"{GRAPH}/{account.platform_account_id}/content_publishing_limit",
        params={"fields": "quota_usage,config", "access_token": account.access_token},
        timeout=30,
    )
    r.raise_for_status()
    data = r.json().get("data", [{}])[0]
    quota = data.get("config", {}).get("quota_total", 100)
    used = data.get("quota_usage", 0)
    return max(0, quota - used)


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
    create.raise_for_status()
    creation_id = create.json()["id"]
    logger.info("Instagram container created: %s", creation_id)

    # 2. Poll until Instagram has ingested the video.
    for _ in range(MAX_POLLS):
        status = requests.get(
            f"{GRAPH}/{creation_id}",
            params={"fields": "status_code", "access_token": token},
            timeout=30,
        )
        status.raise_for_status()
        code = status.json().get("status_code")
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
    pub.raise_for_status()
    media_id = pub.json()["id"]
    logger.info("Published to Instagram: %s", media_id)
    return media_id
