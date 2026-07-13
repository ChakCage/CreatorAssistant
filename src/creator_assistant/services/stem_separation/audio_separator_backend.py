from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Callable, Optional

from creator_assistant.domain.errors import (
    AudioSeparatorOutputMissingError,
    AudioSeparatorProcessError,
    AudioSeparatorRuntimeMissingError,
    JobCancelledError,
    ProcessExecutionError,
    ValidationError,
)
from creator_assistant.domain.job import CancellationToken
from creator_assistant.infrastructure.audio_separator_runtime import AudioSeparatorRuntimeManager
from creator_assistant.infrastructure.process_runner import ProcessRunner
from creator_assistant.infrastructure.settings_store import local_data_root

from .base import StemSeparatorBackend


class AudioSeparatorBackend(StemSeparatorBackend):
    EVENT_PREFIX = "CREATOR_JSON:"

    def __init__(self, runner: ProcessRunner, runtime: AudioSeparatorRuntimeManager, ffprobe_path: str, ffmpeg_path: str = "ffmpeg", use_gpu: bool = True) -> None:
        self.runner = runner
        self.runtime = runtime
        self.ffprobe_path = ffprobe_path
        self.ffmpeg_path = ffmpeg_path
        self.use_gpu = use_gpu
        self.job_id = "unknown"
        self.temp_dir: Optional[Path] = None

    def configure_job(self, job_id: str, temp_dir: Optional[Path] = None) -> None:
        self.job_id = job_id
        self.temp_dir = Path(temp_dir) if temp_dir else None

    def available(self) -> bool:
        return self.runtime.is_ready()

    def separate(
        self,
        source: Path,
        expected_output: Path,
        cancellation: CancellationToken,
        on_message: Optional[Callable[[str], None]] = None,
    ) -> Path:
        if not self.available():
            raise AudioSeparatorRuntimeMissingError(self.runtime.root)
        if source.resolve().parent != expected_output.resolve().parent:
            raise ValidationError("Audio Separator input не принадлежит текущей папке проекта.")
        sample_rate, channels, source_duration, _source_codec = self._audio_properties(source, cancellation)
        if channels != 2:
            raise ValidationError(f"Ожидалось stereo-аудио, найдено каналов: {channels}")
        worker_source = source
        temporary_input: Optional[Path] = None
        if source.suffix.casefold() == ".webm":
            temporary_input = (self.temp_dir or (local_data_root() / "jobs" / self.job_id / "temp")) / "separator_input.flac"
            temporary_input.parent.mkdir(parents=True, exist_ok=True)
            temporary_input.unlink(missing_ok=True)
            if on_message:
                on_message("Подготавливаю WEBM/Opus через FFmpeg без изменения громкости")
            self.runner.run(
                [
                    self.ffmpeg_path,
                    "-hide_banner",
                    "-loglevel", "error",
                    "-nostdin",
                    "-y",
                    "-i", str(source),
                    "-map", "0:a:0",
                    "-vn",
                    "-c:a", "flac",
                    "-ar", str(sample_rate),
                    "-ac", str(channels),
                    str(temporary_input),
                ],
                cancellation=cancellation,
                environment=self._environment(),
                cwd=self.temp_dir,
            )
            worker_source = temporary_input
        gpu_probe = getattr(self.runtime, "gpu_available", None)
        effective_gpu = self.use_gpu and (bool(gpu_probe()) if gpu_probe else True)
        if self.use_gpu and not effective_gpu and on_message:
            on_message("CUDA недоступна — используется полностью автоматический CPU fallback (медленнее)")
        command = [
            str(self.runtime.python_executable),
            str(self.runtime.worker_path),
            "--input", str(worker_source),
            "--output-dir", str(expected_output.parent),
            "--output-name", expected_output.name,
            "--model-path", str(self.runtime.model_path),
            "--sample-rate", str(sample_rate),
            "--job-id", self.job_id,
        ]
        if effective_gpu:
            command.append("--use-gpu")
        result_path: Optional[Path] = None

        def parse(line: str) -> None:
            nonlocal result_path
            marker = line.find(self.EVENT_PREFIX)
            if marker < 0:
                if on_message and line.strip():
                    on_message(line.strip())
                return
            try:
                event = json.loads(line[marker + len(self.EVENT_PREFIX) :])
            except json.JSONDecodeError:
                return
            if event.get("type") == "result":
                result_path = Path(str(event.get("path", "")))
            elif on_message:
                message = str(event.get("message") or "Audio Separator работает")
                elapsed = event.get("elapsed_seconds")
                on_message(message + (f" — прошло {elapsed} с" if elapsed is not None else ""))

        self.runner.logger.info("Audio Separator input: %s", source)
        self.runner.logger.info("Audio Separator expected result: %s", expected_output)
        self.runner.logger.info("Audio Separator model: %s", self.runtime.model_path)
        self.runner.logger.info("Audio Separator GPU requested: %s; effective: %s", self.use_gpu, effective_gpu)
        output_existed = expected_output.exists()
        previous_flacs = {path.resolve() for folder in filter(None, (self.temp_dir, expected_output.parent)) for path in folder.glob("*.flac")}
        try:
            self.runner.run(
                command,
                cancellation=cancellation,
                on_line=parse,
                no_output_timeout=90,
                environment=self._environment(),
                cwd=self.temp_dir,
            )
        except JobCancelledError:
            if not output_existed:
                expected_output.unlink(missing_ok=True)
            raise
        except ProcessExecutionError as exc:
            raise AudioSeparatorProcessError("Audio Separator завершился с ошибкой.", exc.details) from exc
        finally:
            if temporary_input is not None:
                temporary_input.unlink(missing_ok=True)
        path = result_path or expected_output
        if not path.is_file() or path.stat().st_size <= 0:
            recovered = self._find_new_flac(previous_flacs, expected_output, source_duration, channels, sample_rate, cancellation)
            if recovered:
                if recovered.resolve() != expected_output.resolve():
                    os.replace(str(recovered), str(expected_output))
                path = expected_output
            else:
                raise AudioSeparatorOutputMissingError(self.temp_dir or "", expected_output.parent)
        if path.resolve().parent != expected_output.resolve().parent:
            raise ValidationError("Audio Separator вернул файл вне текущего проекта.")
        output_rate, output_channels, output_duration, output_codec = self._audio_properties(path, cancellation)
        if path.suffix.casefold() != ".flac" or output_codec.casefold() != "flac":
            raise ValidationError("Audio Separator создал файл, который не является FLAC.")
        if output_rate != sample_rate:
            raise ValidationError(f"Audio Separator изменил sample rate: {sample_rate} -> {output_rate} Hz.")
        if output_channels != channels:
            raise ValidationError(f"Audio Separator изменил число каналов: {channels} -> {output_channels}.")
        if source_duration and output_duration and abs(source_duration - output_duration) > max(2.0, source_duration * 0.01):
            raise ValidationError("Длительность Instrumental заметно отличается от исходного аудио.")
        return path

    def _environment(self) -> dict[str, str]:
        if not self.temp_dir:
            return {}
        value = str(self.temp_dir)
        return {"TEMP": value, "TMP": value, "TMPDIR": value}

    def _find_new_flac(self, previous, expected_output, duration, channels, sample_rate, cancellation):
        candidates = []
        for folder in filter(None, (self.temp_dir, expected_output.parent)):
            if not Path(folder).is_dir():
                continue
            for candidate in Path(folder).glob("*.flac"):
                if candidate.resolve() in previous or candidate.stat().st_size <= 0:
                    continue
                try:
                    rate, found_channels, found_duration, codec = self._audio_properties(candidate, cancellation)
                    if codec.casefold() != "flac" or found_channels != channels or rate != sample_rate:
                        continue
                    if duration and found_duration and abs(duration - found_duration) > max(2.0, duration * 0.02):
                        continue
                    candidates.append(candidate)
                except Exception:
                    continue
        return max(candidates, key=lambda path: path.stat().st_mtime_ns) if candidates else None

    def _audio_properties(self, source: Path, cancellation: CancellationToken) -> tuple[int, int, float, str]:
        result = self.runner.run(
            [self.ffprobe_path, "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=sample_rate,channels,codec_name:format=duration", "-of", "json", str(source)],
            cancellation=cancellation,
            timeout=60,
            environment=self._environment(),
            cwd=self.temp_dir,
        )
        data = json.loads(result.stdout or result.output)
        stream = data.get("streams", [{}])[0]
        duration = float(data.get("format", {}).get("duration") or 0.0)
        return (
            int(stream.get("sample_rate") or 44100),
            int(stream.get("channels") or 0),
            duration,
            str(stream.get("codec_name") or ""),
        )
