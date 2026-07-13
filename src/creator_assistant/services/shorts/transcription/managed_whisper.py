from creator_assistant.services.shorts.transcription.existing_whisper import ExistingWhisperBackend
from creator_assistant.services.shorts.transcription.runtime_manager import WhisperRuntimeManager


class ManagedWhisperBackend(ExistingWhisperBackend):
    def __init__(self, runner, settings, runtime: WhisperRuntimeManager):
        managed = dict(settings)
        managed["whisper_python"] = str(runtime.python)
        managed["whisper_model_dir"] = str(runtime.models)
        super().__init__(runner, managed)
        self.runtime = runtime
