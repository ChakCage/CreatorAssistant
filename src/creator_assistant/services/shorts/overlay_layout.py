from __future__ import annotations

from dataclasses import dataclass


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
        offset_x = int(settings.get("banner_offset_x", settings.get("banner_x", 0)) or 0)
        offset_y = int(settings.get("banner_offset_y", 0) or 0)
        if "banner_y" in settings and "banner_offset_y" not in settings:
            # Migrate old absolute Y around the previous default 1600 into offset semantics.
            offset_y = int(settings.get("banner_y", 1600) or 1600) - 1600
        x = round((self.width - target_width) / 2 + offset_x)
        y = round(self.height - safe - target_height + offset_y)
        x = max(safe, min(self.width - safe - target_width, x))
        y = max(safe, min(self.height - safe - target_height, y))
        return Rect(x, y, target_width, target_height)
