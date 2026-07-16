from creator_assistant.services.shorts.reframe.base import ReframeBackend


class SolidColorReframe(ReframeBackend):
    """Place the source over a fixed black 1080x1920 background."""

    def __init__(self, foreground_scale: int = 100, color: str = "black") -> None:
        self.foreground_scale = max(50, min(550, foreground_scale))
        self.color = color or "black"

    def video_filter(self, width: int, height: int, fps: float = 30.0) -> str:
        foreground_width = round(1080 * self.foreground_scale / 100)
        foreground_height = round(1920 * self.foreground_scale / 100)
        rate = _ffmpeg_rate(fps)
        return (
            f"scale={foreground_width}:{foreground_height}:force_original_aspect_ratio=decrease[fg];"
            f"color=c={self.color}:s=1080x1920:r={rate}[bg];"
            "[bg][fg]overlay=(W-w)/2:(H-h)/2:shortest=1,setsar=1"
        )


def _ffmpeg_rate(fps: float) -> str:
    for value, rate in (
        (24000 / 1001, "24000/1001"),
        (30000 / 1001, "30000/1001"),
        (60000 / 1001, "60000/1001"),
    ):
        if abs(fps - value) < 0.01:
            return rate
    return f"{max(1.0, fps):.6f}"
