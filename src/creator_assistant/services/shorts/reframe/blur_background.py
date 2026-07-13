from creator_assistant.services.shorts.reframe.base import ReframeBackend


class BlurBackgroundReframe(ReframeBackend):
    def __init__(self, foreground_scale: int = 100) -> None:
        self.foreground_scale = max(70, min(115, foreground_scale))

    def video_filter(self, width: int, height: int) -> str:
        foreground_width = round(1080 * self.foreground_scale / 100)
        foreground_height = round(1920 * self.foreground_scale / 100)
        return (
            "split=2[bgsrc][fgsrc];"
            "[bgsrc]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=30:15[bg];"
            f"[fgsrc]scale={foreground_width}:{foreground_height}:force_original_aspect_ratio=decrease[fg];"
            "[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1"
        )
