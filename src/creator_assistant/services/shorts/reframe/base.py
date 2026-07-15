from abc import ABC, abstractmethod


class ReframeBackend(ABC):
    @abstractmethod
    def video_filter(self, width: int, height: int, fps: float = 30.0) -> str:
        raise NotImplementedError
