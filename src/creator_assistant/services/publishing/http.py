from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class PublishingHttpError(RuntimeError):
    def __init__(self, status: int, message: str, payload: dict[str, Any] | None = None, headers: dict[str, str] | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.payload = payload or {}
        self.headers = headers or {}


@dataclass
class HttpResponse:
    status: int
    headers: dict[str, str]
    body: bytes

    def json(self) -> dict[str, Any]:
        if not self.body:
            return {}
        value = json.loads(self.body.decode("utf-8"))
        return value if isinstance(value, dict) else {}


class PublishingHttpClient:
    def request(self, method: str, url: str, *, headers: dict[str, str] | None = None, body: bytes | None = None, timeout: float = 60) -> HttpResponse:
        request = Request(url, data=body, headers=headers or {}, method=method)
        try:
            with urlopen(request, timeout=timeout) as response:
                return HttpResponse(int(response.status), dict(response.headers.items()), response.read())
        except HTTPError as exc:
            raw = exc.read()
            try:
                payload = json.loads(raw.decode("utf-8")) if raw else {}
            except (UnicodeError, ValueError):
                payload = {}
            message = self._safe_message(payload) or f"Publishing API returned HTTP {exc.code}"
            raise PublishingHttpError(int(exc.code), message, payload, dict(exc.headers.items()) if exc.headers else {}) from exc
        except URLError as exc:
            raise PublishingHttpError(0, f"Publishing API is unavailable: {exc.reason}") from exc

    @staticmethod
    def _safe_message(payload: Any) -> str:
        if not isinstance(payload, dict):
            return ""
        error = payload.get("error")
        if isinstance(error, dict):
            return str(error.get("message") or error.get("code") or "")
        return str(error or payload.get("message") or "")
