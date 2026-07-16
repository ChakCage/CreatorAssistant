from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

from PySide6.QtGui import QImageReader

from creator_assistant.domain.shorts.models import Candidate, SubtitleCue, Transcript
from creator_assistant.services.shorts.subtitle_layout import (
    FRAME_WIDTH,
    MAX_TEXT_WIDTH,
    MIN_FONT_SIZE,
    POSITION_Y,
    STYLE_PRESETS,
    SubtitleLayoutCalculator,
    font_metrics,
    pixel_pages,
    resolved_style,
)
from creator_assistant.services.shorts.text_alignment import HorizontalTextAlignment, ass_anchor
from creator_assistant.services.shorts.overlay_layout import OverlayLayoutCalculator
from creator_assistant.services.shorts.transcription_service import srt_timestamp


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


def _metrics(font_name: str, size: int):
    return font_metrics(font_name, size)


def ass_timestamp(seconds: float) -> str:
    centiseconds = max(0, round(seconds * 100))
    hours, remainder = divmod(centiseconds, 360000)
    minutes, remainder = divmod(remainder, 6000)
    secs, cents = divmod(remainder, 100)
    return f"{hours}:{minutes:02d}:{secs:02d}.{cents:02d}"


def fit_cues(cues: Iterable[SubtitleCue], settings: dict) -> tuple[list[SubtitleCue], dict]:
    cues = list(cues)
    style = resolved_style(settings)
    safe_margin = max(80, int(settings.get("safe_margin", 90)))
    max_width = min(MAX_TEXT_WIDTH, FRAME_WIDTH - 2 * safe_margin)
    available = max_width - 2 * (style["outline"] + 4)
    size = style["size"]
    words = [word for cue in cues for word in cue.text.replace("\n", " ").split()]
    while size > int(settings.get("minimum_size", MIN_FONT_SIZE)):
        metrics = font_metrics(style["font"], size, int(style.get("weight", 700)))
        if not words or max(metrics.horizontalAdvance(word) for word in words) <= available:
            break
        size -= 2
    style["size"] = size
    result: list[SubtitleCue] = []
    max_lines = min(2, int(settings.get("lines", 2)))
    for cue in cues:
        metrics = font_metrics(style["font"], size, int(style.get("weight", 700)))
        explicit_lines = [line.strip() for line in cue.text.splitlines() if line.strip()]
        if (
            1 < len(explicit_lines) <= max_lines
            and all(metrics.horizontalAdvance(line) <= available for line in explicit_lines)
        ):
            pages = ["\n".join(explicit_lines)]
        else:
            pages = pixel_pages(cue.text, style["font"], size, style["outline"], max_lines, max_width, int(style.get("weight", 700)))
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
        timed_words: list[tuple[float, float, str]] = []
        fallback_segments: list[tuple[float, float, str]] = []
        for segment in transcript.segments:
            start = max(segment.start, candidate.start)
            end = min(segment.end, candidate.end)
            if end <= start or not segment.text.strip():
                continue
            words = [
                (max(word.start, candidate.start), min(word.end, candidate.end), word.word.strip())
                for word in segment.words
                if word.end > candidate.start and word.start < candidate.end and word.word.strip()
            ]
            if words:
                timed_words.extend(item for item in words if item[1] > item[0])
            else:
                fallback_segments.append((start, end, segment.text.strip()))
        cues = self._word_cues(timed_words, candidate, maximum, lines)
        cues.extend(self._segment_cues(fallback_segments, candidate, maximum, lines))
        cues.sort(key=lambda item: (item.start, item.end))
        result: list[SubtitleCue] = []
        for cue in cues:
            cue = SubtitleCue(max(0.0, cue.start), min(candidate.duration, cue.end), cue.text.strip())
            if cue.end <= cue.start or not cue.text:
                continue
            if result and cue.text.casefold() == result[-1].text.casefold() and cue.start <= result[-1].end + 0.05:
                result[-1].end = max(result[-1].end, cue.end)
                continue
            if result and cue.start < result[-1].end:
                cue.start = result[-1].end
            if cue.end > cue.start:
                result.append(cue)
        return result

    @staticmethod
    def _word_cues(words, candidate: Candidate, maximum: int, lines: int) -> list[SubtitleCue]:
        if not words:
            return []
        limit = max(16, int(maximum) * max(1, min(2, int(lines))))
        result, group = [], []

        def flush() -> None:
            if not group:
                return
            text = " ".join(item[2] for item in group).strip()
            result.append(SubtitleCue(
                round(group[0][0] - candidate.start, 3),
                round(group[-1][1] - candidate.start, 3),
                wrap_subtitle(text, maximum, min(2, lines)),
            ))
            group.clear()

        for item in sorted(words, key=lambda value: (value[0], value[1])):
            proposed = " ".join([*(value[2] for value in group), item[2]])
            pause = item[0] - group[-1][1] if group else 0
            duration = item[1] - group[0][0] if group else item[1] - item[0]
            if group and (pause >= 0.65 or duration > 4.5 or len(proposed) > limit):
                flush()
            group.append(item)
            current_duration = group[-1][1] - group[0][0]
            if item[2].rstrip().endswith((".", "!", "?", "…", ":", ";")) and current_duration >= 1.0:
                flush()
        flush()
        return result

    @staticmethod
    def _segment_cues(segments, candidate: Candidate, maximum: int, lines: int) -> list[SubtitleCue]:
        result: list[SubtitleCue] = []
        limit = max(16, int(maximum) * max(1, min(2, int(lines))))
        for start, end, text in segments:
            words = text.split()
            chunks: list[list[str]] = [words]
            duration = end - start
            count = int(duration / 6.0 + 0.999) if duration > 8.0 else 1
            if len(text) > limit * 3:
                count = max(count, int(len(text) / (limit * 2) + 0.999))
            if count > 1 and words:
                per_chunk = max(1, int(len(words) / count + 0.999))
                chunks = [words[index:index + per_chunk] for index in range(0, len(words), per_chunk)]
            weights = [max(1, len(chunk)) for chunk in chunks]
            total = sum(weights) or 1
            cursor = start
            for index, (chunk, weight) in enumerate(zip(chunks, weights)):
                cue_end = end if index == len(chunks) - 1 else cursor + duration * weight / total
                result.append(SubtitleCue(
                    round(cursor - candidate.start, 3), round(cue_end - candidate.start, 3),
                    wrap_subtitle(" ".join(chunk), maximum, min(2, lines)),
                ))
                cursor = cue_end
        return result

    @staticmethod
    def validate(cues: Iterable[SubtitleCue], duration: float, settings: dict) -> list[str]:
        values = list(cues)
        if not values:
            return ["События субтитров отсутствуют"]
        problems: list[str] = []
        previous_end = -1.0
        seen: set[tuple[int, int, str]] = set()
        for cue in values:
            if cue.start < 0 or cue.end > duration + 0.001 or cue.end <= cue.start:
                problems.append("Событие выходит за границы кандидата")
            if cue.start < previous_end - 0.001:
                problems.append("События субтитров пересекаются")
            if cue.end - cue.start > 8.01:
                problems.append("Событие субтитров длится более 8 секунд")
            if len(cue.text.replace("\n", " ")) > max(30, int(settings.get("maximum", 36)) * 3):
                problems.append("Событие содержит слишком большой абзац")
            if len(cue.text.splitlines()) > min(2, int(settings.get("lines", 2))):
                problems.append("Событие содержит слишком много строк")
            if cue.text.rstrip().endswith(("-", "‑", "�")):
                problems.append("Последнее слово события оборвано")
            identity = (round(cue.start * 1000), round(cue.end * 1000), cue.text.casefold())
            if identity in seen:
                problems.append("Обнаружено дублирующее событие")
            seen.add(identity)
            previous_end = max(previous_end, cue.end)
        return list(dict.fromkeys(problems))

    def write(
        self,
        cues: Iterable[SubtitleCue],
        srt_path: Path,
        ass_path: Path,
        settings: dict,
        branding_settings: dict | None = None,
    ) -> None:
        cues, style = fit_cues(cues, settings)
        srt_path.parent.mkdir(parents=True, exist_ok=True)
        srt_lines = []
        for index, cue in enumerate(cues, 1):
            srt_lines.extend([str(index), f"{srt_timestamp(cue.start)} --> {srt_timestamp(cue.end)}", cue.text, ""])
        srt_path.write_text("\n".join(srt_lines), encoding="utf-8")

        alignment = HorizontalTextAlignment.parse(settings.get("alignment", "center"))
        anchor = ass_anchor(str(settings.get("position", "lower")), alignment)
        back = "&H80000000" if settings.get("background", False) else "&H00000000"
        border_style = 3 if settings.get("background", False) else 1
        side_margin = max(80, int(settings.get("safe_margin", 90)), round((FRAME_WIDTH - MAX_TEXT_WIDTH) / 2) + style["outline"])
        header = (
            "[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\nWrapStyle: 2\nScaledBorderAndShadow: yes\n\n"
            "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
            "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
            f"Style: Shorts,{style['font']},{style['size']},{style['primary']},{style['secondary']},&H00000000,{back},"
            f"-1,0,0,0,100,100,{style['spacing']},0,{border_style},{style['outline']},{style['shadow']},{anchor},{side_margin},{side_margin},0,1\n\n"
            "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        )
        events = []
        branding = dict(branding_settings or {})
        banner_path = Path(str(branding.get("channel_banner_path") or ""))
        banner_size = None
        if bool(branding.get("show_channel_card", False)) and banner_path.is_file():
            size = QImageReader(str(banner_path)).size()
            if size.isValid():
                banner_size = (size.width(), size.height())
        for cue in cues:
            text = cue.text.replace("{", "(").replace("}", ")").replace("\n", r"\N")
            cue_layout = OverlayLayoutCalculator().subtitle_layout(
                cue.text,
                {**settings, "size": style["size"]},
                branding,
                banner_size,
            )
            y = cue_layout.y
            position = str(settings.get("position", "lower"))
            if position == "lower":
                y += cue_layout.height // 2
            elif position == "upper":
                y -= cue_layout.height // 2
            events.append(
                f"Dialogue: 0,{ass_timestamp(cue.start)},{ass_timestamp(cue.end)},Shorts,,0,0,0,,"
                f"{{\\an{anchor}\\pos({cue_layout.x},{y})}}{text}"
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
