from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtGui import QFontMetrics, QGuiApplication

from creator_assistant.services.shorts.font_resolver import resolved_qfont
from creator_assistant.services.shorts.subtitle_layout import SubtitleLayout, SubtitleLayoutCalculator


@dataclass(frozen=True)
class Rect:
    x: int
    y: int
    width: int
    height: int


class OverlayLayoutCalculator:
    width = 1080
    height = 1920

    def banner_rect(self, image_width: int, image_height: int, settings: dict) -> Rect:
        image_width = max(1, int(image_width or 1))
        image_height = max(1, int(image_height or 1))
        safe = max(0, int(settings.get("safe_margin", 80) or 80))
        scale = max(10, min(300, int(settings.get("banner_scale", 100) or 100))) / 100
        max_width = int(self.width * 0.90) - safe * 2
        max_width = max(120, max_width)
        target_width = min(int(image_width * scale), max_width)
        target_height = max(1, round(image_height * target_width / image_width))
        # banner_x in pre-offset manifests was an absolute canvas coordinate, not an offset.
        offset_x = int(settings.get("banner_offset_x", 0) or 0)
        offset_y = int(settings.get("banner_offset_y", 0) or 0)
        if "banner_y" in settings and "banner_offset_y" not in settings:
            # Migrate old absolute Y around the previous default 1600 into offset semantics.
            offset_y = int(settings.get("banner_y", 1600) or 1600) - 1600
        x = round((self.width - target_width) / 2 + offset_x)
        y = round(self.height - safe - target_height + offset_y)
        x = max(safe, min(self.width - safe - target_width, x))
        y = max(safe, min(self.height - safe - target_height, y))
        return Rect(x, y, target_width, target_height)

    def subtitle_layout(
        self,
        text: str,
        subtitle_settings: dict,
        branding_settings: dict | None = None,
        banner_size: tuple[int, int] | None = None,
    ) -> SubtitleLayout:
        """Calculate the one canonical subtitle geometry used by Qt and ASS.

        Coordinates are always expressed in the final 1080x1920 canvas.  Preview
        quality only scales this result and therefore cannot change placement.
        """
        settings = dict(subtitle_settings or {})
        branding = dict(branding_settings or {})
        if (
            bool(settings.get("auto_above_banner", True))
            and bool(branding.get("show_channel_card", False))
            and banner_size
        ):
            banner = self.banner_rect(banner_size[0], banner_size[1], branding)
            settings["maximum_bottom"] = banner.y - max(20, int(settings.get("banner_gap", 32) or 32))
        return SubtitleLayoutCalculator().calculate(text, settings)


def layout_title_text(
    text: str,
    requested_size: int,
    bold: bool = True,
    max_width: int = 900,
    max_lines: int = 2,
    minimum_size: int = 32,
    font_family: str = "Segoe UI",
) -> tuple[str, int]:
    """Wrap a title by the actual font pixel width and shrink only when two lines cannot fit."""
    cleaned = " ".join(str(text or "").replace("\n", " ").split())
    requested_size = max(minimum_size, int(requested_size or 78))
    if not cleaned:
        return "", requested_size
    for size in range(requested_size, minimum_size - 1, -2):
        measure = _title_measurer(size, bold, font_family)
        lines = _pixel_wrap(cleaned, measure, max_width)
        if len(lines) <= max_lines:
            return "\n".join(lines), size
    # At the minimum size, preserve every character; hooks up to 60 chars fit this path in two lines.
    return "\n".join(_pixel_wrap(cleaned, _title_measurer(minimum_size, bold, font_family), max_width)[:max_lines]), minimum_size


def _title_measurer(size: int, bold: bool, font_family: str):
    if QGuiApplication.instance() is not None:
        font = resolved_qfont(font_family or "Segoe UI", bold=bold, pixel_size=size)
        metrics = QFontMetrics(font)
        return metrics.horizontalAdvance

    def approximate(value: str) -> int:
        # Headless render/benchmark fallback equivalent to Segoe UI's conservative glyph widths.
        units = sum(0.34 if char.isspace() else 0.76 if ord(char) > 127 else 0.64 for char in value)
        return round(units * size * (1.04 if bold else 1.0))

    return approximate


def _pixel_wrap(text: str, measure, max_width: int) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        pieces = _split_wide_word(word, measure, max_width)
        for piece in pieces:
            proposal = f"{current} {piece}".strip()
            if current and measure(proposal) > max_width:
                lines.append(current)
                current = piece
            else:
                current = proposal
    if current:
        lines.append(current)
    return lines


def _split_wide_word(word: str, measure, max_width: int) -> list[str]:
    if measure(word) <= max_width:
        return [word]
    result: list[str] = []
    part = ""
    for char in word:
        if part and measure(part + char) > max_width:
            result.append(part)
            part = char
        else:
            part += char
    if part:
        result.append(part)
    return result
