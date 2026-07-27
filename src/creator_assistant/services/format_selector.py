from __future__ import annotations

from typing import Iterable, List

from creator_assistant.domain.errors import ValidationError
from creator_assistant.domain.models import FormatPlan, ProxyFpsPolicy, VideoFormat


HDR_MARKERS = ("hdr", "hlg", "dolby", "dv", "smpte2084", "arib-std-b67", "pq")


def is_hdr(fmt: VideoFormat) -> bool:
    haystack = " ".join((fmt.dynamic_range, fmt.color_transfer)).casefold()
    return any(marker in haystack for marker in HDR_MARKERS)


def _video_score(fmt: VideoFormat) -> tuple[float, ...]:
    codec = fmt.vcodec.casefold()
    codec_quality = 4 if codec.startswith("av01") else 3 if codec.startswith("vp9") else 2
    return (
        float(fmt.height or 0),
        float(fmt.fps or 0),
        float(fmt.vbr or fmt.tbr or 0),
        float(codec_quality),
    )


def select_maximum_sdr(formats: Iterable[VideoFormat]) -> VideoFormat:
    candidates = [fmt for fmt in formats if fmt.has_video and fmt.height and not is_hdr(fmt)]
    if not candidates:
        raise ValidationError("Для этого видео не найден доступный SDR-видеоформат.")
    return max(candidates, key=_video_score)


def select_audio(formats: Iterable[VideoFormat], prefer_aac: bool = False) -> VideoFormat:
    candidates = [fmt for fmt in formats if fmt.has_audio and not fmt.has_video]
    if not candidates:
        # Редкий случай: только совмещённые форматы.
        candidates = [fmt for fmt in formats if fmt.has_audio]
    if not candidates:
        raise ValidationError("Для этого видео не найден аудиоформат.")

    def score(fmt: VideoFormat) -> tuple[float, ...]:
        codec = fmt.acodec.casefold()
        aac = codec.startswith(("mp4a", "aac"))
        return (float(aac if prefer_aac else 0), float(fmt.abr or fmt.tbr or 0), float(fmt.size or 0))

    return max(candidates, key=score)


def select_proxy_video(
    formats: Iterable[VideoFormat],
    maximum_height: int = 720,
    fps_policy: ProxyFpsPolicy = ProxyFpsPolicy.PRESERVE,
) -> VideoFormat:
    candidates = [
        fmt
        for fmt in formats
        if fmt.has_video and fmt.height and fmt.height <= maximum_height and not is_hdr(fmt)
    ]
    if not candidates:
        raise ValidationError(f"Не найден SDR-видеоформат с разрешением не выше {maximum_height}p.")

    def score(fmt: VideoFormat) -> tuple[float, ...]:
        codec = fmt.vcodec.casefold()
        h264 = codec.startswith(("avc1", "h264"))
        compatible_audio = not fmt.has_audio or fmt.acodec.casefold().startswith(("mp4a", "aac"))
        fps_compatible = fps_policy == ProxyFpsPolicy.PRESERVE or (fmt.fps or 0) <= 30.05
        return (
            float(fmt.height or 0),
            float(fps_compatible),
            float(h264 and compatible_audio),
            float(fmt.fps or 0),
            float(fmt.vbr or fmt.tbr or 0),
        )

    return max(candidates, key=score)


def choose_container(video: VideoFormat, audio: VideoFormat) -> str:
    return "mp4"


def is_reaper_compatible(video: VideoFormat, audio: VideoFormat) -> bool:
    video_codec = video.vcodec.casefold()
    audio_codec = (video.acodec if video.has_audio else audio.acodec).casefold()
    return video_codec.startswith(("avc1", "h264")) and audio_codec.startswith(("mp4a", "aac"))


def build_format_plan(
    formats: List[VideoFormat],
    proxy_height: int = 720,
    fps_policy: ProxyFpsPolicy = ProxyFpsPolicy.PRESERVE,
) -> FormatPlan:
    maximum_video = select_maximum_sdr(formats)
    maximum_audio = select_audio(formats)
    proxy_height = proxy_height if proxy_height in {480, 720, 1080} else 720
    proxy_video = select_proxy_video(formats, proxy_height, fps_policy)
    proxy_audio = select_audio(formats, prefer_aac=True)
    return FormatPlan(
        maximum_video=maximum_video,
        maximum_audio=maximum_audio,
        maximum_container=choose_container(maximum_video, maximum_audio),
        proxy_video=proxy_video,
        proxy_audio=proxy_audio,
        proxy_requires_transcode=(
            not is_reaper_compatible(proxy_video, proxy_audio)
            or fps_policy == ProxyFpsPolicy.EXACT_30
            or (
                fps_policy == ProxyFpsPolicy.CAP_30
                and bool(proxy_video.fps)
                and float(proxy_video.fps) > 30.05
            )
        ),
        best_audio=maximum_audio,
    )
