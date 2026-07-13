from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class VideoFormat:
    format_id: str
    ext: str
    width: Optional[int] = None
    height: Optional[int] = None
    fps: Optional[float] = None
    vcodec: str = "none"
    acodec: str = "none"
    dynamic_range: str = "SDR"
    color_transfer: str = ""
    tbr: Optional[float] = None
    vbr: Optional[float] = None
    abr: Optional[float] = None
    filesize: Optional[int] = None
    filesize_approx: Optional[int] = None
    protocol: str = ""

    @property
    def size(self) -> Optional[int]:
        return self.filesize or self.filesize_approx

    @property
    def has_video(self) -> bool:
        return bool(self.vcodec and self.vcodec != "none")

    @property
    def has_audio(self) -> bool:
        return bool(self.acodec and self.acodec != "none")


@dataclass(frozen=True)
class ThumbnailInfo:
    url: str
    width: int = 0
    height: int = 0
    preference: int = 0


@dataclass
class VideoMetadata:
    video_id: str
    title: str
    duration: Optional[float]
    webpage_url: str
    formats: List[VideoFormat] = field(default_factory=list)
    thumbnails: List[ThumbnailInfo] = field(default_factory=list)
    channel_id: str = ""
    channel: str = ""
    channel_url: str = ""
    uploader_id: str = ""
    uploader: str = ""
    uploader_url: str = ""
    channel_handle: str = ""

    @property
    def best_thumbnail(self) -> Optional[ThumbnailInfo]:
        if not self.thumbnails:
            return None
        return max(
            self.thumbnails,
            key=lambda item: (item.width * item.height, item.preference),
        )


@dataclass(frozen=True)
class FormatPlan:
    maximum_video: VideoFormat
    maximum_audio: VideoFormat
    maximum_container: str
    proxy_video: VideoFormat
    proxy_audio: VideoFormat
    proxy_requires_transcode: bool
    best_audio: VideoFormat

    @property
    def maximum_selector(self) -> str:
        if self.maximum_video.has_audio:
            return self.maximum_video.format_id
        return f"{self.maximum_video.format_id}+{self.maximum_audio.format_id}"

    @property
    def proxy_selector(self) -> str:
        if self.proxy_video.has_audio:
            return self.proxy_video.format_id
        return f"{self.proxy_video.format_id}+{self.proxy_audio.format_id}"


@dataclass
class ProjectOptions:
    download_maximum: bool = True
    create_proxy: bool = True
    download_audio: bool = True
    create_instrumental: bool = True
    create_reaper_project: bool = True
    dry_run: bool = False
    reaper_proxy_height: int = 720
    temp_root: str = ""
    job_id: str = ""


@dataclass(frozen=True)
class ProjectPaths:
    root: Path
    materials: Path
    base_name: str


@dataclass
class ProgressInfo:
    stage: str
    message: str
    percent: Optional[float] = None
    speed: str = ""
    downloaded: str = ""
    total: str = ""
    eta: str = ""
    stage_id: str = ""
    stage_index: int = 0
    stage_count: int = 0
    overall_percent: Optional[float] = None
    downloaded_bytes: Optional[int] = None
    total_bytes: Optional[int] = None
    speed_bytes_per_second: Optional[float] = None
    eta_seconds: Optional[int] = None
    substage_name: str = ""
    state: str = "running"

    @property
    def stage_percent(self) -> Optional[float]:
        return self.percent


@dataclass
class DependencyInfo:
    key: str
    name: str
    status: str
    path: str = ""
    version: str = ""
    details: str = ""
    source: str = ""


@dataclass
class ProjectResult:
    project_path: Optional[Path]
    plan_lines: List[str]
    files: Dict[str, Path] = field(default_factory=dict)
    resumed: bool = False

    def serializable_files(self) -> Dict[str, str]:
        return {key: str(value) for key, value in self.files.items()}


def format_from_dict(data: Dict[str, Any]) -> VideoFormat:
    def number(name: str, integer: bool = False) -> Any:
        value = data.get(name)
        if value is None:
            return None
        try:
            return int(value) if integer else float(value)
        except (TypeError, ValueError):
            return None

    return VideoFormat(
        format_id=str(data.get("format_id", "")),
        ext=str(data.get("ext", "")),
        width=number("width", True),
        height=number("height", True),
        fps=number("fps"),
        vcodec=str(data.get("vcodec") or "none"),
        acodec=str(data.get("acodec") or "none"),
        dynamic_range=str(data.get("dynamic_range") or "SDR"),
        color_transfer=str(data.get("color_transfer") or ""),
        tbr=number("tbr"),
        vbr=number("vbr"),
        abr=number("abr"),
        filesize=number("filesize", True),
        filesize_approx=number("filesize_approx", True),
        protocol=str(data.get("protocol") or ""),
    )
