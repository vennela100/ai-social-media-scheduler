"""
YouTube OAuth + upload service (Phase 3).

Responsibilities:
  - Build the Google consent URL and exchange the callback code for tokens.
  - Persist tokens (encrypted) on a SocialAccount, refreshing when expired.
  - Upload a video to YouTube via a resumable videos.insert.

Gated on settings.GOOGLE_OAUTH_CLIENT_ID/SECRET. The heavy Google imports are
done lazily inside functions so the app boots without credentials.
"""

import datetime as dt
import io
import logging

import requests
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from .models import Platform, SocialAccount

logger = logging.getLogger("scheduler")

# youtube.upload publishes a video; youtube.readonly lets us read back a video's
# statistics (views/likes/comments) for analytics. Adding readonly means existing
# connections must reconnect once to re-consent to the wider scope.
SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
]
TOKEN_URI = "https://oauth2.googleapis.com/token"

# Default visibility of uploaded videos. Decision point — 'private' is the safe
# default while testing; switch to 'unlisted' or 'public' when you trust the flow.
DEFAULT_PRIVACY = "private"
# YouTube category 22 = "People & Blogs", a sane generic default.
DEFAULT_CATEGORY_ID = "22"


def is_configured() -> bool:
    return bool(settings.GOOGLE_OAUTH_CLIENT_ID and settings.GOOGLE_OAUTH_CLIENT_SECRET)


def _require_configured() -> None:
    if not is_configured():
        raise ImproperlyConfigured(
            "GOOGLE_OAUTH_CLIENT_ID / GOOGLE_OAUTH_CLIENT_SECRET are not set. "
            "Create an OAuth client (type 'Web application') in Google Cloud and "
            "add them to your .env."
        )


def _client_config(redirect_uri: str) -> dict:
    return {
        "web": {
            "client_id": settings.GOOGLE_OAUTH_CLIENT_ID,
            "client_secret": settings.GOOGLE_OAUTH_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": TOKEN_URI,
            "redirect_uris": [redirect_uri],
        }
    }


def _allow_insecure_local() -> None:
    """Permit http:// redirect URIs during local DEBUG dev only."""
    if settings.DEBUG:
        import os

        os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")
        os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")


# --- OAuth flow ---

def build_auth_url(redirect_uri: str):
    """
    Return (authorization_url, state, code_verifier) to redirect the user to
    Google. The code_verifier (PKCE) MUST be stored and passed back to
    exchange_code(), or the token exchange fails with "Missing code verifier".
    """
    _require_configured()
    _allow_insecure_local()
    from google_auth_oauthlib.flow import Flow

    flow = Flow.from_client_config(_client_config(redirect_uri), scopes=SCOPES)
    flow.redirect_uri = redirect_uri
    auth_url, state = flow.authorization_url(
        access_type="offline",          # get a refresh token for unattended publishing
        include_granted_scopes="true",
        prompt="consent",               # force refresh-token issuance every time
    )
    return auth_url, state, flow.code_verifier


def exchange_code(redirect_uri: str, authorization_response_url: str,
                  state: str | None, code_verifier: str | None = None):
    """Exchange the callback URL for credentials (token + refresh_token)."""
    _require_configured()
    _allow_insecure_local()
    from google_auth_oauthlib.flow import Flow

    flow = Flow.from_client_config(_client_config(redirect_uri), scopes=SCOPES, state=state)
    flow.redirect_uri = redirect_uri
    # Replay the PKCE verifier generated during build_auth_url().
    if code_verifier:
        flow.code_verifier = code_verifier
    flow.fetch_token(authorization_response=authorization_response_url)
    return flow.credentials


def save_account(user, creds) -> SocialAccount:
    """Upsert the user's YouTube SocialAccount from fresh credentials."""
    expiry = None
    if creds.expiry:
        # google-auth gives a naive UTC datetime; make it tz-aware.
        expiry = creds.expiry.replace(tzinfo=dt.timezone.utc)

    account, _ = SocialAccount.objects.update_or_create(
        user=user,
        platform=Platform.YOUTUBE,
        defaults={
            "access_token": creds.token or "",
            # refresh_token is only present on first consent; keep old one if absent.
            "refresh_token": creds.refresh_token or _existing_refresh(user),
            "token_expires_at": expiry,
        },
    )
    logger.info("Saved YouTube account for user %s", user)
    return account


def _existing_refresh(user) -> str:
    acct = SocialAccount.objects.filter(user=user, platform=Platform.YOUTUBE).first()
    return acct.refresh_token if acct else ""


# --- Credentials / refresh ---

def get_credentials(account: SocialAccount):
    """
    Return valid google credentials for this account, refreshing if expired.
    Persists the refreshed access token. Raises if refresh fails (caller should
    then mark the post 'needs_reconnect').
    """
    _require_configured()
    from google.auth.transport.requests import Request as GoogleRequest
    from google.oauth2.credentials import Credentials

    creds = Credentials(
        token=account.access_token or None,
        refresh_token=account.refresh_token or None,
        token_uri=TOKEN_URI,
        client_id=settings.GOOGLE_OAUTH_CLIENT_ID,
        client_secret=settings.GOOGLE_OAUTH_CLIENT_SECRET,
        scopes=SCOPES,
    )

    if not creds.valid:
        if not creds.refresh_token:
            raise ImproperlyConfigured("No refresh token; account must reconnect.")
        creds.refresh(GoogleRequest())
        account.access_token = creds.token
        if creds.expiry:
            account.token_expires_at = creds.expiry.replace(tzinfo=dt.timezone.utc)
        account.save(update_fields=["access_token", "token_expires_at"])
        logger.info("Refreshed YouTube token for user %s", account.user)

    return creds


# --- Upload ---

def publish(account: SocialAccount, *, video_url: str, title: str, description: str,
            tags=None, privacy: str = DEFAULT_PRIVACY) -> str:
    """
    Upload the video at video_url to YouTube. Returns the new video id.

    Streams the file from Cloudinary into a resumable upload so we never hold
    the whole video on disk.
    """
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaIoBaseUpload

    creds = get_credentials(account)
    youtube = build("youtube", "v3", credentials=creds, cache_discovery=False)

    resp = requests.get(video_url, timeout=120)
    resp.raise_for_status()
    media = MediaIoBaseUpload(
        io.BytesIO(resp.content), mimetype="video/*", chunksize=-1, resumable=True
    )

    body = {
        "snippet": {
            "title": title[:100],          # YouTube hard limit
            "description": description[:5000],
            "tags": tags or [],
            "categoryId": DEFAULT_CATEGORY_ID,
        },
        "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False},
    }

    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
    response = None
    while response is None:
        _, response = request.next_chunk()

    video_id = response["id"]
    logger.info("Published to YouTube: %s", video_id)
    return video_id


# --- Analytics ---

def fetch_stats(account: SocialAccount, video_id: str) -> dict | None:
    """Return {"views","likes","comments"} for a published video, or None.

    videos.list(part=statistics) is a public read, so the existing upload-scoped
    token is enough. likeCount/commentCount can be hidden by the creator — those
    come back absent, which we surface as None (not 0). Never raises: any failure
    degrades to None so the dashboard simply keeps the last known numbers.
    """
    if not video_id:
        return None
    try:
        from googleapiclient.discovery import build

        creds = get_credentials(account)
        youtube = build("youtube", "v3", credentials=creds, cache_discovery=False)
        items = youtube.videos().list(part="statistics", id=video_id).execute().get("items", [])
        if not items:
            logger.warning("YouTube stats: video %s not found", video_id)
            return None
        s = items[0].get("statistics", {})
        as_int = lambda key: int(s[key]) if key in s else None
        return {
            "views": as_int("viewCount"),
            "likes": as_int("likeCount"),
            "comments": as_int("commentCount"),
        }
    except Exception as exc:  # API / refresh / parse — keep analytics non-fatal
        logger.warning("YouTube stats fetch failed for %s: %s", video_id, exc)
        return None
