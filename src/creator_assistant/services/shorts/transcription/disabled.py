from creator_assistant.domain.shorts.errors import TranscriptionUnavailableError
from creator_assistant.services.shorts.transcription.base import TranscriptionBackend, TranscriptionCapabilities


class DisabledTranscriptionBackend(TranscriptionBackend):
    def capabilities(self):
        return TranscriptionCapabilities("Отключено", False, details="Распознавание отключено в настройках")

    def transcribe(self, audio, output_dir, cancellation, on_line=None):
        raise TranscriptionUnavailableError("Распознавание речи отключено в настройках.")
