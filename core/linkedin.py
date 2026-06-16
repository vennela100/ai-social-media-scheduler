"""
LinkedIn OAuth + video publishing — Phase 6.

Publishing a member video is a 3-step flow on the classic v2 surface (the one a
self-serve "Share on LinkedIn" app can actually get approved for — the newer
/rest/posts Videos API needs Community Management access):

  1. POST /v2/assets?action=registerUpload   -> returns an uploadUrl + asset URN
  2. Upload the raw video bytes to that uploadUrl (LinkedIn stores them; unlike
     Instagram, it will NOT fetch a remote URL for you).
  3. POST /v2/ugcPosts  with shareMediaCategory=VIDEO referencing the asset URN.

Auth is OpenID Connect: exchange the code for a ~60-day access token, then call
/v2/userinfo to learn the member's person id (the `sub` claim). LinkedIn member
tokens are long-lived but NOT refreshable for most apps, so we store the access
token + expiry and ask the user to reconnect when it lapses.

Gated on settings.LINKEDIN_CLIENT_ID / LINKEDIN_CLIENT_SECRET. All work is plain
`requests`, so nothing here is imported until a real connect/publish happens.
"""

import datetime as dt
import logging
from urllib.parse import urlencode

import requests
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from .models import Platform, SocialAccount

logger = logging.getLogger("scheduler")

AUTH_URL = "https://www.linkedin.com/oauth/v2/authorization"
TOKEN_URL = "https://www.linkedin.com/oauth/v2/accessToken"
USERINFO_URL = "https://api.linkedin.com/v2/userinfo"
ASSETS_URL = "https://api.linkedin.com/v2/assets"
UGC_POSTS_URL = "https://api.linkedin.com/v2/ugcPosts"

# openid+profile let us read the member's person id via /userinfo;
# w_member_social is what actually authorizes posting on their behalf.
SCOPES = ["openid", "profile", "w_member_social"]

# Where a published post is visible. PUBLIC = anyone on LinkedIn (right for a
# content-publishing tool); CONNECTIONS = 1st-degree only. See _post_visibility().
DEFAULT_VISIBILITY = "PUBLIC"

# LinkedIn truncates commentary past this; we guard against a hard API reject.
MAX_COMMENTARY = 3000


def is_configured() -> bool:
    return bool(settings.LINKEDIN_CLIENT_ID and settings.LINKEDIN_CLIENT_SECRET)


def _require_configured() -> None:
    if not is_configured():
        raise ImproperlyConfigured(
            "LINKEDIN_CLIENT_ID / LINKEDIN_CLIENT_SECRET are not set. Create an app "
            "at linkedin.com/developers, add the 'Share on LinkedIn' + 'Sign In with "
            "LinkedIn using OpenID Connect' products, and put the keys in your .env."
        )


# --- OAuth ---

def build_auth_url(redirect_uri: str, state: str) -> str:
    """URL to send the user to LinkedIn's consent dialog."""
    _require_configured()
    params = {
        "response_type": "code",
        "client_id": settings.LINKEDIN_CLIENT_ID,
        "redirect_uri": redirect_uri,
        "state": state,
        "scope": " ".join(SCOPES),
    }
    return f"{AUTH_URL}?{urlencode(params)}"


def exchange_code(redirect_uri: str, code: str) -> tuple[str, int]:
    """Exchange the OAuth code for an access token. Returns (token, expires_in)."""
    _require_configured()
    r = requests.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": settings.LINKEDIN_CLIENT_ID,
            "client_secret": settings.LINKEDIN_CLIENT_SECRET,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    return data["access_token"], data.get("expires_in", 60 * 24 * 3600)


def fetch_member_id(access_token: str) -> str:
    """Return the member's person id (the OpenID `sub` claim)."""
    r = requests.get(
        USERINFO_URL,
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["sub"]


def save_account(user, access_token: str, expires_in: int) -> SocialAccount:
    """Upsert the LinkedIn SocialAccount with the member token + person id."""
    from django.utils import timezone

    member_id = fetch_member_id(access_token)
    expiry = timezone.now() + dt.timedelta(seconds=expires_in)
    account, _ = SocialAccount.objects.update_or_create(
        user=user,
        platform=Platform.LINKEDIN,
        defaults={
            "access_token": access_token,
            "refresh_token": "",  # member tokens generally aren't refreshable
            "platform_account_id": member_id,
            "token_expires_at": expiry,
        },
    )
    logger.info("Saved LinkedIn account %s for user %s", member_id, user)
    return account


# --- Publishing ---

def _author_urn(account: SocialAccount) -> str:
    return f"urn:li:person:{account.platform_account_id}"


def _headers(token: str) -> dict:
    # X-Restli-Protocol-Version is required by the v2 UGC/assets surface.
    return {
        "Authorization": f"Bearer {token}",
        "X-Restli-Protocol-Version": "2.0.0",
        "Content-Type": "application/json",
    }


def _register_upload(account: SocialAccount) -> tuple[str, str]:
    """Register a video upload. Returns (upload_url, asset_urn)."""
    body = {
        "registerUploadRequest": {
            "recipes": ["urn:li:digitalmediaRecipe:feedshare-video"],
            "owner": _author_urn(account),
            "serviceRelationships": [
                {"relationshipType": "OWNER", "identifier": "urn:li:userGeneratedContent"}
            ],
        }
    }
    r = requests.post(
        f"{ASSETS_URL}?action=registerUpload",
        json=body,
        headers=_headers(account.access_token),
        timeout=30,
    )
    r.raise_for_status()
    value = r.json()["value"]
    upload_url = value["uploadMechanism"][
        "com.linkedin.digitalmedia.uploading.MediaUploadHttpRequest"
    ]["uploadUrl"]
    return upload_url, value["asset"]


def _upload_bytes(upload_url: str, token: str, video_url: str) -> None:
    """Stream the video down from Cloudinary and up to LinkedIn's upload URL."""
    src = requests.get(video_url, timeout=120)
    src.raise_for_status()
    up = requests.post(
        upload_url,
        data=src.content,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/octet-stream",
        },
        timeout=300,
    )
    up.raise_for_status()


def _create_share(account: SocialAccount, asset_urn: str, commentary: str) -> str:
    """Create the UGC video post. Returns the post URN."""
    body = {
        "author": _author_urn(account),
        "lifecycleState": "PUBLISHED",
        "specificContent": {
            "com.linkedin.ugc.ShareContent": {
                "shareCommentary": {"text": commentary[:MAX_COMMENTARY]},
                "shareMediaCategory": "VIDEO",
                "media": [{"status": "READY", "media": asset_urn}],
            }
        },
        "visibility": {
            "com.linkedin.ugc.MemberNetworkVisibility": _post_visibility(account)
        },
    }
    r = requests.post(
        UGC_POSTS_URL, json=body, headers=_headers(account.access_token), timeout=60
    )
    r.raise_for_status()
    # The created post id comes back in a header, not the body.
    return r.headers.get("X-RestLi-Id") or r.json().get("id", "")


def publish(account: SocialAccount, *, video_url: str, caption: str) -> str:
    """Publish a video to the member's feed. Returns the LinkedIn post URN."""
    upload_url, asset_urn = _register_upload(account)
    logger.info("LinkedIn upload registered: %s", asset_urn)
    _upload_bytes(upload_url, account.access_token, video_url)
    post_urn = _create_share(account, asset_urn, caption)
    logger.info("Published to LinkedIn: %s", post_urn)
    return post_urn


def _post_visibility(account: SocialAccount) -> str:
    """
    Decide who can see a published LinkedIn post: "PUBLIC" or "CONNECTIONS".

    This is a genuine product decision, so it's left for you to shape (learning
    mode). For a scheduling/reach tool the obvious default is PUBLIC, but you may
    want CONNECTIONS-only for certain accounts, or to read it off the post/user.

    Returning DEFAULT_VISIBILITY keeps publishing working today; refine as needed.
    """
    return DEFAULT_VISIBILITY
