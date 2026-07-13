import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from creator_assistant.domain.errors import ValidationError
from creator_assistant.domain.job import CancellationToken
from creator_assistant.infrastructure import audio_separator_runtime as runtime_module
from creator_assistant.infrastructure.audio_separator_runtime import AudioSeparatorRuntimeManager
from creator_assistant.infrastructure.process_runner import ProcessResult
from creator_assistant.services.stem_separation.audio_separator_backend import AudioSeparatorBackend
from creator_assistant.services.stem_separation import audio_separator_backend as backend_module


class RuntimeStub:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.python_executable = root / "Scripts" / "python.exe"
        self.worker_path = root / "worker" / "audio_separator_worker.py"
        self.model_path = root / "models" / "UVR-MDX-NET-Inst_HQ_3.onnx"
        for path in (self.python_executable, self.worker_path, self.model_path):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"ready")

    def is_ready(self) -> bool:
        return True


class SeparatorRunner:
    def __init__(self, output_codec: str = "flac") -> None:
        self.calls = []
        self.output_codec = output_codec
        self.logger = SimpleNamespace(info=lambda *args, **kwargs: None)

    def run(self, command, **kwargs):
        command = [str(value) for value in command]
        self.calls.append(command)
        if command[0] == "ffprobe.exe":
            is_output = Path(command[-1]).suffix.casefold() == ".flac"
            codec = self.output_codec if is_output else "opus"
            payload = {
                "streams": [{"codec_name": codec, "sample_rate": "48000", "channels": 2}],
                "format": {"duration": "30.0"},
            }
            return ProcessResult(command, 0, json.dumps(payload), json.dumps(payload))
        if command[0] == "ffmpeg.exe":
            Path(command[-1]).parent.mkdir(parents=True, exist_ok=True)
            Path(command[-1]).write_bytes(b"temporary flac")
            return ProcessResult(command, 0, "ok", "ok")
        output = Path(command[command.index("--output-dir") + 1]) / command[command.index("--output-name") + 1]
        output.write_bytes(b"valid instrumental")
        callback = kwargs.get("on_line")
        if callback:
            callback("CREATOR_JSON:" + json.dumps({"type": "environment", "message": "cuda", "torch_device": "cuda"}))
            callback("CREATOR_JSON:" + json.dumps({"type": "result", "path": str(output)}))
        return ProcessResult(command, 0, "ok", "ok")


def test_backend_passes_current_unicode_paths_and_gpu_flag(monkeypatch, tmp_path: Path):
    materials = tmp_path / "Делаю" / "Author's ролик" / "Материалы"
    materials.mkdir(parents=True)
    source = materials / "Author's ролик [Audio].webm"
    output = materials / "Author's ролик [Instrumental].flac"
    source.write_bytes(b"opus")
    runtime = RuntimeStub(tmp_path / "runtime")
    runner = SeparatorRunner()
    monkeypatch.setattr(backend_module, "local_data_root", lambda: tmp_path / "local" / "CreatorAssistant")
    backend = AudioSeparatorBackend(runner, runtime, "ffprobe.exe", "ffmpeg.exe", use_gpu=True)
    backend.configure_job("job-id")

    assert backend.separate(source, output, CancellationToken()) == output
    ffmpeg_command = runner.calls[1]
    assert ffmpeg_command[ffmpeg_command.index("-i") + 1] == str(source)
    assert ffmpeg_command[ffmpeg_command.index("-ar") + 1] == "48000"
    assert ffmpeg_command[ffmpeg_command.index("-ac") + 1] == "2"
    worker_command = runner.calls[2]
    assert worker_command[worker_command.index("--input") + 1].endswith("jobs\\job-id\\temp\\separator_input.flac")
    assert worker_command[worker_command.index("--output-dir") + 1] == str(materials)
    assert worker_command[worker_command.index("--output-name") + 1] == output.name
    assert worker_command[worker_command.index("--sample-rate") + 1] == "48000"
    assert worker_command[worker_command.index("--model-path") + 1] == str(runtime.model_path)
    assert "--use-gpu" in worker_command
    assert all("UVR_Launcher.exe" not in argument for argument in worker_command)


def test_backend_cpu_fallback_does_not_request_gpu(monkeypatch, tmp_path: Path):
    materials = tmp_path / "Материалы"
    materials.mkdir()
    source = materials / "source.webm"
    output = materials / "output.flac"
    source.write_bytes(b"opus")
    runtime = RuntimeStub(tmp_path / "runtime")
    runner = SeparatorRunner()
    monkeypatch.setattr(backend_module, "local_data_root", lambda: tmp_path / "local" / "CreatorAssistant")

    AudioSeparatorBackend(runner, runtime, "ffprobe.exe", "ffmpeg.exe", use_gpu=False).separate(
        source, output, CancellationToken()
    )

    assert "--use-gpu" not in runner.calls[2]


def test_backend_rejects_non_flac_result(monkeypatch, tmp_path: Path):
    materials = tmp_path / "Материалы"
    materials.mkdir()
    source = materials / "source.webm"
    output = materials / "output.flac"
    source.write_bytes(b"opus")
    runtime = RuntimeStub(tmp_path / "runtime")
    monkeypatch.setattr(backend_module, "local_data_root", lambda: tmp_path / "local" / "CreatorAssistant")

    with pytest.raises(ValidationError, match="FLAC"):
        AudioSeparatorBackend(SeparatorRunner(output_codec="pcm_s16le"), runtime, "ffprobe.exe", "ffmpeg.exe").separate(
            source, output, CancellationToken()
        )


def test_runtime_uses_managed_localappdata_and_short_venv(monkeypatch, tmp_path: Path):
    local_root = tmp_path / "CreatorAssistant"
    monkeypatch.setattr(runtime_module, "local_data_root", lambda: local_root)
    source_model = tmp_path / "UVR-MDX-NET-Inst_HQ_3.onnx"
    source_model.write_bytes(b"model bytes")
    manager = AudioSeparatorRuntimeManager(SimpleNamespace(), source_model)

    assert manager.root == local_root / "runtimes" / "audio-separator"
    assert manager.venv_root == tmp_path / "CA" / "as"
    copied = manager.prepare_model()
    assert copied.read_bytes() == source_model.read_bytes()
    assert manager.sha256(copied) == manager.sha256(source_model)


def test_runtime_reports_gpu_only_when_cuda_is_really_available(tmp_path: Path):
    source_model = tmp_path / "source.onnx"
    source_model.write_bytes(b"model")
    manager = AudioSeparatorRuntimeManager(SimpleNamespace(), source_model, root=tmp_path / "runtime")
    manager.root.mkdir(parents=True, exist_ok=True)
    manager.marker_path.write_text(
        json.dumps(
            {
                "environment": {
                    "onnxruntime": json.dumps(
                        {
                            "cuda_available": True,
                            "providers": ["CUDAExecutionProvider", "CPUExecutionProvider"],
                        }
                    )
                }
            }
        ),
        encoding="utf-8",
    )
    assert manager.gpu_available()
    manager.marker_path.write_text(
        json.dumps({"environment": {"onnxruntime": json.dumps({"cuda_available": False, "providers": ["CPUExecutionProvider"]})}}),
        encoding="utf-8",
    )
    assert not manager.gpu_available()


def test_worker_is_headless_instrumental_only():
    worker = Path(runtime_module.__file__).resolve().parents[1] / "workers" / "audio_separator_worker.py"
    source = worker.read_text(encoding="utf-8")

    assert 'output_single_stem="Instrumental"' in source
    assert 'output_format="FLAC"' in source
    assert "UVR_Launcher.exe" not in source
    assert "pyautogui" not in source
    assert "CREATOR_JSON:" in source
