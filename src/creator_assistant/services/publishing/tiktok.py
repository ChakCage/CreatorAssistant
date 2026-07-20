from __future__ import annotations

import json
import math
import mimetypes
import time
from pathlib import Path
from typing import Any, Callable

from creator_assistant.infrastructure.credential_store import WindowsCredentialStore
from creator_assistant.services.publishing.http import PublishingHttpClient
from creator_assistant.services.publishing.oauth import OAuthService


class TikTokPublishingConnector:
    platform = "tiktok"
    api = "https://open.tiktokapis.com/v2/post/publish"

    def __init__(self, account_id: str, credentials: WindowsCredentialStore, http: PublishingHttpClient | None = None) -> None:
        self.account_id = account_id
        self.credentials = credentials
        self.http = http or PublishingHttpClient()
        self.oauth = OAuthService(credentials, self.http)

    def _token_data(self) -> dict[str, Any]:
        value = self.credentials.read_json(self.account_id)
        if float(value.get("expires_at", 0) or 0) <= time.time() + 120:
            value = self.oauth.refresh_tiktok(self.account_id)
        if not value.get("access_token"):
            raise ValueError("TikTok account is not authorized")
        return value

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token_data()['access_token']}", "Content-Type": "application/json; charset=UTF-8"}

    def validate_account(self) -> dict[str, Any]:
        response = self.http.request("POST", f"{self.api}/creator_info/query/", headers=self._headers(), body=b"{}").json()
        error = response.get("error") or {}
        data = response.get("data") or {}
        return {
            "valid": error.get("code") in {None, "ok"}, "display_name": str(data.get("creator_nickname") or ""),
            "privacy_options": list(data.get("privacy_level_options") or []),
            "max_duration": int(data.get("max_video_post_duration_sec") or 0),
            "comment_disabled": bool(data.get("comment_disabled")),
            "duet_disabled": bool(data.get("duet_disabled")), "stitch_disabled": bool(data.get("stitch_disabled")),
        }

    def upload(
        self, artifact_path: str, metadata: dict[str, Any], *,
        progress: Callable[[int, int], None] | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> str:
        path = Path(artifact_path)
        if not path.is_file(): raise FileNotFoundError(path)
        total = path.stat().st_size
        chunk_size = total if total < 5_000_000 else min(max(5_000_000, int(metadata.get("chunk_size", 10_000_000))), 64_000_000)
        total_chunks = int(math.ceil(total / chunk_size))
        mode = str(metadata.get("post_mode") or "draft")
        source = {"source": "FILE_UPLOAD", "video_size": total, "chunk_size": chunk_size, "total_chunk_count": total_chunks}
        if mode == "direct":
            creator = self.validate_account()
            allowed = creator.get("privacy_options") or []
            requested_privacy = str(metadata.get("privacy_level") or "SELF_ONLY")
            if requested_privacy not in allowed:
                raise ValueError(f"TikTok privacy option is not available for this account: {requested_privacy}")
            payload = {"post_info": {
                "title": str(metadata.get("title") or path.stem)[:2200],
                "privacy_level": requested_privacy,
                "disable_duet": bool(metadata.get("disable_duet", False)),
                "disable_comment": bool(metadata.get("disable_comment", False)),
                "disable_stitch": bool(metadata.get("disable_stitch", False)),
                "brand_content_toggle": bool(metadata.get("brand_content", False)),
                "brand_organic_toggle": bool(metadata.get("brand_organic", False)),
            }, "source_info": source}
            endpoint = f"{self.api}/video/init/"
        else:
            payload = {"source_info": source}; endpoint = f"{self.api}/inbox/video/init/"
        init = self.http.request("POST", endpoint, headers=self._headers(), body=json.dumps(payload).encode()).json()
        error = init.get("error") or {}
        if error.get("code") not in {None, "ok"}:
            raise RuntimeError(str(error.get("message") or error.get("code")))
        data = init.get("data") or {}; publish_id = str(data.get("publish_id") or ""); upload_url = str(data.get("upload_url") or "")
        if not publish_id or not upload_url: raise RuntimeError("TikTok did not return publish_id/upload_url")
        mime = mimetypes.guess_type(path.name)[0] or "video/mp4"
        offset = 0
        with path.open("rb") as stream:
            while offset < total:
                if cancelled and cancelled(): raise InterruptedError("TikTok upload cancelled")
                chunk = stream.read(min(chunk_size, total - offset)); end = offset + len(chunk) - 1
                self.http.request("PUT", upload_url, headers={"Content-Type": mime, "Content-Length": str(len(chunk)), "Content-Range": f"bytes {offset}-{end}/{total}"}, body=chunk, timeout=180)
                offset = end + 1
                if progress: progress(offset, total)
        return publish_id

    def get_status(self, publish_id: str) -> dict[str, Any]:
        response = self.http.request("POST", f"{self.api}/status/fetch/", headers=self._headers(), body=json.dumps({"publish_id": publish_id}).encode()).json()
        return response.get("data") or {}

    def schedule(self, _upload_id: str, _scheduled_at: str) -> str:
        raise NotImplementedError("TikTok publishAt is not available; use BackgroundPublishingAgent at the target time")

    def refresh_token(self) -> bool:
        return bool(self.oauth.refresh_tiktok(self.account_id).get("access_token"))

    def retry(self, _operation_id: str) -> bool: return True
    def cancel(self, _operation_id: str) -> bool: return True
