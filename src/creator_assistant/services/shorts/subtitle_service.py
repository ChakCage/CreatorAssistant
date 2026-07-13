from __future__ import annotations

import re
from dataclasses import asdict
from pathlib import Path
from typing import Iterable

from creator_assistant.domain.shorts.models import Candidate, SubtitleCue, Transcript
from creator_assistant.services.shorts.transcription_service import srt_timestamp


def wrap_subtitle(text: str, maximum: int = 36, lines: int = 2) -> str:
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
        cues = list(cues)
        srt_path.parent.mkdir(parents=True, exist_ok=True)
        srt_lines = []
        for index, cue in enumerate(cues, 1):
            srt_lines.extend([str(index), f"{srt_timestamp(cue.start)} --> {srt_timestamp(cue.end)}", cue.text, ""])
        srt_path.write_text("\n".join(srt_lines), encoding="utf-8")
        style = str(settings.get("style", "clean"))
        position = str(settings.get("position", "lower"))
        size = int(settings.get("size", 58))
        outline = int(settings.get("outline", 3))
        shadow = int(settings.get("shadow", 1))
        margin = int(settings.get("safe_margin", 160))
        alignment = {"upper": 8, "center": 5, "lower": 2}.get(position, 2)
        primary = "&H00FFFFFF"
        back = "&H80000000" if settings.get("background", False) else "&H00000000"
        border_style = 3 if settings.get("background", False) else 1
        if style == "large":
            size = max(size, 72)
        elif style == "gaming":
            primary, size = "&H0000FFFF", max(size, 66)
        header = (
            "[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\nWrapStyle: 2\nScaledBorderAndShadow: yes\n\n"
            "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
            "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
            f"Style: Shorts,Segoe UI,{size},{primary},&H000000FF,&H00000000,{back},-1,0,0,0,100,100,0,0,{border_style},{outline},{shadow},{alignment},80,80,{margin},1\n\n"
            "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        )
        events = []
        for cue in cues:
            text = cue.text.replace("{", "(").replace("}", ")").replace("\n", r"\N")
            events.append(f"Dialogue: 0,{ass_timestamp(cue.start)},{ass_timestamp(cue.end)},Shorts,,0,0,0,,{text}")
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
