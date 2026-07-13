from creator_assistant.domain.shorts.errors import TranscriptionUnavailableError
from creator_assistant.services.shorts.transcription.base import TranscriptionBackend, TranscriptionCapabilities
from creator_assistant.services.shorts.transcription.existing_whisper import find_existing_python


class FasterWhisperBackend(TranscriptionBackend):
    """Optional backend descriptor; it never silently substitutes another engine."""

    def __init__(self, runner, settings):
        self.runner, self.settings = runner, settings
        self.python = str(settings.get("whisper_python") or find_existing_python())

    def capabilities(self):
        try:
            result = self.runner.run([
                self.python, "-X", "utf8", "-c",
                "import importlib.metadata; print(importlib.metadata.version('faster-whisper'))",
            ], timeout=30)
            version = result.output.strip().splitlines()[-1]
            return TranscriptionCapabilities("Faster Whisper", True, version, self.python, cuda=bool(self.settings.get("whisper_use_gpu", True)), word_timestamps=True)
        except Exception:
            return TranscriptionCapabilities("Faster Whisper", False, executable=self.python, details="Пакет faster-whisper не найден в выбранном runtime")

    def transcribe(self, audio, output_dir, cancellation, on_line=None):
        raise TranscriptionUnavailableError(
            "Faster Whisper выбран, но этот MVP не устанавливает CTranslate2 автоматически. "
            "Выберите найденный локальный Whisper или управляемый backend."
        )
