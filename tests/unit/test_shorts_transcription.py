import json
import sys
from pathlib import Path

from creator_assistant.domain.job import CancellationToken
from creator_assistant.infrastructure.process_runner import ProcessResult
from creator_assistant.services.shorts.transcription.base import TranscriptionCapabilities
from creator_assistant.services.shorts.transcription.existing_whisper import ExistingWhisperBackend, apply_exact_dictionary
from creator_assistant.services.shorts.transcription_service import TranscriptionService, srt_timestamp


def test_dictionary_changes_only_complete_terms():
    text = "minecraft и youtube, но minecraftian не меняется"
    assert apply_exact_dictionary(text, ["Minecraft", "YouTube"]) == "Minecraft и YouTube, но minecraftian не меняется"


def test_timestamp_rounding_and_utf8_srt():
    assert srt_timestamp(3661.2346) == "01:01:01,235"


def test_existing_whisper_parses_real_schema_and_writes_transcripts(tmp_path, monkeypatch):
    audio = tmp_path / "речь автора's.wav"
    audio.write_bytes(b"wav")
    raw = {
        "language": "ru",
        "text": " minecraft и редстоун",
        "segments": [{
            "id": 0, "start": 0.1, "end": 2.5, "text": " minecraft и редстоун",
            "avg_logprob": -0.2,
            "words": [
                {"word": " minecraft", "start": 0.1, "end": 1.0, "probability": 0.9},
                {"word": " редстоун", "start": 1.2, "end": 2.5, "probability": 0.8},
            ],
        }],
    }

    class Runner:
        command = None

        def run(self, command, **kwargs):
            self.command = list(command)
            output = Path(command[command.index("--output_dir") + 1])
            output.mkdir(parents=True, exist_ok=True)
            (output / f"{audio.stem}.json").write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
            return ProcessResult(self.command, 0, "")

    runner = Runner()
    settings = {
        "whisper_python": sys.executable,
        "whisper_model_dir": str(tmp_path / "models"),
        "whisper_model": "large-v3-turbo",
        "whisper_language": "ru",
        "whisper_device": "gpu",
        "whisper_use_gpu": True,
        "whisper_fp16": True,
        "whisper_word_timestamps": True,
        "whisper_dictionary": ["Minecraft", "редстоун"],
    }
    backend = ExistingWhisperBackend(runner, settings)
    monkeypatch.setattr(backend, "capabilities", lambda: TranscriptionCapabilities(
        "local", True, models=["large-v3-turbo"], cuda=True, word_timestamps=True
    ))
    service = TranscriptionService(backend)
    analysis = tmp_path / "Analysis"
    transcript = service.transcribe(audio, analysis, CancellationToken())
    assert transcript.language == "ru"
    assert transcript.model == "large-v3-turbo"
    assert transcript.segments[0].words[0].word.strip() == "Minecraft"
    assert "--device" in runner.command and runner.command[runner.command.index("--device") + 1] == "cuda"
    assert any(Path(argument).name == "речь автора's.wav" for argument in runner.command)
    assert "Minecraft" in (analysis / "transcript.txt").read_text(encoding="utf-8")
    assert "00:00:00,100 --> 00:00:02,500" in (analysis / "transcript.srt").read_text(encoding="utf-8")
    assert (analysis / "transcript.vtt").read_text(encoding="utf-8").startswith("WEBVTT")
    loaded = service.load(analysis / "transcript.json")
    assert loaded.segments[0].words[1].word.strip() == "редстоун"
