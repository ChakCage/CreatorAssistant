from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtGui import QFont, QFontMetricsF, QGuiApplication


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


class _ApproximateFontMetrics:
    def __init__(self, size: int) -> None:
        self.size = size

    def horizontalAdvance(self, text: str) -> float:  # noqa: N802 - mirrors Qt API
        return sum(self.size * (0.34 if character.isspace() else 0.62) for character in text)

    def height(self) -> float:
        return self.size * 1.18


def resolved_style(settings: dict) -> dict:
    name = str(settings.get("style", "clean"))
    preset = dict(STYLE_PRESETS.get(name, STYLE_PRESETS["clean"]))
    preset["size"] = int(settings.get("size", preset["size"]))
    preset["outline"] = int(settings.get("outline", preset["outline"]))
    preset["shadow"] = int(settings.get("shadow", preset["shadow"]))
    preset["name"] = name
    return preset


def font_metrics(font_name: str, size: int, weight: int = 700):
    if QGuiApplication.instance() is None:
        return _ApproximateFontMetrics(size)
    font = QFont(font_name)
    font.setPixelSize(size)
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
        lines = pages[0].splitlines() if pages else [text.strip()]
        metrics = font_metrics(style["font"], size, int(style.get("weight", 700)))
        line_height = max(1, round(metrics.height()))
        height = round(line_height * len(lines) + 2 * int(style["outline"]) + 2 * int(style["shadow"]) + 10)
        requested_offset = int(settings.get("vertical_offset", 0))
        base_y = POSITION_Y.get(str(settings.get("position", "lower")), POSITION_Y["lower"])
        min_y = safe_margin + height // 2
        max_y = FRAME_HEIGHT - safe_margin - height // 2
        y = max(min_y, min(max_y, base_y + requested_offset))
        return SubtitleLayout(
            text="\n".join(lines),
            lines=lines,
            font_name=str(style["font"]),
            font_size=size,
            outline=int(style["outline"]),
            shadow=int(style["shadow"]),
            x=FRAME_WIDTH // 2,
            y=round(y),
            width=max_width,
            height=height,
            safe_margin=safe_margin,
            max_width=max_width,
            vertical_offset=requested_offset,
            clamped_vertical_offset=round(y - base_y),
            primary=str(style.get("primary", "&H00FFFFFF")),
            background=bool(settings.get("background", False)),
        )
