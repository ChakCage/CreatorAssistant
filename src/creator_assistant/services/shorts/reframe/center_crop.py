from creator_assistant.services.shorts.reframe.base import ReframeBackend


def crop_geometry(width: int, height: int, center: int = 50, target_width: int = 1080, target_height: int = 1920):
    if width <= 0 or height <= 0:
        raise ValueError("Размер исходного кадра должен быть положительным.")
    source_ratio = width / height
    target_ratio = target_width / target_height
    center = max(0, min(100, center)) / 100
    if source_ratio >= target_ratio:
        scaled_width = round(width * target_height / height)
        maximum_x = max(0, scaled_width - target_width)
        return scaled_width, target_height, round(maximum_x * center), 0
    scaled_height = round(height * target_width / width)
    maximum_y = max(0, scaled_height - target_height)
    return target_width, scaled_height, 0, round(maximum_y / 2)


class CenterCropReframe(ReframeBackend):
    def __init__(self, center: int = 50) -> None:
        self.center = center

    def video_filter(self, width: int, height: int) -> str:
        scaled_width, scaled_height, x, y = crop_geometry(width, height, self.center)
        return f"scale={scaled_width}:{scaled_height},crop=1080:1920:{x}:{y},setsar=1"
