from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse

from creator_assistant.domain.errors import DependencyMissingError, InvalidVideoUrlError, PlaylistNotSupportedError, ProcessExecutionError, TransientMetadataError
from creator_assistant.domain.job import CancellationToken
from creator_assistant.domain.models import ThumbnailInfo, VideoMetadata, format_from_dict
from creator_assistant.domain.youtube_auth import YtDlpAuthContext
from creator_assistant.infrastructure.process_runner import ProcessRunner
from creator_assistant.services.yt_dlp_errors import classify_yt_dlp_error


def youtube_video_id(url: str) -> str:
    raw = url.strip()
    try:
        parsed = urlparse(raw)
    except ValueError as exc:
        raise InvalidVideoUrlError("Ссылка имеет неверный формат.") from exc
    host = (parsed.hostname or "").casefold()
    allowed = host == "youtu.be" or host == "youtube.com" or host.endswith(".youtube.com")
    if parsed.scheme not in ("http", "https") or not allowed:
        raise InvalidVideoUrlError("Введите обычную ссылку на публичное видео YouTube.")
    query = parse_qs(parsed.query)
    if "list" in query or parsed.path.casefold().startswith(("/playlist", "/channel", "/@")):
        raise PlaylistNotSupportedError("Плейлисты и страницы каналов не поддерживаются. Нужна одна ссылка на видео.")
    if host == "youtu.be" and not parsed.path.strip("/"):
        raise InvalidVideoUrlError("В короткой ссылке отсутствует идентификатор видео.")
    if host != "youtu.be" and parsed.path.casefold() not in ("/watch", "/shorts") and not parsed.path.casefold().startswith("/shorts/"):
        raise InvalidVideoUrlError("Введите прямую ссылку на одно видео YouTube.")
    if host == "youtu.be":
        video_id = parsed.path.strip("/").split("/", 1)[0]
    elif parsed.path.casefold().startswith("/shorts/"):
        video_id = parsed.path.strip("/").split("/", 1)[1]
    else:
        video_id = str(query.get("v", [""])[0])
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
        raise InvalidVideoUrlError("В ссылке отсутствует корректный идентификатор видео.")
    return video_id


def validate_youtube_url(url: str) -> str:
    video_id = youtube_video_id(url)
    return f"https://www.youtube.com/watch?v={video_id}"


class MetadataService:
    def __init__(self, runner: ProcessRunner, yt_dlp_path: str, auth: Optional[YtDlpAuthContext] = None) -> None:
        self.runner = runner
        self.yt_dlp_path = yt_dlp_path
        self.auth = auth or YtDlpAuthContext()

    def fetch(self, url: str, cancellation: CancellationToken) -> VideoMetadata:
        valid_url = validate_youtube_url(url)
        if not self.yt_dlp_path or not Path(self.yt_dlp_path).is_file():
            raise DependencyMissingError("yt-dlp не найден. Откройте диагностику и укажите путь к программе.")
        command = [
                self.yt_dlp_path,
                "--ignore-config",
                "--no-playlist",
                "--skip-download",
                "--dump-single-json",
                "--retries", "1",
                "--extractor-retries", "1",
                "--fragment-retries", "1",
                "--no-write-subs",
                "--no-write-auto-subs",
                "--no-write-comments",
                "--no-write-info-json",
            ]
        command.extend(self.auth.arguments())
        command.append(valid_url)
        try:
            result = self.runner.run(command, cancellation=cancellation)
        except ProcessExecutionError as exc:
            raise classify_yt_dlp_error(exc, valid_url, youtube_video_id(valid_url), self.auth)
        raw_json = (result.stdout or "").strip()
        if not raw_json or raw_json.casefold() == "null":
            raise ProcessExecutionError("yt-dlp не вернул метаданные видео.", result.stderr[-4000:])
        try:
            data = json.loads(raw_json)
        except json.JSONDecodeError as exc:
            raise ProcessExecutionError("yt-dlp вернул непонятный ответ.", result.stderr[-4000:]) from exc
        if not isinstance(data, dict):
            raise ProcessExecutionError("yt-dlp не вернул объект метаданных.", result.stderr[-4000:])
        if data.get("_type") in ("playlist", "multi_video") or data.get("entries"):
            raise PlaylistNotSupportedError("Плейлисты не поддерживаются. Нужна одна ссылка на видео.")
        formats = [format_from_dict(item) for item in data.get("formats", []) if item.get("format_id")]
        thumbnails = [
            ThumbnailInfo(
                url=str(item.get("url", "")),
                width=int(item.get("width") or 0),
                height=int(item.get("height") or 0),
                preference=int(item.get("preference") or 0),
            )
            for item in data.get("thumbnails", [])
            if item.get("url")
        ]
        video_id = str(data.get("id") or "")
        title = str(data.get("title") or "").strip()
        duration = float(data["duration"]) if data.get("duration") is not None else None
        if not video_id or not title or duration is None or not formats:
            raise TransientMetadataError(
                "YouTube временно вернул неполные метаданные.",
                result.stderr[-4000:],
            )
        return VideoMetadata(
            video_id=video_id,
            title=title,
            duration=duration,
            webpage_url=str(data.get("webpage_url") or valid_url),
            formats=formats,
            thumbnails=thumbnails,
        )
