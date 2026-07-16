from __future__ import annotations

from enum import Enum


class HorizontalTextAlignment(str, Enum):
    LEFT = "left"
    CENTER = "center"
    RIGHT = "right"

    @classmethod
    def parse(cls, value: object) -> "HorizontalTextAlignment":
        if isinstance(value, cls):
            return value
        try:
            return cls(str(value or "").strip().lower())
        except ValueError:
            return cls.CENTER


def aligned_left(
    block_width: float,
    alignment: HorizontalTextAlignment | str,
    offset_x: float = 0,
    *,
    canvas_width: float = 1080,
    safe_margin: float = 90,
) -> float:
    """Return a safe, left-edge coordinate for one rendered text line/block."""
    alignment = HorizontalTextAlignment.parse(alignment)
    block_width = min(max(0.0, float(block_width)), canvas_width - 2 * safe_margin)
    if alignment is HorizontalTextAlignment.LEFT:
        base = safe_margin
    elif alignment is HorizontalTextAlignment.RIGHT:
        base = canvas_width - safe_margin - block_width
    else:
        base = (canvas_width - block_width) / 2
    return max(safe_margin, min(canvas_width - safe_margin - block_width, base + float(offset_x)))


def ass_anchor(position: str, alignment: HorizontalTextAlignment | str) -> int:
    horizontal = {
        HorizontalTextAlignment.LEFT: 1,
        HorizontalTextAlignment.CENTER: 2,
        HorizontalTextAlignment.RIGHT: 3,
    }[HorizontalTextAlignment.parse(alignment)]
    vertical_base = {"lower": 0, "center": 3, "upper": 6}.get(str(position), 0)
    return vertical_base + horizontal
