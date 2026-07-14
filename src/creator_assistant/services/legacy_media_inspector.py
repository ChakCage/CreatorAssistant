from __future__ import annotations

import datetime as dt
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

from creator_assistant.domain.job import CancellationToken
from creator_assistant.domain.models import FormatPlan, VideoMetadata
from creator_assistant.infrastructure.manifest_store import LegacyProjectScanner, project_metadata_dir


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
    AUDIO_EXTENSIONS = {".m4a", ".mp3", ".wav", ".flac", ".ogg", ".opus", ".webm", ".aac"}
    VIDEO_EXTENSIONS = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v"}
    WEAK_INSTRUMENTAL_MARKERS = (
        "instrumental", "instrument", "inst", "karaoke", "no vocals", "accompaniment",
        "инструментал", "без голоса",
    )
    INSTRUMENTAL_NEGATIVE_MARKERS = (
        "vocals", "vocal", "voice", "voiceover", "озвучка", "original audio", "final mix", "master",
    )
    WEAK_AUDIO_MARKERS = (
        "[audio]", "original audio", "original", "source", "orig", "оригинал", "исходный звук",
    )
    ORIGINAL_AUDIO_NEGATIVE_MARKERS = (
        "instrumental", "instrument", "inst", "vocals", "vocal", "voice", "voiceover", "озвучка",
        "голос", "дубляж", "mix", "master", "final", "render", "готово", "edited", "processed",
        "normalized", "shorts", "music", "background", "bgm", "reaper", "vegas", "stem", "accompaniment",
    )
    WEAK_PROXY_MARKERS = ("proxy", "720p", "480p", "1080p")
    WEAK_MAX_MARKERS = ("max", "maximum", "source", "original")

    ROLE_LIMITS = {"maximum": 10, "proxy": 10, "audio": 15, "instrumental": 10}

    def __init__(self, probe: ProbeCallable, cache_path: Optional[Path] = None) -> None:
        self.probe = probe
        self._cache: Dict[str, LegacyMediaProbe] = {}
        self.cache_path = cache_path
        self.stats: Dict[str, int] = {
            "allowed_files": 0, "shortlist": 0, "ffprobe_calls": 0,
            "cache_hits": 0, "ignored_sidecar_files": 0, "excluded_directories": 0,
        }
        self._persistent_cache = self._load_cache()

    def inspect(
        self,
        project_path: Path,
        metadata: VideoMetadata,
        plan: FormatPlan,
        *,
        proxy_height: int,
        cancellation: Optional[CancellationToken] = None,
        required_roles: Optional[Iterable[str]] = None,
        assigned_paths: Optional[Dict[str, Path]] = None,
    ) -> Dict[str, LegacyRoleMatch]:
        required = set(required_roles or {"maximum", "proxy", "audio", "instrumental"})
        assigned = {key: Path(value) for key, value in (assigned_paths or {}).items() if value}
        probes = [probe for probe in self._scan(project_path, cancellation, required, assigned) if not probe.error]
        now = dt.datetime.now().astimezone().isoformat(timespec="seconds")
        result: Dict[str, LegacyRoleMatch] = {
            key: LegacyRoleMatch(role, "NOT_REQUIRED", confidence="NOT_REQUIRED", reason="Role is not required by the selected plan.")
            for key, role in {
                "maximum": "MAX_VIDEO", "proxy": "REAPER_PROXY",
                "audio": "ORIGINAL_AUDIO", "instrumental": "INSTRUMENTAL",
            }.items()
        }
        if "maximum" in required:
            result["maximum"] = self._select(
                "MAX_VIDEO",
                probes,
                lambda item: self._score_maximum(item, metadata, plan),
                now,
                preferred=assigned.get("maximum"),
            )
        if "proxy" in required:
            result["proxy"] = self._select(
                "REAPER_PROXY",
                probes,
                lambda item: self._score_proxy(item, metadata, plan, proxy_height),
                now,
                preferred=assigned.get("proxy"),
            )
        occupied = {
            str(path.resolve()).casefold(): role for role, path in assigned.items() if path.exists()
        }
        if "instrumental" in required:
            result["instrumental"] = self._select(
                "INSTRUMENTAL",
                probes,
                lambda item: self._score_audio(item, metadata, instrumental=True),
                now,
                preferred=assigned.get("instrumental"),
            )
            if result["instrumental"].status == "VALID":
                occupied[str(Path(result["instrumental"].path).resolve()).casefold()] = "instrumental"
        if "audio" in required:
            available = [
                item for item in probes
                if occupied.get(str(item.path.resolve()).casefold()) in (None, "audio")
            ]
            result["audio"] = self._select(
                "ORIGINAL_AUDIO",
                available,
                lambda item: self._score_audio(item, metadata, instrumental=False),
                now,
                preferred=assigned.get("audio"),
            )
        self._save_cache()
        return result

    def _scan(
        self,
        project_path: Path,
        cancellation: Optional[CancellationToken],
        required_roles: set[str],
        assigned_paths: Dict[str, Path],
    ) -> List[LegacyMediaProbe]:
        probes: List[LegacyMediaProbe] = []
        paths = self._candidate_files(project_path)
        shortlisted: Dict[str, Path] = {}
        for role in required_roles:
            scored_paths = sorted(
                ((self._cheap_score(path, role), path) for path in paths),
                key=lambda item: item[0],
                reverse=True,
            )
            positive = [item for item in scored_paths if item[0] > 0]
            if len(positive) > 1 and positive[0][0] >= 45 and positive[0][0] - positive[1][0] >= 10:
                positive = [item for item in positive if item[0] >= positive[0][0] - 4]
            for score, path in positive[: self.ROLE_LIMITS.get(role, 10)]:
                if score > 0:
                    shortlisted[str(path.resolve()).casefold()] = path
        for role, path in assigned_paths.items():
            if role in required_roles and path.is_file():
                shortlisted[str(path.resolve()).casefold()] = path
        self.stats["shortlist"] = len(shortlisted)
        for path in shortlisted.values():
            if cancellation:
                cancellation.raise_if_cancelled()
            stat = path.stat()
            cache_key = self._cache_key(path, stat.st_size, stat.st_mtime)
            cached = self._cache.get(cache_key)
            if cached:
                probes.append(cached)
                continue
            persistent = self._persistent_cache.get(cache_key)
            if isinstance(persistent, dict):
                probe = self._probe_from_dict(path, persistent)
                self.stats["cache_hits"] += 1
            else:
                probe = self._probe_file(path, cancellation)
                self.stats["ffprobe_calls"] += 1
            self._cache[cache_key] = probe
            if not probe.error:
                self._persistent_cache[cache_key] = probe.to_dict()
            probes.append(probe)
        return probes

    def _candidate_files(self, project_path: Path) -> List[Path]:
        paths, excluded, sidecars = LegacyProjectScanner._allowed_files(project_path)
        self.stats["excluded_directories"] = excluded
        self.stats["ignored_sidecar_files"] = sidecars
        candidates = [path for path in paths if self._is_candidate(path)]
        self.stats["allowed_files"] = len(paths)
        return candidates

    def _is_candidate(self, path: Path) -> bool:
        try:
            if path.stat().st_size <= 0:
                return False
        except OSError:
            return False
        return path.suffix.casefold() in self.MEDIA_EXTENSIONS

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
        preferred: Optional[Path] = None,
    ) -> LegacyRoleMatch:
        scored: List[tuple[int, float, str, LegacyMediaProbe]] = []
        for item in probes:
            score = score_func(item)
            if score:
                scored.append((score[0], score[1], score[2], item))
        if not scored:
            return LegacyRoleMatch(role, "NOT_MATCHED", reason="No content-compatible files were found.")
        scored.sort(key=lambda item: (item[0], item[1], item[3].size), reverse=True)
        if preferred:
            preferred_key = str(preferred.resolve()).casefold()
            chosen = next((item for item in scored if str(item[3].path.resolve()).casefold() == preferred_key), None)
            if chosen:
                _rank, _weight, reason, probe = chosen
                return LegacyRoleMatch(
                    role, "VALID", str(probe.path), probe.size, "HIGH", source="manifest",
                    reason="Previously confirmed manifest path. " + reason,
                    probe=probe.to_dict(), validated_at=validated_at,
                )
        if role == "audio":
            if len(scored) == 1 and scored[0][0] >= 100:
                _rank, _weight, reason, probe = scored[0]
                return LegacyRoleMatch(
                    role, "VALID", str(probe.path), probe.size, "HIGH",
                    reason=reason, probe=probe.to_dict(), validated_at=validated_at,
                )
            if len(scored) > 1:
                return LegacyRoleMatch(
                    role,
                    "AMBIGUOUS",
                    confidence="AMBIGUOUS",
                    reason="Several content-compatible files require user selection.",
                    candidates=[self._candidate_dict(item[3], item[2]) for item in scored[:8]],
                    validated_at=validated_at,
                )
        high = [item for item in scored if item[0] >= 100]
        if high and (len(high) == 1 or high[0][0] - high[1][0] >= 8):
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
        if len(best) == 1:
            _rank, _weight, reason, probe = best[0]
            return LegacyRoleMatch(
                role,
                "CONFIRMATION_REQUIRED",
                confidence="MEDIUM" if _rank >= 80 else "LOW",
                reason="One content-compatible file needs user confirmation.",
                candidates=[self._candidate_dict(probe, reason)],
                validated_at=validated_at,
            )
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
        if item.path.parent.name.casefold() in {"материалы", "materials", "source", "sources"}:
            rank += 8
        if expected_height and (f"{expected_height}p" in item.path.name.casefold() or f"x{expected_height}" in item.path.name.casefold()):
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
        if item.path.parent.name.casefold() in {"материалы", "materials", "source", "sources"}:
            rank += 8
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
        if instrumental:
            if item.path.suffix.casefold() != ".flac":
                return None
            if self._name_has_markers(item.path, self.INSTRUMENTAL_NEGATIVE_MARKERS):
                return None
        elif self._name_has_markers(item.path, self.ORIGINAL_AUDIO_NEGATIVE_MARKERS):
            return None
        if instrumental:
            rank = 115 if self._name_has_markers(item.path, self.WEAK_INSTRUMENTAL_MARKERS) else 92
        else:
            positive = self._name_has_markers(item.path, self.WEAK_AUDIO_MARKERS)
            downloaded_container = item.path.suffix.casefold() in {".m4a", ".webm", ".opus", ".ogg"}
            in_materials = item.path.parent.name.casefold() in {"материалы", "materials", "source", "sources"}
            rank = 112 if positive else (102 if downloaded_container and in_materials else 85)
        reason = (
            "Audio-only FLAC, duration and Instrumental naming match INSTRUMENTAL."
            if instrumental and rank >= 100 else
            "Audio-only duration and role-specific naming/container match ORIGINAL_AUDIO."
            if not instrumental and rank >= 100 else
            "Audio-only duration matches, but the filename is not role-specific."
        )
        return rank, float(item.bit_rate or item.size), reason

    def _cheap_score(self, path: Path, role: str) -> int:
        suffix = path.suffix.casefold()
        if role in {"maximum", "proxy"}:
            if suffix not in self.VIDEO_EXTENSIONS:
                return 0
            if role == "maximum" and self._name_has_markers(path, (*self.WEAK_PROXY_MARKERS, "short", "preview")):
                return 0
            if role == "proxy" and self._name_has_markers(path, self.WEAK_MAX_MARKERS) and not self._name_has_markers(path, self.WEAK_PROXY_MARKERS):
                return 0
            markers = self.WEAK_MAX_MARKERS if role == "maximum" else self.WEAK_PROXY_MARKERS
            in_materials = path.parent.name.casefold() in {"материалы", "materials", "source", "sources"}
            return 30 + (20 if self._name_has_markers(path, markers) else 0) + (15 if in_materials else 0)
        if role == "instrumental":
            if suffix != ".flac" or self._name_has_markers(path, self.INSTRUMENTAL_NEGATIVE_MARKERS):
                return 0
            return 40 + (40 if self._name_has_markers(path, self.WEAK_INSTRUMENTAL_MARKERS) else 0)
        if role == "audio":
            if suffix not in self.AUDIO_EXTENSIONS or self._name_has_markers(path, self.ORIGINAL_AUDIO_NEGATIVE_MARKERS):
                return 0
            return 30 + (40 if self._name_has_markers(path, self.WEAK_AUDIO_MARKERS) else 0)
        return 0

    @staticmethod
    def _cache_key(path: Path, size: int, mtime: float) -> str:
        return f"{str(path.resolve()).casefold()}|{size}|{mtime:.6f}"

    def _load_cache(self) -> Dict[str, Dict[str, Any]]:
        if not self.cache_path or not self.cache_path.is_file():
            return {}
        try:
            raw = json.loads(self.cache_path.read_text(encoding="utf-8"))
            return raw if isinstance(raw, dict) else {}
        except (OSError, UnicodeError, json.JSONDecodeError):
            return {}

    def _save_cache(self) -> None:
        if not self.cache_path:
            return
        if self.cache_path.parent.is_file():
            return
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.cache_path.with_suffix(self.cache_path.suffix + ".tmp")
        temporary.write_text(json.dumps(self._persistent_cache, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(str(temporary), str(self.cache_path))

    @staticmethod
    def _probe_from_dict(path: Path, data: Dict[str, Any]) -> LegacyMediaProbe:
        fields = {
            key: value for key, value in data.items()
            if key in LegacyMediaProbe.__dataclass_fields__ and key != "path"
        }
        return LegacyMediaProbe(path=path, **fields)

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
    def _name_has_markers(path: Path, markers: Iterable[str]) -> bool:
        name = path.stem.casefold()
        words = set(re.findall(r"[\wа-яё]+", name, flags=re.IGNORECASE))
        for marker in markers:
            normalized = marker.casefold()
            if " " in normalized or any(ch in normalized for ch in "[]"):
                if normalized in name:
                    return True
            elif normalized in words:
                return True
        return False

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
