from __future__ import annotations

from pathlib import Path

from creator_assistant.domain.shorts.models import Candidate, SourceInfo
from creator_assistant.services.shorts.reframe.blur_background import BlurBackgroundReframe
from creator_assistant.services.shorts.reframe.center_crop import CenterCropReframe
from creator_assistant.services.shorts.reframe.solid_color import SolidColorReframe


class ShortsFilterGraphBuilder:
    def build(self, candidate: Candidate, source: SourceInfo, subtitle_file: str = "", input_clipped: bool = False, has_channel_banner: bool = False) -> str:
        layout = candidate.layout_settings or {}
        mode = layout.get("mode", "center_crop")
        if mode == "blur_background":
            reframe = BlurBackgroundReframe(int(layout.get("foreground_scale", 100)))
        elif mode == "solid_color":
            reframe = SolidColorReframe(int(layout.get("foreground_scale", 100)), str(layout.get("background_color", "black")))
        else:
            reframe = CenterCropReframe(int(layout.get("crop_center", 50)))
        width, height = source.width, source.height
        if abs(source.rotation) % 180 == 90:
            width, height = height, width
        video = reframe.video_filter(width, height, source.fps)
        tone_map = ""
        if source.dynamic_range == "HDR":
            tone_map = ",zscale=t=linear:npl=100,tonemap=hable,zscale=p=bt709:t=bt709:m=bt709:r=tv"
        subtitle = ""
        if subtitle_file:
            safe_name = Path(subtitle_file).name.replace("'", r"\'").replace(":", r"\:")
            subtitle = f",subtitles=filename='{safe_name}':charenc=UTF-8"
        if input_clipped:
            video_prefix = "[0:v:0]setpts=PTS-STARTPTS,"
            audio_chain = "[0:a:0]asetpts=PTS-STARTPTS[a]"
        else:
            video_prefix = f"[0:v:0]trim=start={candidate.start:.3f}:end={candidate.end:.3f},setpts=PTS-STARTPTS,"
            audio_chain = f"[0:a:0]atrim=start={candidate.start:.3f}:end={candidate.end:.3f},asetpts=PTS-STARTPTS[a]"
        branding = candidate.branding_settings or {}
        show_title = bool(branding.get("show_title", False)) and str(branding.get("final_title_text", "")).strip()
        show_banner = bool(branding.get("show_channel_card", False)) and has_channel_banner
        if not show_title and not show_banner:
            return f"{video_prefix}{video}{tone_map}{subtitle}[v];{audio_chain}"

        filters = [f"{video_prefix}{video}{tone_map}{subtitle}[base]"]
        current = "base"
        if show_title:
            text = _escape_drawtext(str(branding.get("final_title_text", "")).strip())
            size = max(24, min(180, int(branding.get("title_size", 78) or 78)))
            y = max(0, min(1800, int(branding.get("title_y", 180) or 180)))
            border = max(0, min(20, int(branding.get("title_outline", 4) or 4)))
            fontcolor = str(branding.get("title_color", "#ffffff") or "#ffffff").replace("#", "0x")
            next_label = "title"
            filters.append(
                f"[{current}]drawtext=text='{text}':fontcolor={fontcolor}:fontsize={size}:"
                f"font='Arial':borderw={border}:bordercolor=black:x=(w-text_w)/2:y={y}:"
                f"line_spacing=8[{next_label}]"
            )
            current = next_label
        if show_banner:
            scale = max(0.1, min(2.0, float(branding.get("banner_scale", 100) or 100) / 100))
            opacity = max(0.0, min(1.0, float(branding.get("banner_opacity", 100) or 100) / 100))
            x = int(branding.get("banner_x", 50) or 50)
            y = int(branding.get("banner_y", 1600) or 1600)
            filters.append(f"[1:v]scale=iw*{scale:.3f}:-1,format=rgba,colorchannelmixer=aa={opacity:.3f}[banner]")
            filters.append(f"[{current}][banner]overlay=x={x}:y={y}:format=auto[overlayed]")
            current = "overlayed"
        filters.append(f"[{current}]null[v]")
        return ";".join(filters + [audio_chain])


def _escape_drawtext(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace(":", r"\:")
        .replace("'", r"\'")
        .replace("%", r"\%")
        .replace(",", r"\,")
    )
