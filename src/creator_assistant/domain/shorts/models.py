from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class SourceInfo:
    path: str
    name: str
    size: int
    mtime: float
    duration: float
    width: int
    height: int
    fps: float
    video_codec: str
    audio_codec: str
    audio_channels: int
    sample_rate: int
    dynamic_range: str = "SDR"
    rotation: int = 0
    fingerprint: str = ""

    @classmethod
    def from_dict(cls, value: Dict[str, Any]) -> "SourceInfo":
        return cls(**value)


@dataclass
class TranscriptWord:
    start: float
    end: float
    word: str
    probability: Optional[float] = None


@dataclass
class TranscriptSegment:
    id: int
    start: float
    end: float
    text: str
    confidence: Optional[float] = None
    words: List[TranscriptWord] = field(default_factory=list)


@dataclass
class Transcript:
    language: str
    duration: float
    text: str
    segments: List[TranscriptSegment] = field(default_factory=list)
    backend: str = ""
    model: str = ""


@dataclass
class Scene:
    start: float
    end: float
    score: float = 0.0
    thumbnail: str = ""


@dataclass
class AudioFeatures:
    speech_intervals: List[List[float]] = field(default_factory=list)
    pauses: List[List[float]] = field(default_factory=list)
    peaks: List[Dict[str, float]] = field(default_factory=list)
    mean_loudness: float = -99.0


@dataclass
class Candidate:
    id: str
    start: float
    end: float
    score: float
    text: str
    reasons: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    status: str = "review"
    selected_for_render: bool = False
    alternatives: List[List[float]] = field(default_factory=list)
    thumbnail: str = ""
    title: str = ""
    layout_settings: Dict[str, Any] = field(default_factory=dict)
    subtitle_settings: Dict[str, Any] = field(default_factory=dict)
    branding_settings: Dict[str, Any] = field(default_factory=dict)
    heuristic_score: float = 0.0
    semantic_score: Optional[float] = None
    final_score: float = 0.0
    ai_moment_type: str = ""
    ai_verdict: str = ""
    ai_reason: str = ""
    ai_weaknesses: List[str] = field(default_factory=list)
    selection_source: str = "heuristic"
    ai_model: str = ""
    ai_mode: str = ""
    selected_boundary_variant_id: str = "main"

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


@dataclass
class SubtitleCue:
    start: float
    end: float
    text: str


@dataclass
class LayoutSettings:
    mode: str = "center_crop"
    crop_center: int = 50
    foreground_scale: int = 100
    subtitle_style: str = "clean"
    subtitle_position: str = "lower"
    subtitle_size: int = 58


@dataclass
class RenderJob:
    id: str
    candidate_id: str
    output_path: str
    status: str = "waiting"
    progress: Optional[float] = None
    error: str = ""
    speed: str = ""


@dataclass
class ShortsManifest:
    schema_version: int
    shorts_project_id: str
    source_path: str
    source_fingerprint: str
    source_size: int
    source_mtime: float
    source_duration: float
    transcription_backend: str = ""
    whisper_model: str = ""
    analysis_settings: Dict[str, Any] = field(default_factory=dict)
    ai_analysis: Dict[str, Any] = field(default_factory=dict)
    candidates: List[Dict[str, Any]] = field(default_factory=list)
    approved_clips: List[Dict[str, Any]] = field(default_factory=list)
    render_jobs: List[Dict[str, Any]] = field(default_factory=list)
    completed_stages: List[str] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Dict[str, Any]) -> "ShortsManifest":
        allowed = cls.__dataclass_fields__.keys()
        return cls(**{key: item for key, item in value.items() if key in allowed})
