from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Dict, Optional, Tuple

from creator_assistant.domain.errors import (
    InvalidVideoUrlError,
    JobCancelledError,
    MetadataRequestFailedError,
    PlaylistNotSupportedError,
    ProcessExecutionError,
    TransientMetadataError,
    YouTubeAuthenticationRequiredError,
    YouTubeCookiesUnavailableError,
)
from creator_assistant.domain.job import CancellationToken
from creator_assistant.domain.models import VideoMetadata
from creator_assistant.services.metadata_service import MetadataService, validate_youtube_url, youtube_video_id


class MetadataResultCategory(str, Enum):
    SUCCESS = "SUCCESS"
    TRANSIENT_ERROR = "TRANSIENT_ERROR"
    AUTH_CHALLENGE = "AUTH_CHALLENGE"
    NETWORK_ERROR = "NETWORK_ERROR"
    INVALID_URL = "INVALID_URL"
    VIDEO_UNAVAILABLE = "VIDEO_UNAVAILABLE"
    TOOL_ERROR = "TOOL_ERROR"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True)
class RetryPolicy:
    automatic_attempts: int = 2
    retry_delay_min: float = 3.0
    retry_delay_max: float = 5.0
    manual_delay_min: float = 5.0
    manual_delay_max: float = 8.0
    cache_ttl_seconds: float = 600.0

    def delay(self, manual: bool = False) -> float:
        low = self.manual_delay_min if manual else self.retry_delay_min
        high = self.manual_delay_max if manual else self.retry_delay_max
        return random.uniform(low, high)


@dataclass(frozen=True)
class MetadataRequestOutcome:
    metadata: VideoMetadata
    attempts: int
    categories: Tuple[MetadataResultCategory, ...]
    from_cache: bool = False


class MetadataRequestController:
    TRANSIENT_MARKERS = (
        "the page needs to be reloaded",
        "no title found in player responses",
        "player response",
        "temporarily unavailable",
        "http error 500",
        "http error 502",
        "http error 503",
        "http error 504",
        "extractor error",
    )
    NETWORK_MARKERS = ("timed out", "network is unreachable", "connection reset", "temporary failure in name resolution")
    UNAVAILABLE_MARKERS = ("video unavailable", "private video", "this video has been removed")

    def __init__(
        self,
        service: MetadataService,
        policy: Optional[RetryPolicy] = None,
        logger: Optional[logging.Logger] = None,
        yt_dlp_version: str = "",
    ) -> None:
        self.service = service
        self.policy = policy or RetryPolicy()
        self.logger = logger or logging.getLogger("creator_assistant")
        self.yt_dlp_version = yt_dlp_version
        self._cache: Dict[Tuple[str, str], Tuple[float, VideoMetadata]] = {}

    def request(
        self,
        url: str,
        cancellation: CancellationToken,
        request_id: str,
        on_status: Optional[Callable[[str], None]] = None,
        force: bool = False,
        manual_extra_attempt: bool = False,
        anonymous_first: bool = True,
    ) -> MetadataRequestOutcome:
        valid_url = validate_youtube_url(url)
        video_id = youtube_video_id(valid_url)
        original_auth = (
            self.service.auth.mode,
            self.service.auth.browser,
            self.service.auth.browser_profile,
            self.service.auth.cookies_file,
            self.service.auth.enabled,
            self.service.auth.user_consented,
        )
        if anonymous_first:
            self.service.auth.disable_for_anonymous_job()
        auth_summary = self.service.auth.summary
        cache_key = (video_id, auth_summary)
        cached = self._cache.get(cache_key)
        if not force and cached and time.monotonic() - cached[0] <= self.policy.cache_ttl_seconds:
            return MetadataRequestOutcome(cached[1], 0, (MetadataResultCategory.SUCCESS,), True)
        max_attempts = 1 if manual_extra_attempt else self.policy.automatic_attempts
        categories = []
        try:
            if manual_extra_attempt:
                delay = self.policy.delay(True)
                if on_status:
                    on_status(f"Дополнительная анонимная попытка через {int(round(delay))} с…")
                cancellation.wait(delay)
            for attempt in range(1, max_attempts + 1):
                cancellation.raise_if_cancelled()
                if on_status:
                    on_status(f"Получение информации — попытка {attempt} из {max_attempts}")
                self.logger.info(
                    "YouTube operation=metadata request_id=%s video_id=%s attempt=%s/%s auth_mode=%s "
                    "cookies_argument_present=%s po_token_provider=unknown player_client=unknown yt_dlp=%s version=%s",
                    request_id, video_id, attempt, max_attempts, self.service.auth.effective_mode,
                    "yes" if self.service.auth.arguments(validate=False) else "no",
                    self.service.yt_dlp_path, self.yt_dlp_version or "unknown",
                )
                try:
                    metadata = self.service.fetch(valid_url, cancellation)
                    categories.append(MetadataResultCategory.SUCCESS)
                    self._cache[cache_key] = (time.monotonic(), metadata)
                    return MetadataRequestOutcome(metadata, attempt, tuple(categories), False)
                except (InvalidVideoUrlError, PlaylistNotSupportedError):
                    categories.append(MetadataResultCategory.INVALID_URL)
                    raise
                except YouTubeAuthenticationRequiredError:
                    category = MetadataResultCategory.AUTH_CHALLENGE
                    categories.append(category)
                    if attempt >= max_attempts:
                        raise
                except TransientMetadataError as exc:
                    category = MetadataResultCategory.TRANSIENT_ERROR
                    categories.append(category)
                    if attempt >= max_attempts:
                        raise MetadataRequestFailedError(category.value, "YouTube дважды вернул временно неполные данные.", exc.details)
                except YouTubeCookiesUnavailableError:
                    raise
                except ProcessExecutionError as exc:
                    category = self.classify_process_error(exc)
                    categories.append(category)
                    if category not in (MetadataResultCategory.TRANSIENT_ERROR, MetadataResultCategory.NETWORK_ERROR) or attempt >= max_attempts:
                        raise MetadataRequestFailedError(category.value, self.user_message(category), exc.details)
                if attempt < max_attempts:
                    delay = self.policy.delay(manual_extra_attempt)
                    if on_status:
                        on_status(f"YouTube временно отклонил запрос. Повторная попытка через {int(round(delay))} с…")
                    cancellation.wait(delay)
        except JobCancelledError:
            categories.append(MetadataResultCategory.CANCELLED)
            raise
        finally:
            if anonymous_first and not any(category == MetadataResultCategory.SUCCESS for category in categories):
                (
                    self.service.auth.mode,
                    self.service.auth.browser,
                    self.service.auth.browser_profile,
                    self.service.auth.cookies_file,
                    self.service.auth.enabled,
                    self.service.auth.user_consented,
                ) = original_auth
        raise MetadataRequestFailedError(MetadataResultCategory.TOOL_ERROR.value, "Не удалось получить метаданные.")

    def classify_process_error(self, exc: ProcessExecutionError) -> MetadataResultCategory:
        lower = str(exc.details).casefold()
        if any(marker in lower for marker in self.TRANSIENT_MARKERS):
            return MetadataResultCategory.TRANSIENT_ERROR
        if any(marker in lower for marker in self.NETWORK_MARKERS):
            return MetadataResultCategory.NETWORK_ERROR
        if any(marker in lower for marker in self.UNAVAILABLE_MARKERS):
            return MetadataResultCategory.VIDEO_UNAVAILABLE
        return MetadataResultCategory.TOOL_ERROR

    @staticmethod
    def user_message(category: MetadataResultCategory) -> str:
        return {
            MetadataResultCategory.TRANSIENT_ERROR: "YouTube дважды временно отклонил запрос.",
            MetadataResultCategory.NETWORK_ERROR: "Не удалось связаться с YouTube. Проверьте подключение к сети.",
            MetadataResultCategory.VIDEO_UNAVAILABLE: "Видео недоступно или удалено.",
            MetadataResultCategory.TOOL_ERROR: "yt-dlp завершился с непредвиденной ошибкой.",
        }.get(category, "Не удалось получить метаданные YouTube.")

    def clear_cache(self, video_id: str = "") -> None:
        if not video_id:
            self._cache.clear()
            return
        self._cache = {key: value for key, value in self._cache.items() if key[0] != video_id}
