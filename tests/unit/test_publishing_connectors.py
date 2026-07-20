from __future__ import annotations

import json
import threading
import time
from urllib.request import urlopen

from creator_assistant.infrastructure.credential_store import WindowsCredentialStore
from creator_assistant.services.publishing.http import HttpResponse, PublishingHttpError
from creator_assistant.services.publishing.oauth import OAuthCallback
from creator_assistant.services.publishing.tiktok import TikTokPublishingConnector
from creator_assistant.services.publishing.youtube import YouTubePublishingConnector


class YouTubeHttp:
    def __init__(self): self.calls = []; self.offset = 0; self.metadata = {}

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if method == "POST" and "uploadType=resumable" in url:
            self.metadata = json.loads(kwargs["body"]); return HttpResponse(200, {"Location": "https://upload/session"}, b"")
        if method == "PUT" and url == "https://upload/session" and kwargs.get("headers", {}).get("Content-Range", "").startswith("bytes */"):
            raise PublishingHttpError(308, "resume", headers={"Range": f"bytes=0-{self.offset - 1}"} if self.offset else {})
        if method == "PUT" and url == "https://upload/session":
            header = kwargs["headers"]["Content-Range"]; end = int(header.split("-", 1)[1].split("/", 1)[0]); self.offset = end + 1
            total = int(header.rsplit("/", 1)[1])
            if self.offset < total: raise PublishingHttpError(308, "continue", headers={"Range": f"bytes=0-{end}"})
            return HttpResponse(201, {}, b'{"id":"video-123"}')
        if method == "GET" and "/channels?" in url:
            return HttpResponse(200, {}, b'{"items":[{"id":"UC1","snippet":{"title":"Channel"}}]}')
        raise AssertionError((method, url))


def test_youtube_resumable_upload_private_metadata_and_progress(tmp_path):
    media = tmp_path / "short.mp4"; media.write_bytes(b"x" * (9 * 1024 * 1024))
    credentials = WindowsCredentialStore(allow_test_memory=True); credentials.write_json("youtube", {"access_token": "token", "expires_at": time.time() + 3600})
    http = YouTubeHttp(); progress = []
    publish_at = "2026-07-20T13:00:00+03:00"
    video_id = YouTubePublishingConnector("youtube", credentials, http).upload(str(media), {"title": "Тест", "privacyStatus": "public", "publishAt": publish_at, "madeForKids": False}, progress=lambda done, total: progress.append((done, total)))
    assert video_id == "video-123" and progress[-1][0] == media.stat().st_size
    assert http.metadata["status"] == {"privacyStatus": "private", "publishAt": publish_at, "selfDeclaredMadeForKids": False}
    assert len([call for call in http.calls if call[0] == "PUT" and call[2].get("body")]) == 2


class TikTokHttp:
    def __init__(self): self.calls = []; self.payload = {}
    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if url.endswith("creator_info/query/"):
            return HttpResponse(200, {}, b'{"data":{"creator_nickname":"Creator","privacy_level_options":["SELF_ONLY"]},"error":{"code":"ok"}}')
        if url.endswith("video/init/"):
            self.payload = json.loads(kwargs["body"]); return HttpResponse(200, {}, b'{"data":{"publish_id":"pub-1","upload_url":"https://upload/tiktok"},"error":{"code":"ok"}}')
        if url == "https://upload/tiktok": return HttpResponse(201, {}, b"")
        raise AssertionError((method, url))


def test_tiktok_direct_private_upload_queries_real_contract(tmp_path):
    media = tmp_path / "short.mp4"; media.write_bytes(b"video")
    credentials = WindowsCredentialStore(allow_test_memory=True); credentials.write_json("tiktok", {"access_token": "token", "expires_at": time.time() + 3600})
    http = TikTokHttp(); connector = TikTokPublishingConnector("tiktok", credentials, http)
    assert connector.validate_account()["privacy_options"] == ["SELF_ONLY"]
    assert connector.upload(str(media), {"post_mode": "direct", "privacy_level": "SELF_ONLY", "title": "Тест"}) == "pub-1"
    assert http.payload["post_info"]["privacy_level"] == "SELF_ONLY"
    assert http.payload["source_info"]["source"] == "FILE_UPLOAD"


def test_oauth_loopback_callback_validates_code_and_state():
    callback = OAuthCallback()
    def send():
        time.sleep(0.05)
        urlopen(callback.redirect_uri + "?code=abc&state=expected", timeout=2).read()
    thread = threading.Thread(target=send); thread.start()
    result = callback.wait(2); thread.join()
    assert result == {"code": "abc", "state": "expected"}
