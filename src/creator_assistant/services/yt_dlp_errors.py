from __future__ import annotations

from enum import Enum

from creator_assistant.domain.errors import (
    ProcessExecutionError,
    YouTubeAuthenticationRequiredError,
    YouTubeCookiesUnavailableError,
)
from creator_assistant.domain.youtube_auth import BROWSERS, YtDlpAuthContext


AUTH_MARKERS = (
    "sign in to confirm you’re not a bot",
    "sign in to confirm you're not a bot",
    "use --cookies-from-browser",
    "login required",
    "confirm you’re not a bot",
    "confirm you're not a bot",
    "this video is private",
    "private video",
    "this video is age-restricted",
    "age-restricted",
    "members-only content",
    "members only",
)
COOKIE_MARKERS = (
    "database is locked",
    "permission denied",
    "could not copy cookie database",
    "failed to decrypt cookies",
    "dpapi error",
    "failed to decrypt with dpapi",
    "cookie database unavailable",
    "could not copy chrome cookie database",
    "app-bound encryption",
    "app bound encryption",
)


class YouTubeAccessCategory(str, Enum):
    ANONYMOUS_ACCESS = "ANONYMOUS_ACCESS"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    MEDIA_URL_EXPIRED = "MEDIA_URL_EXPIRED"
    MEDIA_HTTP_403 = "MEDIA_HTTP_403"
    PO_TOKEN_REQUIRED = "PO_TOKEN_REQUIRED"
    NETWORK_ERROR = "NETWORK_ERROR"
    COOKIE_READ_ERROR = "COOKIE_READ_ERROR"
    FORMAT_UNAVAILABLE = "FORMAT_UNAVAILABLE"
    TOOL_ERROR = "TOOL_ERROR"


def classify_youtube_failure(details: str, auth: YtDlpAuthContext, media_operation: bool = False) -> YouTubeAccessCategory:
    lower = relevant_stderr(details).casefold()
    if auth.enabled and any(marker in lower for marker in COOKIE_MARKERS):
        return YouTubeAccessCategory.COOKIE_READ_ERROR
    if any(marker in lower for marker in AUTH_MARKERS):
        return YouTubeAccessCategory.AUTH_REQUIRED
    if media_operation and ("http error 403" in lower or "403: forbidden" in lower):
        return YouTubeAccessCategory.MEDIA_HTTP_403
    if any(marker in lower for marker in (
        "requires a gvs po token",
        "po token was not provided",
        "formats may yield http error 403",
    )):
        return YouTubeAccessCategory.PO_TOKEN_REQUIRED
    if any(marker in lower for marker in (
        "read timed out", "connection reset", "connection aborted", "temporarily unavailable",
        "http error 500", "http error 502", "http error 503", "http error 504",
    )):
        return YouTubeAccessCategory.NETWORK_ERROR
    if any(marker in lower for marker in ("requested format is not available", "format unavailable")):
        return YouTubeAccessCategory.FORMAT_UNAVAILABLE
    if media_operation and any(marker in lower for marker in ("url has expired", "expired url", "signature has expired")):
        return YouTubeAccessCategory.MEDIA_URL_EXPIRED
    return YouTubeAccessCategory.TOOL_ERROR


def relevant_stderr(details: str) -> str:
    return "\n".join(line for line in str(details).splitlines() if line.strip().casefold() != "null").strip()


def classify_yt_dlp_error(
    exc: ProcessExecutionError,
    url: str,
    video_id: str,
    auth: YtDlpAuthContext,
):
    stderr = relevant_stderr(exc.details)
    lower = stderr.casefold()
    if auth.enabled and auth.mode != "none" and any(marker in lower for marker in COOKIE_MARKERS):
        browser_name = BROWSERS.get(auth.browser, auth.browser)
        if any(marker in lower for marker in ("decrypt", "dpapi", "app-bound", "app bound")):
            reason = (
                f"Текущая версия {browser_name} не позволяет Creator Assistant прочитать cookies. "
                "Используйте Firefox или файл cookies.txt."
            )
        elif any(marker in lower for marker in ("locked", "could not copy", "permission denied")):
            reason = f"Полностью закройте {browser_name} и повторите."
        else:
            reason = f"Creator Assistant не смог прочитать cookies {browser_name}. Выберите Firefox или файл cookies.txt."
        return YouTubeCookiesUnavailableError(
            auth.browser,
            reason,
            stderr,
        )
    if any(marker in lower for marker in AUTH_MARKERS):
        return YouTubeAuthenticationRequiredError(
            url,
            video_id,
            "YouTube не разрешил получить данные без подтверждённой браузерной сессии.",
            stderr,
            cookies_used=bool(auth.enabled and auth.mode != "none"),
            browser=auth.browser if auth.mode == "browser" else "",
        )
    return exc
