from __future__ import annotations

from pathlib import Path

from creator_assistant.domain.shorts.models import Candidate, SourceInfo
from creator_assistant.services.shorts.reframe.blur_background import BlurBackgroundReframe
from creator_assistant.services.shorts.reframe.center_crop import CenterCropReframe


class ShortsFilterGraphBuilder:
    def build(self, candidate: Candidate, source: SourceInfo, subtitle_file: str = "") -> str:
        layout = candidate.layout_settings or {}
        if layout.get("mode", "center_crop") == "blur_background":
            reframe = BlurBackgroundReframe(int(layout.get("foreground_scale", 100)))
        else:
            reframe = CenterCropReframe(int(layout.get("crop_center", 50)))
        width, height = source.width, source.height
        if abs(source.rotation) % 180 == 90:
            width, height = height, width
        video = reframe.video_filter(width, height)
        tone_map = ""
        if source.dynamic_range == "HDR":
            tone_map = ",zscale=t=linear:npl=100,tonemap=hable,zscale=p=bt709:t=bt709:m=bt709:r=tv"
        subtitle = ""
        if subtitle_file:
            safe_name = Path(subtitle_file).name.replace("'", r"\'").replace(":", r"\:")
            subtitle = f",subtitles=filename='{safe_name}':charenc=UTF-8"
        return (
            f"[0:v:0]trim=start={candidate.start:.3f}:end={candidate.end:.3f},setpts=PTS-STARTPTS,{video}{tone_map}{subtitle}[v];"
            f"[0:a:0]atrim=start={candidate.start:.3f}:end={candidate.end:.3f},asetpts=PTS-STARTPTS[a]"
        )
