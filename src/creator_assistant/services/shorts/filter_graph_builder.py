from __future__ import annotations

from pathlib import Path

from creator_assistant.domain.shorts.models import Candidate, SourceInfo
from creator_assistant.services.shorts.reframe.blur_background import BlurBackgroundReframe
from creator_assistant.services.shorts.reframe.center_crop import CenterCropReframe
from creator_assistant.services.shorts.reframe.solid_color import SolidColorReframe
from creator_assistant.services.shorts.overlay_layout import layout_title_text
from creator_assistant.services.shorts.font_resolver import drawtext_font_option, fonts_dir_option
from creator_assistant.services.shorts.subtitle_layout import resolved_style
from creator_assistant.services.shorts.text_alignment import HorizontalTextAlignment


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
            subtitle = f",subtitles=filename='{safe_name}'{fonts_dir_option()}:charenc=UTF-8"
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
            raw_title = str(branding.get("final_title_text", "")).strip()
            requested_size = max(24, min(180, int(branding.get("title_size", 78) or 78)))
            title_bold = bool(branding.get("title_bold", True))
            subtitle_style = resolved_style(candidate.subtitle_settings or {})
            title_font_family = (
                str(subtitle_style.get("font") or "Segoe UI")
                if bool(branding.get("use_subtitle_font_for_title", True))
                else str(branding.get("title_font_family") or subtitle_style.get("font") or "Segoe UI")
            )
            wrapped_title, size = layout_title_text(
                raw_title, requested_size, title_bold, 900, 2, font_family=title_font_family,
            )
            y = max(0, min(1800, int(branding.get("title_y", 180) or 180)))
            border = max(0, min(20, int(branding.get("title_outline", 4) or 4)))
            shadow = max(0, min(20, int(branding.get("title_shadow", 2) or 0)))
            fontcolor = str(branding.get("title_color", "#ffffff") or "#ffffff").replace("#", "0x")
            font_option = drawtext_font_option(title_font_family, title_bold)
            alignment = HorizontalTextAlignment.parse(branding.get("title_alignment", "center"))
            offset_x = max(-300, min(300, int(branding.get("title_offset_x", 0) or 0)))
            if alignment is HorizontalTextAlignment.LEFT:
                x_expr = f"max(90,min(w-text_w-90,90+{offset_x}))"
            elif alignment is HorizontalTextAlignment.RIGHT:
                x_expr = f"max(90,min(w-text_w-90,w-text_w-90+{offset_x}))"
            else:
                x_expr = f"max(90,min(w-text_w-90,(w-text_w)/2+{offset_x}))"
            # Render every wrapped line separately. A single multiline drawtext centers
            # the text box but left-aligns shorter lines inside it.
            for line_index, line in enumerate(wrapped_title.splitlines() or [wrapped_title]):
                text = _escape_drawtext(line)
                next_label = f"title{line_index}"
                line_y = y + line_index * (size + 8)
                filters.append(
                    f"[{current}]drawtext=text='{text}':fontcolor={fontcolor}:fontsize={size}:"
                    f"{font_option}:borderw={border}:bordercolor=black:shadowx={shadow}:shadowy={shadow}:"
                    f"shadowcolor=black@0.75:x='{x_expr}':y={line_y}[{next_label}]"
                )
                current = next_label
        if show_banner:
            scale = max(0.1, min(2.0, float(branding.get("banner_scale", 100) or 100) / 100))
            opacity = max(0.0, min(1.0, float(branding.get("banner_opacity", 100) or 100) / 100))
            safe = max(0, int(branding.get("safe_margin", 80) or 80))
            offset_x = int(branding.get("banner_offset_x", 0) or 0)
            offset_y = int(branding.get("banner_offset_y", 0) or 0)
            if "banner_y" in branding and "banner_offset_y" not in branding:
                offset_y = int(branding.get("banner_y", 1600) or 1600) - 1600
            max_width = max(120, int(1080 * 0.90) - safe * 2)
            filters.append(
                f"[1:v]scale=w='min(iw*{scale:.3f},{max_width})':h=-1,"
                f"format=rgba,colorchannelmixer=aa={opacity:.3f}[banner]"
            )
            x_expr = f"max({safe},min(W-w-{safe},(W-w)/2+{offset_x}))"
            y_expr = f"max({safe},min(H-h-{safe},H-h-{safe}+{offset_y}))"
            filters.append(f"[{current}][banner]overlay=x='{x_expr}':y='{y_expr}':format=auto[overlayed]")
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
