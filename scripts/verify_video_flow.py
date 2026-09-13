"""Opt-in live local upload test: R2 -> analysis -> three drafts, never publish.

Run with: .venv/Scripts/python scripts/verify_video_flow.py --source-video 14
Uses an existing local source as the fixture and adds a labelled verification
upload to the same local account. Calls real R2/Gemini services and uses quota.
"""

import argparse
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from urllib.parse import urlsplit

import requests


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-video", type=int, required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:8002")
    args = parser.parse_args()
    if urlsplit(args.base_url).hostname not in {"127.0.0.1", "localhost"}:
        parser.error("This verification helper only authenticates against the local app.")
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    os.environ["DATABASE_URL"] = "sqlite:///" + (root / "db.sqlite3").as_posix()
    os.environ["DJANGO_SETTINGS_MODULE"] = "config.settings"
    import django
    django.setup()
    from django.conf import settings
    from django.test import Client
    from django.contrib.sessions.models import Session
    from core import ai
    from core.models import AIContent, Video

    source = Video.objects.select_related("user").get(pk=args.source_video, source_deleted=False)
    # Development-only session: avoids changing or logging the user's password.
    local_login = Client()
    local_login.force_login(source.user)
    session_key = local_login.cookies[settings.SESSION_COOKIE_NAME].value
    base = args.base_url.rstrip("/")
    started = time.monotonic()

    def report(message):
        print(f"[{int(time.monotonic() - started)}s] {message}", flush=True)

    http = requests.Session()
    http.cookies.set(settings.SESSION_COOKIE_NAME, session_key)

    def post(path, data=None, timeout=120):
        response = http.post(
            base + path, data=data or {}, timeout=(10, timeout), allow_redirects=False,
            headers={"X-CSRFToken": http.cookies.get("csrftoken", ""), "Referer": base + "/upload/"},
        )
        response.raise_for_status()
        return response

    try:
        page = http.get(base + "/upload/", timeout=30, allow_redirects=False)
        assert page.status_code == 200, "Local upload page did not authenticate"
        report(f"Authenticated local account; source video {source.pk}, {source.source_size_bytes} bytes")
        with tempfile.TemporaryDirectory(prefix="cadence-live-upload-") as directory:
            path = Path(directory) / "source.mp4"
            last = time.monotonic()
            downloaded = 0
            with requests.get(source.file_url, stream=True, timeout=(15, 120)) as response:
                response.raise_for_status()
                with path.open("wb") as target:
                    for chunk in response.iter_content(1024 * 1024):
                        target.write(chunk)
                        downloaded += len(chunk)
                        if time.monotonic() - last > 20:
                            report(f"Downloaded {downloaded // 1048576} MiB")
                            last = time.monotonic()
            assert downloaded == source.source_size_bytes, "Downloaded source size mismatch"
            name = "verification-" + (source.original_filename or "video.mp4")
            upload = post("/upload/presign/", {
                "filename": name, "size": downloaded, "content_type": "video/mp4",
            }).json()
            assert upload.get("ok"), upload.get("error")
            report("Uploading fresh copy through the app's signed R2 upload URL")

            class ProgressReader:
                def __init__(self, stream):
                    self.stream = stream
                    self.last = time.monotonic()

                def __len__(self):
                    return downloaded

                def __getattr__(self, name):
                    return getattr(self.stream, name)

                def read(self, size=-1):
                    chunk = self.stream.read(size)
                    if time.monotonic() - self.last > 20:
                        report(f"Sent {self.stream.tell() // 1048576} / {downloaded // 1048576} MiB to R2")
                        self.last = time.monotonic()
                    return chunk

            for attempt in range(1, 4):
                try:
                    with path.open("rb") as body:
                        response = requests.put(upload["upload_url"], data=ProgressReader(body),
                                                headers={"Content-Type": "video/mp4"}, timeout=(15, 600))
                    if 200 <= response.status_code < 300:
                        break
                    if response.status_code not in {408, 429, 500, 502, 503, 504}:
                        raise AssertionError(f"R2 upload returned HTTP {response.status_code}")
                    report(f"Temporary R2 HTTP {response.status_code}, attempt {attempt}/3")
                except requests.RequestException as error:
                    report(f"R2 connection interrupted ({type(error).__name__}), attempt {attempt}/3")
                if attempt == 3:
                    raise AssertionError("R2 upload failed after three attempts")
                time.sleep(2 ** attempt)
            report(f"R2 PUT succeeded (HTTP {response.status_code})")
            response = post("/upload/", {
                "r2_key": upload["key"], "r2_filename": name,
                "r2_duration": source.duration_seconds,
                "title": "", "description": "", "category": "",
                "platforms": ["youtube", "instagram", "linkedin"],
            })
            match = re.fullmatch(r"/video/(\d+)/", response.headers.get("Location", ""))
            assert response.status_code == 302 and match, "Upload finalization did not create a review page"
            video_id = int(match.group(1))
            report(f"Created local review page: {base}/video/{video_id}/")
            deadline = time.monotonic() + 1800
            retry = True
            previous = None
            last = time.monotonic()
            while time.monotonic() < deadline:
                response = post(f"/video/{video_id}/analyze/", {"retry": "1"} if retry else {})
                retry = False
                data = response.json()
                assert data.get("ok"), data.get("error", "Analysis failed")
                if data.get("analysis"):
                    report(f"Real Gemini analysis complete: {len(data['analysis'])} characters")
                    break
                if data.get("status") != previous or time.monotonic() - last > 25:
                    report("Analysis status: " + data.get("status", "unknown"))
                    previous = data.get("status")
                    last = time.monotonic()
                time.sleep(3)
            else:
                raise TimeoutError("Live analysis did not finish within 30 minutes")
            results = []
            for draft in AIContent.objects.filter(video_id=video_id).order_by("platform"):
                report(f"Generating {draft.platform} draft")
                data = post(f"/ai/{draft.pk}/generate/", timeout=180).json()
                assert data.get("ok"), data.get("error")
                draft.refresh_from_db()
                assert draft.ai_model_used == ai.GEMINI_MODEL, f"{draft.platform} used fallback: {data.get('notice')}"
                assert data.get("title") and data.get("description") and data.get("hashtags"), "Empty draft fields"
                errors = ai.validate_metadata(draft.platform, data["title"], data["description"], data["hashtags"])
                assert not errors, errors
                result = {"platform": draft.platform, "title": data["title"],
                          "description": data["description"], "tags": data["hashtags"],
                          "model": draft.ai_model_used}
                results.append(result)
                report(f"PASS {draft.platform}: title {len(data['title'])}, description {len(data['description'])} chars, tags present")
            assert len(results) == 3, "Expected three platform drafts"
            report("LIVE FLOW PASSED: " + json.dumps({"video_id": video_id, "drafts": results}, ensure_ascii=True))
    finally:
        http.close()
        Session.objects.filter(session_key=session_key).delete()


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # Never echo signed URLs or session credentials from requests exceptions.
        if isinstance(error, requests.RequestException):
            print("LIVE FLOW FAILED: " + type(error).__name__, flush=True)
        else:
            print("LIVE FLOW FAILED: " + str(error), flush=True)
        raise SystemExit(1)
