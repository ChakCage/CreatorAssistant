from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

from PySide6.QtGui import QFont, QFontMetricsF, QGuiApplication

from creator_assistant.domain.shorts.models import Candidate, SubtitleCue, Transcript
from creator_assistant.services.shorts.transcription_service import srt_timestamp


FRAME_WIDTH = 1080
MAX_TEXT_WIDTH = round(FRAME_WIDTH * 0.82)
MIN_FONT_SIZE = 44
POSITION_Y = {"upper": 420, "center": 930, "lower": 1450}
STYLE_PRESETS = {
    "clean": {
        "font": "Segoe UI", "size": 58, "outline": 3, "shadow": 1,
        "primary": "&H00FFFFFF", "secondary": "&H000000FF", "spacing": 0,
    },
    "large": {
        "font": "Segoe UI", "size": 72, "outline": 5, "shadow": 2,
        "primary": "&H00FFFFFF", "secondary": "&H000000FF", "spacing": -1,
    },
    "gaming": {
        "font": "Arial Black", "size": 68, "outline": 6, "shadow": 4,
        "primary": "&H0000D7FF", "secondary": "&H00FFFFFF", "spacing": 1,
    },
}


def wrap_subtitle(text: str, maximum: int = 36, lines: int = 2) -> str:
    """Legacy character wrapping used while generating editable draft cues."""
    words = text.strip().split()
    if not words:
        return ""
    result = [""]
    for word in words:
        proposed = (result[-1] + " " + word).strip()
        if len(proposed) <= maximum or not result[-1]:
            result[-1] = proposed
        elif len(result) < lines:
            result.append(word)
        else:
            result[-1] += " " + word
    return "\n".join(result)


def ass_timestamp(seconds: float) -> str:
    centiseconds = max(0, round(seconds * 100))
    hours, remainder = divmod(centiseconds, 360000)
    minutes, remainder = divmod(remainder, 6000)
    secs, cents = divmod(remainder, 100)
    return f"{hours}:{minutes:02d}:{secs:02d}.{cents:02d}"


def resolved_style(settings: dict) -> dict:
    name = str(settings.get("style", "clean"))
    preset = dict(STYLE_PRESETS.get(name, STYLE_PRESETS["clean"]))
    # A user may tune the active preset, but choosing a preset always establishes
    # a genuinely distinct minimum size/outline/shadow.
    preset["size"] = max(int(settings.get("size", preset["size"])), preset["size"])
    preset["outline"] = max(int(settings.get("outline", preset["outline"])), preset["outline"])
    preset["shadow"] = max(int(settings.get("shadow", preset["shadow"])), preset["shadow"])
    preset["name"] = name
    return preset


class _ApproximateFontMetrics:
    def __init__(self, size: int) -> None:
        self.size = size

    def horizontalAdvance(self, text: str) -> float:  # noqa: N802 - mirrors Qt API
        # Used only in non-GUI workers/tests. Rendering in the application uses
        # QFontMetricsF from the same font that libass receives.
        return sum(self.size * (0.34 if character.isspace() else 0.62) for character in text)


def _metrics(font_name: str, size: int):
    if QGuiApplication.instance() is None:
        return _ApproximateFontMetrics(size)
    font = QFont(font_name)
    font.setPixelSize(size)
    font.setBold(True)
    return QFontMetricsF(font)


def _split_oversized_word(word: str, metrics: QFontMetricsF, width: float) -> list[str]:
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


def pixel_pages(text: str, font_name: str, size: int, outline: int, max_lines: int = 2, max_width: int = MAX_TEXT_WIDTH) -> list[str]:
    """Wrap text by measured pixel width and return pages of at most two lines."""
    available = min(MAX_TEXT_WIDTH, max_width) - 2 * (outline + 4)
    metrics = _metrics(font_name, size)
    tokens: list[str] = []
    for word in text.replace("\n", " ").split():
        if metrics.horizontalAdvance(word) <= available:
            tokens.append(word)
        else:
            tokens.extend(_split_oversized_word(word, metrics, available))
    if not tokens:
        return []
    pages: list[str] = []
    lines: list[str] = []
    current = ""
    for token in tokens:
        proposed = f"{current} {token}".strip()
        if not current or metrics.horizontalAdvance(proposed) <= available:
            current = proposed
            continue
        lines.append(current)
        current = token
        if len(lines) == max(1, min(2, max_lines)):
            pages.append("\n".join(lines))
            lines = []
    if current:
        lines.append(current)
    if lines:
        pages.append("\n".join(lines))
    return pages


def fit_cues(cues: Iterable[SubtitleCue], settings: dict) -> tuple[list[SubtitleCue], dict]:
    cues = list(cues)
    style = resolved_style(settings)
    safe_margin = max(80, int(settings.get("safe_margin", 90)))
    max_width = min(MAX_TEXT_WIDTH, FRAME_WIDTH - 2 * safe_margin)
    available = max_width - 2 * (style["outline"] + 4)
    size = style["size"]
    words = [word for cue in cues for word in cue.text.replace("\n", " ").split()]
    while size > int(settings.get("minimum_size", MIN_FONT_SIZE)):
        metrics = _metrics(style["font"], size)
        if not words or max(metrics.horizontalAdvance(word) for word in words) <= available:
            break
        size -= 2
    style["size"] = size
    result: list[SubtitleCue] = []
    max_lines = min(2, int(settings.get("lines", 2)))
    for cue in cues:
        pages = pixel_pages(cue.text, style["font"], size, style["outline"], max_lines, max_width)
        if not pages:
            continue
        duration = max(0.01, cue.end - cue.start)
        weights = [max(1, len(page.replace("\n", ""))) for page in pages]
        total = sum(weights)
        cursor = cue.start
        for index, (page, weight) in enumerate(zip(pages, weights)):
            end = cue.end if index == len(pages) - 1 else cursor + duration * weight / total
            result.append(SubtitleCue(round(cursor, 3), round(end, 3), page))
            cursor = end
    return result, style


class SubtitleService:
    def generate(self, transcript: Transcript, candidate: Candidate, maximum: int = 36, lines: int = 2) -> list[SubtitleCue]:
        cues = []
        for segment in transcript.segments:
            start = max(segment.start, candidate.start)
            end = min(segment.end, candidate.end)
            if end <= start or not segment.text.strip():
                continue
            cues.append(SubtitleCue(round(start - candidate.start, 3), round(end - candidate.start, 3), wrap_subtitle(segment.text, maximum, lines)))
        return cues

    def write(self, cues: Iterable[SubtitleCue], srt_path: Path, ass_path: Path, settings: dict) -> None:
        cues, style = fit_cues(cues, settings)
        srt_path.parent.mkdir(parents=True, exist_ok=True)
        srt_lines = []
        for index, cue in enumerate(cues, 1):
            srt_lines.extend([str(index), f"{srt_timestamp(cue.start)} --> {srt_timestamp(cue.end)}", cue.text, ""])
        srt_path.write_text("\n".join(srt_lines), encoding="utf-8")

        position = str(settings.get("position", "lower"))
        y = POSITION_Y.get(position, POSITION_Y["lower"])
        back = "&H80000000" if settings.get("background", False) else "&H00000000"
        border_style = 3 if settings.get("background", False) else 1
        side_margin = max(80, int(settings.get("safe_margin", 90)), round((FRAME_WIDTH - MAX_TEXT_WIDTH) / 2) + style["outline"])
        header = (
            "[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\nWrapStyle: 2\nScaledBorderAndShadow: yes\n\n"
            "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
            "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
            f"Style: Shorts,{style['font']},{style['size']},{style['primary']},{style['secondary']},&H00000000,{back},"
            f"-1,0,0,0,100,100,{style['spacing']},0,{border_style},{style['outline']},{style['shadow']},5,{side_margin},{side_margin},0,1\n\n"
            "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        )
        events = []
        for cue in cues:
            text = cue.text.replace("{", "(").replace("}", ")").replace("\n", r"\N")
            events.append(
                f"Dialogue: 0,{ass_timestamp(cue.start)},{ass_timestamp(cue.end)},Shorts,,0,0,0,,"
                f"{{\\an5\\pos(540,{y})}}{text}"
            )
        ass_path.write_text(header + "\n".join(events) + "\n", encoding="utf-8-sig")

    @staticmethod
    def parse_srt(path: Path) -> list[SubtitleCue]:
        if not path.is_file():
            return []
        blocks = re.split(r"\r?\n\s*\r?\n", path.read_text(encoding="utf-8-sig").strip())
        cues = []
        for block in blocks:
            lines = block.splitlines()
            if len(lines) < 3 or " --> " not in lines[1]:
                continue
            left, right = lines[1].split(" --> ", 1)
            cues.append(SubtitleCue(self_seconds(left), self_seconds(right), "\n".join(lines[2:])))
        return cues


def self_seconds(value: str) -> float:
    hours, minutes, rest = value.replace(".", ",").split(":")
    seconds, millis = rest.split(",")
    return int(hours) * 3600 + int(minutes) * 60 + int(seconds) + int(millis) / 1000
