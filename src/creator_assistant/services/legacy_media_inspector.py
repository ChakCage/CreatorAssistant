from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

from creator_assistant.domain.job import CancellationToken
from creator_assistant.domain.models import FormatPlan, VideoMetadata


ProbeCallable = Callable[[Path, Optional[CancellationToken]], Dict[str, Any]]


@dataclass(frozen=True)
class LegacyMediaProbe:
    path: Path
    size: int
    duration: Optional[float] = None
    width: Optional[int] = None
    height: Optional[int] = None
    fps: Optional[float] = None
    video_codec: str = ""
    audio_codec: str = ""
    container: str = ""
    video_streams: int = 0
    audio_streams: int = 0
    sample_rate: Optional[int] = None
    channels: Optional[int] = None
    color_transfer: str = ""
    dynamic_range: str = "SDR"
    bit_rate: Optional[int] = None
    mtime: float = 0.0
    error: str = ""

    @property
    def has_video(self) -> bool:
        return self.video_streams > 0

    @property
    def has_audio(self) -> bool:
        return self.audio_streams > 0

    @property
    def is_sdr(self) -> bool:
        haystack = " ".join((self.color_transfer, self.dynamic_range)).casefold()
        return not any(marker in haystack for marker in ("smpte2084", "arib-std-b67", "hdr", "hlg", "dolby", "pq"))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "path": str(self.path),
            "size": self.size,
            "duration": self.duration,
            "width": self.width,
            "height": self.height,
            "fps": self.fps,
            "video_codec": self.video_codec,
            "audio_codec": self.audio_codec,
            "container": self.container,
            "video_streams": self.video_streams,
            "audio_streams": self.audio_streams,
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "color_transfer": self.color_transfer,
            "dynamic_range": self.dynamic_range,
            "bit_rate": self.bit_rate,
            "mtime": self.mtime,
            "error": self.error,
        }


@dataclass(frozen=True)
class LegacyRoleMatch:
    role: str
    status: str
    path: str = ""
    size: int = 0
    confidence: str = "NOT_MATCHED"
    source: str = "legacy_content_scan"
    reason: str = ""
    probe: Dict[str, Any] = field(default_factory=dict)
    candidates: List[Dict[str, Any]] = field(default_factory=list)
    validated_at: str = ""

    def to_state(self) -> Dict[str, Any]:
        state: Dict[str, Any] = {
            "status": self.status,
            "confidence": self.confidence,
            "source": self.source,
        }
        if self.path:
            state["path"] = self.path
            state["size"] = self.size
        if self.reason:
            state["reason"] = self.reason
        if self.probe:
            state["probe"] = self.probe
        if self.candidates:
            state["candidates"] = self.candidates
        if self.validated_at:
            state["validated_at"] = self.validated_at
        return state


class LegacyProjectMediaInspector:
    MEDIA_EXTENSIONS = {
        ".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v",
        ".m4a", ".mp3", ".wav", ".flac", ".ogg", ".opus",
    }
    PARTIAL_EXTENSIONS = {".part", ".ytdl", ".tmp", ".crdownload"}
    IGNORED_DIR_MARKERS = {
        "shorts cache", "shorts renders", "renders", "render",
        "fixture", "fixtures", "backup", "backups", "temp", "tmp",
    }
    WEAK_INSTRUMENTAL_MARKERS = ("instrumental", "inst", "karaoke", "no vocal")
    WEAK_AUDIO_MARKERS = ("audio", "original", "source", "vocals")
    WEAK_PROXY_MARKERS = ("proxy", "720p", "480p", "1080p")
    WEAK_MAX_MARKERS = ("max", "maximum", "source", "original")

    def __init__(self, probe: ProbeCallable) -> None:
        self.probe = probe
        self._cache: Dict[str, LegacyMediaProbe] = {}

    def inspect(
        self,
        project_path: Path,
        metadata: VideoMetadata,
        plan: FormatPlan,
        *,
        proxy_height: int,
        cancellation: Optional[CancellationToken] = None,
    ) -> Dict[str, LegacyRoleMatch]:
        probes = [probe for probe in self._scan(project_path, cancellation) if not probe.error]
        now = dt.datetime.now().astimezone().isoformat(timespec="seconds")
        return {
            "maximum": self._select(
                "MAX_VIDEO",
                probes,
                lambda item: self._score_maximum(item, metadata, plan),
                now,
            ),
            "proxy": self._select(
                "REAPER_PROXY",
                probes,
                lambda item: self._score_proxy(item, metadata, plan, proxy_height),
                now,
            ),
            "audio": self._select(
                "ORIGINAL_AUDIO",
                probes,
                lambda item: self._score_audio(item, metadata, instrumental=False),
                now,
            ),
            "instrumental": self._select(
                "INSTRUMENTAL",
                probes,
                lambda item: self._score_audio(item, metadata, instrumental=True),
                now,
            ),
        }

    def _scan(self, project_path: Path, cancellation: Optional[CancellationToken]) -> List[LegacyMediaProbe]:
        probes: List[LegacyMediaProbe] = []
        for path in self._candidate_files(project_path):
            if cancellation:
                cancellation.raise_if_cancelled()
            cache_key = str(path.resolve()).casefold()
            cached = self._cache.get(cache_key)
            if cached:
                probes.append(cached)
                continue
            probe = self._probe_file(path, cancellation)
            self._cache[cache_key] = probe
            probes.append(probe)
        return probes

    def _candidate_files(self, project_path: Path) -> Iterable[Path]:
        seen: set[str] = set()
        roots = [project_path, project_path / "Материалы"]
        for root in roots:
            if not root.is_dir():
                continue
            for path in self._files_one_level(root):
                key = str(path).casefold()
                if key not in seen and self._is_candidate(path):
                    seen.add(key)
                    yield path

    def _files_one_level(self, root: Path) -> Iterable[Path]:
        try:
            children = list(root.iterdir())
        except OSError:
            return
        for child in children:
            if child.is_file():
                yield child
            elif child.is_dir() and not self._ignored_path(child):
                try:
                    for nested in child.iterdir():
                        if nested.is_file():
                            yield nested
                except OSError:
                    continue

    def _is_candidate(self, path: Path) -> bool:
        if self._ignored_path(path):
            return False
        suffixes = {suffix.casefold() for suffix in path.suffixes}
        if suffixes & self.PARTIAL_EXTENSIONS:
            return False
        try:
            if path.stat().st_size <= 0:
                return False
        except OSError:
            return False
        return path.suffix.casefold() in self.MEDIA_EXTENSIONS

    def _ignored_path(self, path: Path) -> bool:
        directory_names = [path.name.casefold()] if path.is_dir() else [path.parent.name.casefold()]
        if any(marker in part for part in directory_names for marker in self.IGNORED_DIR_MARKERS):
            return True
        stem = path.name.casefold()
        return any(marker in stem for marker in (".part", ".ytdl", ".tmp"))

    def _probe_file(self, path: Path, cancellation: Optional[CancellationToken]) -> LegacyMediaProbe:
        try:
            stat = path.stat()
            raw = self.probe(path, cancellation)
            streams = raw.get("streams", []) if isinstance(raw, dict) else []
            video_streams = [item for item in streams if isinstance(item, dict) and item.get("codec_type") == "video"]
            audio_streams = [item for item in streams if isinstance(item, dict) and item.get("codec_type") == "audio"]
            video = video_streams[0] if video_streams else {}
            audio = audio_streams[0] if audio_streams else {}
            fmt = raw.get("format", {}) if isinstance(raw.get("format"), dict) else {}
            return LegacyMediaProbe(
                path=path,
                size=stat.st_size,
                duration=self._float(fmt.get("duration")),
                width=self._int(video.get("width")),
                height=self._int(video.get("height")),
                fps=self._rate(video.get("avg_frame_rate") or video.get("r_frame_rate")),
                video_codec=str(video.get("codec_name") or ""),
                audio_codec=str(audio.get("codec_name") or ""),
                container=str(fmt.get("format_name") or path.suffix.lstrip(".")),
                video_streams=len(video_streams),
                audio_streams=len(audio_streams),
                sample_rate=self._int(audio.get("sample_rate")),
                channels=self._int(audio.get("channels")),
                color_transfer=str(video.get("color_transfer") or ""),
                dynamic_range=str(video.get("dynamic_range") or video.get("color_transfer") or "SDR"),
                bit_rate=self._int(fmt.get("bit_rate")),
                mtime=stat.st_mtime,
            )
        except Exception as exc:
            try:
                stat = path.stat()
                size = stat.st_size
                mtime = stat.st_mtime
            except OSError:
                size = 0
                mtime = 0.0
            return LegacyMediaProbe(path=path, size=size, mtime=mtime, error=str(exc))

    def _select(
        self,
        role: str,
        probes: List[LegacyMediaProbe],
        score_func: Callable[[LegacyMediaProbe], Optional[tuple[int, float, str]]],
        validated_at: str,
    ) -> LegacyRoleMatch:
        scored: List[tuple[int, float, str, LegacyMediaProbe]] = []
        for item in probes:
            score = score_func(item)
            if score:
                scored.append((score[0], score[1], score[2], item))
        if not scored:
            return LegacyRoleMatch(role, "NOT_MATCHED", reason="No content-compatible files were found.")
        scored.sort(key=lambda item: (item[0], item[1], item[3].size), reverse=True)
        high = [item for item in scored if item[0] >= 100]
        if len(high) == 1:
            _rank, _weight, reason, probe = high[0]
            return LegacyRoleMatch(
                role,
                "VALID",
                str(probe.path),
                probe.size,
                "HIGH",
                reason=reason,
                probe=probe.to_dict(),
                validated_at=validated_at,
            )
        if len(high) > 1:
            return LegacyRoleMatch(
                role,
                "AMBIGUOUS",
                confidence="AMBIGUOUS",
                reason="Several files match this role with high confidence.",
                candidates=[self._candidate_dict(item[3], item[2]) for item in high[:8]],
                validated_at=validated_at,
            )
        best = scored[:8]
        return LegacyRoleMatch(
            role,
            "AMBIGUOUS",
            confidence="MEDIUM",
            reason="The scan found possible files, but none were safe enough to auto-accept.",
            candidates=[self._candidate_dict(item[3], item[2]) for item in best],
            validated_at=validated_at,
        )

    def _score_maximum(
        self,
        item: LegacyMediaProbe,
        metadata: VideoMetadata,
        plan: FormatPlan,
    ) -> Optional[tuple[int, float, str]]:
        if not item.has_video or not item.has_audio or not item.is_sdr:
            return None
        if not self._duration_matches(item.duration, metadata.duration):
            return None
        expected_height = plan.maximum_video.height
        if expected_height and item.height != expected_height:
            return None
        if not self._fps_matches(item.fps, plan.maximum_video.fps):
            return None
        if self._looks_like_proxy_or_short(item):
            return None
        rank = 100
        if self._weak_name(item.path, self.WEAK_MAX_MARKERS):
            rank += 4
        return rank, float(item.height or 0), "Duration, SDR, video/audio streams, resolution and FPS match MAX_VIDEO."

    def _score_proxy(
        self,
        item: LegacyMediaProbe,
        metadata: VideoMetadata,
        plan: FormatPlan,
        proxy_height: int,
    ) -> Optional[tuple[int, float, str]]:
        if not item.has_video or not item.has_audio or not item.is_sdr:
            return None
        if item.width and item.height and item.height > item.width:
            return None
        if not self._duration_matches(item.duration, metadata.duration):
            return None
        if item.height != proxy_height:
            return None
        if not self._fps_matches(item.fps, plan.proxy_video.fps or plan.maximum_video.fps):
            return None
        rank = 100
        if self._weak_name(item.path, self.WEAK_PROXY_MARKERS):
            rank += 4
        return rank, float(item.height or 0), "Duration, SDR, video/audio streams, horizontal frame, height and FPS match REAPER_PROXY."

    def _score_audio(
        self,
        item: LegacyMediaProbe,
        metadata: VideoMetadata,
        *,
        instrumental: bool,
    ) -> Optional[tuple[int, float, str]]:
        if not item.has_audio or item.has_video:
            return None
        if not self._duration_matches(item.duration, metadata.duration):
            return None
        if instrumental and item.path.suffix.casefold() != ".flac":
            return None
        name_markers = self.WEAK_INSTRUMENTAL_MARKERS if instrumental else self.WEAK_AUDIO_MARKERS
        if instrumental:
            rank = 104 if self._weak_name(item.path, name_markers) else 100
        else:
            rank = 104 if self._weak_name(item.path, name_markers) else (80 if item.path.suffix.casefold() == ".flac" else 100)
        reason = "Duration and audio-only stream match " + ("INSTRUMENTAL." if instrumental else "ORIGINAL_AUDIO.")
        return rank, float(item.bit_rate or item.size), reason

    def _looks_like_proxy_or_short(self, item: LegacyMediaProbe) -> bool:
        name = item.path.name.casefold()
        if "short" in name or "preview" in name or "proxy" in name:
            return True
        return bool(item.width and item.height and item.height > item.width)

    @staticmethod
    def _candidate_dict(item: LegacyMediaProbe, reason: str) -> Dict[str, Any]:
        data = item.to_dict()
        data["reason"] = reason
        return data

    @staticmethod
    def _duration_matches(actual: Optional[float], expected: Optional[float]) -> bool:
        if not expected or not actual:
            return True
        return abs(actual - expected) <= max(1.0, expected * 0.005)

    @staticmethod
    def _fps_matches(actual: Optional[float], expected: Optional[float]) -> bool:
        if not expected or not actual:
            return True
        return abs(actual - expected) <= 0.15

    @staticmethod
    def _weak_name(path: Path, markers: Iterable[str]) -> bool:
        name = path.name.casefold()
        return any(marker in name for marker in markers)

    @staticmethod
    def _int(value: Any) -> Optional[int]:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _float(value: Any) -> Optional[float]:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _rate(value: Any) -> Optional[float]:
        try:
            text = str(value)
            if "/" in text:
                numerator, denominator = text.split("/", 1)
                den = float(denominator)
                return float(numerator) / den if den else None
            return float(text)
        except (TypeError, ValueError, ZeroDivisionError):
            return None
