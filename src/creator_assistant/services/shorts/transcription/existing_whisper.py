from __future__ import annotations

import importlib.metadata
import json
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Callable, Dict, Iterable, Optional

from creator_assistant.domain.job import CancellationToken
from creator_assistant.domain.shorts.errors import TranscriptionUnavailableError
from creator_assistant.domain.shorts.models import Transcript, TranscriptSegment, TranscriptWord
from creator_assistant.infrastructure.process_runner import ProcessRunner
from creator_assistant.services.shorts.transcription.base import TranscriptionBackend, TranscriptionCapabilities


DEFAULT_DICTIONARY = [
    "Minecraft", "GTA 6", "PS5 Pro", "OLED", "Beppo", "MylesMC", "редстоун",
    "хардкор", "YouTube", "Shorts", "REAPER", "Vegas",
]


def _model_cache() -> Path:
    return Path.home() / ".cache" / "whisper"


def _available_models(folder: Path) -> list[str]:
    return sorted((item.stem for item in folder.glob("*.pt") if item.stat().st_size > 0), key=str.casefold)


def find_existing_python() -> str:
    configured = shutil.which("whisper") or shutil.which("whisper.exe")
    if configured:
        scripts = Path(configured).resolve().parent
        for candidate in (scripts.parent / "python.exe", scripts.parent / "python", scripts / "python.exe"):
            if candidate.is_file():
                return str(candidate)
    return sys.executable


def apply_exact_dictionary(text: str, terms: Iterable[str]) -> str:
    """Restore casing only for complete tokens/phrases; never replace substrings."""
    result = text
    for term in sorted({item.strip() for item in terms if item.strip()}, key=len, reverse=True):
        pattern = re.compile(r"(?<![\w])" + re.escape(term) + r"(?![\w])", re.IGNORECASE | re.UNICODE)
        result = pattern.sub(term, result)
    return result


class ExistingWhisperBackend(TranscriptionBackend):
    def __init__(self, runner: ProcessRunner, settings: Dict[str, object]) -> None:
        self.runner = runner
        self.settings = settings
        self.python = str(settings.get("whisper_python") or find_existing_python())
        configured = str(settings.get("whisper_model_dir") or "")
        self.model_dir = Path(configured) if configured else _model_cache()

    def capabilities(self) -> TranscriptionCapabilities:
        executable = Path(self.python)
        available = executable.is_file()
        version = ""
        if available:
            try:
                if executable.resolve() == Path(sys.executable).resolve():
                    version = importlib.metadata.version("openai-whisper")
                else:
                    result = self.runner.run([
                        self.python, "-X", "utf8", "-c",
                        "import importlib.metadata; print(importlib.metadata.version('openai-whisper'))",
                    ], timeout=30)
                    version = result.output.strip().splitlines()[-1]
            except Exception:
                available = False
        return TranscriptionCapabilities(
            name="Найденный локальный Whisper", available=available, version=version,
            executable=self.python, models=_available_models(self.model_dir),
            cuda=bool(self.settings.get("whisper_use_gpu", True)), word_timestamps=True,
            details=f"Модели: {self.model_dir}",
        )

    def transcribe(self, audio: Path, output_dir: Path, cancellation: CancellationToken, on_line: Optional[Callable[[str], None]] = None) -> Transcript:
        capabilities = self.capabilities()
        if not capabilities.available:
            raise TranscriptionUnavailableError("Найденный локальный Whisper недоступен для headless-запуска.")
        model = str(self.settings.get("whisper_model") or ("large-v3-turbo" if "large-v3-turbo" in capabilities.models else (capabilities.models[-1] if capabilities.models else "turbo")))
        language = str(self.settings.get("whisper_language") or "ru")
        device = str(self.settings.get("whisper_device") or "auto")
        use_gpu = bool(self.settings.get("whisper_use_gpu", True))
        if device == "auto":
            device = "cuda" if use_gpu else "cpu"
        elif device == "gpu":
            device = "cuda"
        fp16 = bool(self.settings.get("whisper_fp16", True)) and device == "cuda"
        words = bool(self.settings.get("whisper_word_timestamps", True))
        dictionary = (
            list(self.settings.get("whisper_dictionary") or DEFAULT_DICTIONARY)
            if bool(self.settings.get("whisper_use_dictionary", True)) else []
        )
        output_dir.mkdir(parents=True, exist_ok=True)
        command = [
            self.python, "-X", "utf8", "-m", "whisper", str(audio),
            "--model", model, "--model_dir", str(self.model_dir), "--device", device,
            "--task", "transcribe", "--fp16", str(fp16),
            "--word_timestamps", str(words), "--output_dir", str(output_dir),
            "--output_format", "all", "--verbose", "False",
        ]
        if language != "auto":
            command.extend(["--language", language])
        if dictionary:
            command.extend(["--initial_prompt", ", ".join(dictionary)])
        self.runner.run(command, cancellation=cancellation, on_line=on_line, environment={"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"})
        raw_path = output_dir / f"{audio.stem}.json"
        if not raw_path.is_file():
            raise TranscriptionUnavailableError("Whisper завершился без transcript JSON.")
        raw = json.loads(raw_path.read_text(encoding="utf-8"))
        segments = []
        for index, item in enumerate(raw.get("segments") or []):
            segment_words = [
                TranscriptWord(
                    start=float(word["start"]), end=float(word["end"]),
                    word=apply_exact_dictionary(str(word.get("word") or ""), dictionary),
                    probability=float(word["probability"]) if word.get("probability") is not None else None,
                )
                for word in (item.get("words") or []) if "start" in word and "end" in word
            ]
            avg_logprob = item.get("avg_logprob")
            segments.append(TranscriptSegment(
                id=int(item.get("id", index)), start=float(item.get("start", 0)), end=float(item.get("end", 0)),
                text=apply_exact_dictionary(str(item.get("text") or "").strip(), dictionary),
                confidence=float(pow(2.718281828, avg_logprob)) if avg_logprob is not None else None,
                words=segment_words,
            ))
        duration = max((segment.end for segment in segments), default=0.0)
        text = " ".join(segment.text for segment in segments).strip()
        return Transcript(str(raw.get("language") or language), duration, text, segments, "existing_whisper", model)
