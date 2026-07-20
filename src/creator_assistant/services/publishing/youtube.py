from __future__ import annotations

import json
import math
import mimetypes
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode

from creator_assistant.infrastructure.credential_store import WindowsCredentialStore
from creator_assistant.services.publishing.http import PublishingHttpClient, PublishingHttpError
from creator_assistant.services.publishing.oauth import OAuthService


class YouTubePublishingConnector:
    platform = "youtube"
    api = "https://www.googleapis.com/youtube/v3"
    upload_api = "https://www.googleapis.com/upload/youtube/v3/videos"

    def __init__(self, account_id: str, credentials: WindowsCredentialStore, http: PublishingHttpClient | None = None) -> None:
        self.account_id = account_id
        self.credentials = credentials
        self.http = http or PublishingHttpClient()
        self.oauth = OAuthService(credentials, self.http)

    def _token(self) -> str:
        value = self.credentials.read_json(self.account_id)
        if float(value.get("expires_at", 0) or 0) <= time.time() + 120:
            value = self.oauth.refresh_google(self.account_id)
        token = str(value.get("access_token") or "")
        if not token:
            raise ValueError("YouTube account is not authorized")
        return token

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token()}"}

    def validate_account(self) -> dict[str, Any]:
        query = urlencode({"part": "id,snippet", "mine": "true"})
        response = self.http.request("GET", f"{self.api}/channels?{query}", headers=self._headers()).json()
        item = next(iter(response.get("items") or []), {})
        return {
            "valid": bool(item.get("id")), "channel_id": str(item.get("id") or ""),
            "display_name": str((item.get("snippet") or {}).get("title") or ""),
        }

    def upload(
        self, artifact_path: str, metadata: dict[str, Any], *,
        progress: Callable[[int, int], None] | None = None,
        cancelled: Callable[[], bool] | None = None,
        resume_key: str = "",
    ) -> str:
        path = Path(artifact_path)
        if not path.is_file():
            raise FileNotFoundError(path)
        total = path.stat().st_size
        mime = mimetypes.guess_type(path.name)[0] or "video/mp4"
        upload_key = resume_key or f"youtube-upload-{self.account_id}-{path.stat().st_size}-{path.stat().st_mtime_ns}"
        session = self.credentials.read_json(upload_key, "upload-session")
        upload_url = str(session.get("url") or "")
        if not upload_url:
            status: dict[str, Any] = {"privacyStatus": str(metadata.get("privacyStatus") or "private")}
            if metadata.get("publishAt"):
                status["privacyStatus"] = "private"; status["publishAt"] = metadata["publishAt"]
            if "madeForKids" in metadata:
                status["selfDeclaredMadeForKids"] = bool(metadata["madeForKids"])
            body = json.dumps({
                "snippet": {"title": str(metadata.get("title") or path.stem)[:100], "description": str(metadata.get("description") or "")[:5000], "tags": list(metadata.get("tags") or []), "categoryId": str(metadata.get("category") or "22")},
                "status": status,
            }, ensure_ascii=False).encode("utf-8")
            headers = {**self._headers(), "Content-Type": "application/json; charset=UTF-8", "X-Upload-Content-Length": str(total), "X-Upload-Content-Type": mime}
            init_query = {"uploadType": "resumable", "part": "snippet,status"}
            if "notifySubscribers" in metadata:
                init_query["notifySubscribers"] = "true" if metadata["notifySubscribers"] else "false"
            response = self.http.request("POST", f"{self.upload_api}?{urlencode(init_query)}", headers=headers, body=body)
            upload_url = response.headers.get("Location") or response.headers.get("location") or ""
            if not upload_url:
                raise RuntimeError("YouTube did not return a resumable upload URL")
            self.credentials.write_json(upload_key, {"url": upload_url}, "upload-session")
        offset = self._resume_offset(upload_url, total)
        chunk_size = 8 * 1024 * 1024
        with path.open("rb") as stream:
            stream.seek(offset)
            while offset < total:
                if cancelled and cancelled():
                    raise InterruptedError("YouTube upload cancelled")
                chunk = stream.read(min(chunk_size, total - offset))
                end = offset + len(chunk) - 1
                headers = {**self._headers(), "Content-Type": mime, "Content-Length": str(len(chunk)), "Content-Range": f"bytes {offset}-{end}/{total}"}
                try:
                    response = self.http.request("PUT", upload_url, headers=headers, body=chunk, timeout=180)
                    offset = end + 1
                except PublishingHttpError as exc:
                    if exc.status != 308:
                        raise
                    value = exc.headers.get("Range") or exc.headers.get("range") or ""
                    offset = int(value.rsplit("-", 1)[-1]) + 1 if "-" in value else end + 1
                    stream.seek(offset)
                    if progress: progress(offset, total)
                    continue
                if progress: progress(offset, total)
                if response.status in {200, 201}:
                    result = response.json(); video_id = str(result.get("id") or "")
                    self.credentials.delete(upload_key, "upload-session")
                    if not video_id:
                        raise RuntimeError("YouTube upload completed without a video id")
                    return video_id
        raise RuntimeError("YouTube upload did not complete")

    def _resume_offset(self, upload_url: str, total: int) -> int:
        try:
            response = self.http.request("PUT", upload_url, headers={**self._headers(), "Content-Length": "0", "Content-Range": f"bytes */{total}"}, body=b"")
        except PublishingHttpError as exc:
            if exc.status == 308:
                value = exc.headers.get("Range") or exc.headers.get("range") or ""
                return int(value.rsplit("-", 1)[-1]) + 1 if "-" in value else 0
            if exc.status in {404, 410}:
                return 0
            raise
        value = response.headers.get("Range") or response.headers.get("range") or ""
        return int(value.rsplit("-", 1)[-1]) + 1 if "-" in value else 0

    def get_status(self, video_id: str) -> dict[str, Any]:
        query = urlencode({"part": "status,processingDetails", "id": video_id})
        value = self.http.request("GET", f"{self.api}/videos?{query}", headers=self._headers()).json()
        return next(iter(value.get("items") or []), {})

    def schedule(self, upload_id: str, scheduled_at: str) -> str:
        body = json.dumps({"id": upload_id, "status": {"privacyStatus": "private", "publishAt": scheduled_at}}).encode()
        self.http.request("PUT", f"{self.api}/videos?part=status", headers={**self._headers(), "Content-Type": "application/json"}, body=body)
        return upload_id

    def refresh_token(self) -> bool:
        return bool(self.oauth.refresh_google(self.account_id).get("access_token"))

    def retry(self, _operation_id: str) -> bool:
        return True

    def cancel(self, _operation_id: str) -> bool:
        return True
