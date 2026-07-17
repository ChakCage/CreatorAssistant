from creator_assistant.services.shorts.reframe.base import ReframeBackend


class BlurBackgroundReframe(ReframeBackend):
    def __init__(self, foreground_scale: int = 100, blur_radius: int = 12, blur_power: int = 6) -> None:
        self.foreground_scale = max(50, min(550, foreground_scale))
        self.blur_radius = max(2, min(40, int(blur_radius)))
        self.blur_power = max(1, min(10, int(blur_power)))

    def video_filter(self, width: int, height: int, fps: float = 30.0) -> str:
        foreground_width = round(1080 * self.foreground_scale / 100)
        foreground_height = round(1920 * self.foreground_scale / 100)
        return (
            "split=2[bgsrc][fgsrc];"
            "[bgsrc]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,"
            f"boxblur={self.blur_radius}:{self.blur_power},scale=1080:1920:flags=fast_bilinear[bg];"
            f"[fgsrc]scale={foreground_width}:{foreground_height}:force_original_aspect_ratio=decrease[fg];"
            "[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1"
        )
