from pathlib import Path

from creator_assistant.services.shorts.transcription.existing_whisper import ExistingWhisperBackend
from creator_assistant.services.shorts.transcription.runtime_manager import WhisperRuntimeManager


class ManagedWhisperBackend(ExistingWhisperBackend):
    def __init__(self, runner, settings, runtime: WhisperRuntimeManager):
        managed = dict(settings)
        managed["whisper_python"] = str(runtime.python)
        existing_cache = Path.home() / ".cache" / "whisper"
        managed["whisper_model_dir"] = str(runtime.models if any(runtime.models.glob("*.pt")) else existing_cache)
        super().__init__(runner, managed)
        self.runtime = runtime
