from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtGui import QFont, QFontMetricsF, QGuiApplication

from creator_assistant.services.shorts.font_resolver import resolved_qfont
from creator_assistant.services.shorts.text_alignment import HorizontalTextAlignment, aligned_left


FRAME_WIDTH = 1080
FRAME_HEIGHT = 1920
MAX_TEXT_WIDTH = round(FRAME_WIDTH * 0.82)
MIN_FONT_SIZE = 44
POSITION_Y = {"upper": 420, "center": 930, "lower": 1485}
STYLE_PRESETS = {
    "clean": {
        "font": "Segoe UI", "size": 58, "outline": 3, "shadow": 1,
        "primary": "&H00FFFFFF", "secondary": "&H000000FF", "spacing": 0,
        "weight": 700,
    },
    "large": {
        "font": "Segoe UI", "size": 80, "outline": 5, "shadow": 2,
        "primary": "&H00FFFFFF", "secondary": "&H000000FF", "spacing": -1,
        "weight": 800,
    },
    "gaming": {
        "font": "Arial Black", "size": 70, "outline": 5, "shadow": 3,
        "primary": "&H0000D7FF", "secondary": "&H00FFFFFF", "spacing": 1,
        "weight": 900,
    },
}


@dataclass(frozen=True)
class SubtitleLayout:
    text: str
    lines: list[str]
    font_name: str
    font_size: int
    outline: int
    shadow: int
    x: int
    y: int
    width: int
    height: int
    safe_margin: int
    max_width: int
    vertical_offset: int
    clamped_vertical_offset: int
    primary: str
    background: bool
    alignment: HorizontalTextAlignment
    horizontal_offset: int
    clamped_horizontal_offset: int
    line_anchor_mode: str
    first_line_baseline: int
    second_line_baseline: int
    reserved_y: int
    reserved_height: int
    applied_gap: int


class _ApproximateFontMetrics:
    def __init__(self, size: int) -> None:
        self.size = size

    def horizontalAdvance(self, text: str) -> float:  # noqa: N802 - mirrors Qt API
        return sum(self.size * (0.34 if character.isspace() else 0.62) for character in text)

    def height(self) -> float:
        return self.size * 1.18

    def ascent(self) -> float:
        return self.size * 0.93


def resolved_style(settings: dict) -> dict:
    name = str(settings.get("style", "clean"))
    preset = dict(STYLE_PRESETS.get(name, STYLE_PRESETS["clean"]))
    if str(settings.get("font_family", "")).strip():
        preset["font"] = str(settings.get("font_family")).strip()
    preset["size"] = int(settings.get("size", preset["size"]))
    preset["outline"] = int(settings.get("outline", preset["outline"]))
    preset["shadow"] = int(settings.get("shadow", preset["shadow"]))
    preset["name"] = name
    return preset


def font_metrics(font_name: str, size: int, weight: int = 700):
    if QGuiApplication.instance() is None:
        return _ApproximateFontMetrics(size)
    font = resolved_qfont(font_name, bold=weight >= 600, pixel_size=size)
    font.setWeight(QFont.Weight(max(100, min(900, int(weight)))))
    return QFontMetricsF(font)


def split_oversized_word(word: str, metrics, width: float) -> list[str]:
    chunks: list[str] = []
    current = ""
    for character in word:
        proposed = current + character
        if current and metrics.horizontalAdvance(proposed) > width:
            chunks.append(current)
            current = character
        else:
            current = proposed
    if current:
        chunks.append(current)
    return chunks


def pixel_pages(text: str, font_name: str, size: int, outline: int, max_lines: int = 2, max_width: int = MAX_TEXT_WIDTH, weight: int = 700) -> list[str]:
    available = min(MAX_TEXT_WIDTH, max_width) - 2 * (outline + 4)
    metrics = font_metrics(font_name, size, weight)
    tokens: list[str] = []
    for word in text.replace("\n", " ").split():
        if metrics.horizontalAdvance(word) <= available:
            tokens.append(word)
        else:
            tokens.extend(split_oversized_word(word, metrics, available))
    if not tokens:
        return []
    pages: list[str] = []
    lines: list[str] = []
    current = ""
    line_limit = max(1, min(2, int(max_lines)))
    for token in tokens:
        proposed = f"{current} {token}".strip()
        if not current or metrics.horizontalAdvance(proposed) <= available:
            current = proposed
            continue
        lines.append(current)
        current = token
        if len(lines) == line_limit:
            pages.append("\n".join(lines))
            lines = []
    if current:
        lines.append(current)
    if lines:
        pages.append("\n".join(lines))
    return pages


class SubtitleLayoutCalculator:
    def calculate(self, text: str, settings: dict) -> SubtitleLayout:
        style = resolved_style(settings)
        safe_margin = max(80, int(settings.get("safe_margin", 90)))
        max_width = min(MAX_TEXT_WIDTH, FRAME_WIDTH - 2 * safe_margin)
        max_lines = max(1, min(2, int(settings.get("lines", 2))))
        size = int(style["size"])
        minimum = int(settings.get("minimum_size", MIN_FONT_SIZE))
        words = text.replace("\n", " ").split()
        while size > minimum:
            metrics = font_metrics(style["font"], size, int(style.get("weight", 700)))
            available = max_width - 2 * (int(style["outline"]) + 4)
            if not words or max(metrics.horizontalAdvance(word) for word in words) <= available:
                break
            size -= 2
        pages = pixel_pages(
            text, style["font"], size, int(style["outline"]), max_lines, max_width, int(style.get("weight", 700))
        )
        metrics = font_metrics(style["font"], size, int(style.get("weight", 700)))
        explicit_lines = [line.strip() for line in text.splitlines() if line.strip()]
        available = max_width - 2 * (int(style["outline"]) + 4)
        if (
            1 < len(explicit_lines) <= max_lines
            and all(metrics.horizontalAdvance(line) <= available for line in explicit_lines)
        ):
            lines = explicit_lines
        else:
            lines = pages[0].splitlines() if pages else [text.strip()]
        line_height = max(1, round(metrics.height()))
        decoration_height = 2 * int(style["outline"]) + 2 * int(style["shadow"]) + 10
        height = round(line_height * len(lines) + decoration_height)
        max_lines = max(1, min(2, int(settings.get("lines", 2))))
        anchor_mode = str(settings.get("line_anchor_mode", "first_line_fixed") or "first_line_fixed")
        if anchor_mode not in {"first_line_fixed", "banner_bottom", "block_center"}:
            anchor_mode = "first_line_fixed"
        reserved_height = round(line_height * max_lines + decoration_height)
        text_width = max((metrics.horizontalAdvance(line) for line in lines), default=0)
        width = min(max_width, max(1, round(text_width + 2 * (int(style["outline"]) + int(style["shadow"]) + 6))))
        alignment = HorizontalTextAlignment.parse(settings.get("alignment", "center"))
        requested_horizontal_offset = max(-300, min(300, int(settings.get("horizontal_offset", 0))))
        left = aligned_left(width, alignment, requested_horizontal_offset, safe_margin=safe_margin)
        if alignment is HorizontalTextAlignment.LEFT:
            x = left
            base_x = safe_margin
        elif alignment is HorizontalTextAlignment.RIGHT:
            x = left + width
            base_x = FRAME_WIDTH - safe_margin
        else:
            x = left + width / 2
            base_x = FRAME_WIDTH / 2
        requested_offset = int(settings.get("vertical_offset", 0))
        base_y = POSITION_Y.get(str(settings.get("position", "lower")), POSITION_Y["lower"])
        constraint_height = height if anchor_mode == "banner_bottom" else reserved_height
        min_constraint_y = safe_margin + constraint_height // 2
        max_bottom = min(
            FRAME_HEIGHT - safe_margin,
            int(settings.get("maximum_bottom", FRAME_HEIGHT - safe_margin) or FRAME_HEIGHT - safe_margin),
        )
        max_constraint_y = max(min_constraint_y, max_bottom - constraint_height // 2)
        constraint_y = max(min_constraint_y, min(max_constraint_y, base_y + requested_offset))
        if anchor_mode == "first_line_fixed":
            reserved_top = constraint_y - reserved_height // 2
            y = reserved_top + height // 2
            reserved_y = constraint_y
        elif anchor_mode == "block_center":
            y = constraint_y
            reserved_y = constraint_y
        else:
            y = constraint_y
            reserved_y = y - height // 2 + reserved_height // 2
        top = y - height // 2
        ascent = round(metrics.ascent()) if hasattr(metrics, "ascent") else round(size * 0.93)
        first_line_baseline = top + int(style["outline"]) + int(style["shadow"]) + 5 + ascent
        second_line_baseline = first_line_baseline + line_height
        applied_gap = max(0, max_bottom - (top + height)) if "maximum_bottom" in settings else 0
        return SubtitleLayout(
            text="\n".join(lines),
            lines=lines,
            font_name=str(style["font"]),
            font_size=size,
            outline=int(style["outline"]),
            shadow=int(style["shadow"]),
            x=round(x),
            y=round(y),
            width=width,
            height=height,
            safe_margin=safe_margin,
            max_width=max_width,
            vertical_offset=requested_offset,
            clamped_vertical_offset=round(constraint_y - base_y),
            primary=str(style.get("primary", "&H00FFFFFF")),
            background=bool(settings.get("background", False)),
            alignment=alignment,
            horizontal_offset=requested_horizontal_offset,
            clamped_horizontal_offset=round(x - base_x),
            line_anchor_mode=anchor_mode,
            first_line_baseline=first_line_baseline,
            second_line_baseline=second_line_baseline,
            reserved_y=reserved_y,
            reserved_height=reserved_height,
            applied_gap=applied_gap,
        )
