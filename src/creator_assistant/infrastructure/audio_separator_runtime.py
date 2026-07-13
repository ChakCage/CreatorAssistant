from __future__ import annotations

import hashlib
import json
import shutil
import sys
import urllib.request
from pathlib import Path
from typing import Callable, Dict, Optional

from creator_assistant.infrastructure.process_runner import ProcessRunner
from creator_assistant.infrastructure.settings_store import local_data_root
from creator_assistant.domain.errors import ProcessExecutionError


PYTHON_VERSION = "3.11.9"
AUDIO_SEPARATOR_VERSION = "0.44.2"
PYTHON_INSTALLER_URL = f"https://www.python.org/ftp/python/{PYTHON_VERSION}/python-{PYTHON_VERSION}-amd64.exe"


class AudioSeparatorRuntimeManager:
    def __init__(self, runner: ProcessRunner, source_model: Path, root: Optional[Path] = None) -> None:
        self.runner = runner
        self.source_model = source_model
        self.root = root or (local_data_root() / "runtimes" / "audio-separator")
        # PyTorch wheels contain very deep license paths. Keep the physical venv
        # short to avoid WinError 206, while all runtime metadata stays in the
        # documented CreatorAssistant runtime directory above.
        self.venv_root = root or (local_data_root().parent / "CA" / "as")
        self.base_python_dir = local_data_root() / "runtimes" / "audio-separator" / "python"
        self.base_python_executable = self.base_python_dir / "python.exe"
        self.python_executable = self.venv_root / "Scripts" / "python.exe"
        self.scripts_dir = self.venv_root / "Scripts"
        self.cli_executable = self.scripts_dir / "audio-separator.exe"
        self.model_dir = local_data_root() / "models"
        self.model_path = self.model_dir / "UVR-MDX-NET-Inst_HQ_3.onnx"
        self.worker_path = self.root / "worker" / "audio_separator_worker.py"
        self.marker_path = self.root / "runtime.json"
        self.lock_path = self.root / "requirements-lock.txt"

    def is_ready(self) -> bool:
        core_ready = self.python_executable.is_file() and self.cli_executable.is_file() and self.model_path.is_file()
        if not core_ready:
            return False
        try:
            if not self.worker_path.is_file():
                self.deploy_worker()
            if not self.marker_path.is_file():
                self._write_marker(self.environment_info())
        except (OSError, ProcessExecutionError):
            return False
        return self.worker_path.is_file() and self.marker_path.is_file()

    def gpu_available(self) -> bool:
        try:
            marker = json.loads(self.marker_path.read_text(encoding="utf-8"))
            raw = marker.get("environment", {}).get("onnxruntime", "{}")
            environment = json.loads(raw)
            return bool(
                environment.get("cuda_available")
                and "CUDAExecutionProvider" in environment.get("providers", [])
            )
        except (OSError, ValueError, TypeError):
            return False

    def install(self, on_progress: Optional[Callable[[str], None]] = None) -> Dict[str, str]:
        emit = on_progress or (lambda _message: None)
        self.root.mkdir(parents=True, exist_ok=True)
        self.venv_root.mkdir(parents=True, exist_ok=True)
        downloads = local_data_root() / "downloads"
        downloads.mkdir(parents=True, exist_ok=True)
        installer = downloads / f"python-{PYTHON_VERSION}-amd64.exe"
        if not self.base_python_executable.is_file():
            emit("Скачивание управляемого Python 3.11…")
            if not installer.is_file():
                urllib.request.urlretrieve(PYTHON_INSTALLER_URL, installer)
            emit("Установка Python в LocalAppData…")
            self.runner.run(
                [str(installer), "/quiet", "InstallAllUsers=0", f"TargetDir={self.base_python_dir}", "Include_pip=1", "Include_launcher=0", "Include_test=0", "PrependPath=0", "Shortcuts=0"],
                timeout=600,
            )
        if not self.python_executable.is_file():
            emit(f"Создание короткого изолированного venv: {self.venv_root}")
            self.runner.run([str(self.base_python_executable), "-m", "venv", str(self.venv_root)], timeout=300)
        emit(f"Установка audio-separator {AUDIO_SEPARATOR_VERSION} с GPU extra…")
        self.runner.run(
            [str(self.python_executable), "-m", "pip", "install", "--disable-pip-version-check", f"audio-separator[gpu]=={AUDIO_SEPARATOR_VERSION}"],
            timeout=3600,
        )
        cuda_check = self.runner.run(
            [str(self.python_executable), "-c", "import torch; print(torch.__version__); print(torch.cuda.is_available())"],
            timeout=60,
            check=False,
        )
        if "True" not in cuda_check.output and shutil.which("nvidia-smi"):
            emit("Установка официальной CUDA 13.0 сборки PyTorch…")
            try:
                self.runner.run(
                    [str(self.python_executable), "-m", "pip", "install", "--force-reinstall", "torch==2.13.0", "torchvision==0.28.0", "--index-url", "https://download.pytorch.org/whl/test/cu130"],
                    timeout=3600,
                )
            except ProcessExecutionError:
                emit("CUDA runtime установить не удалось; сохранён полностью автоматический CPU fallback.")
        emit("Подготовка локальной модели UVR-MDX-NET Inst HQ 3…")
        self.prepare_model()
        self.deploy_worker()
        freeze = self.runner.run([str(self.python_executable), "-m", "pip", "freeze", "--all"], timeout=120)
        self.lock_path.write_text(freeze.stdout or freeze.output, encoding="utf-8")
        info = self.environment_info()
        self._write_marker(info)
        return info

    def _write_marker(self, info: Dict[str, str]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.marker_path.write_text(
            json.dumps(
                {
                    "python": PYTHON_VERSION,
                    "audio_separator": AUDIO_SEPARATOR_VERSION,
                    "model_sha256": self.sha256(self.model_path),
                    "environment": info,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    def prepare_model(self) -> Path:
        if not self.source_model.is_file():
            raise FileNotFoundError(f"Исходная UVR-модель не найдена: {self.source_model}")
        self.model_dir.mkdir(parents=True, exist_ok=True)
        source_hash = self.sha256(self.source_model)
        if not self.model_path.is_file() or self.sha256(self.model_path) != source_hash:
            temporary = self.model_path.with_suffix(".onnx.tmp")
            shutil.copy2(self.source_model, temporary)
            if self.sha256(temporary) != source_hash:
                temporary.unlink(missing_ok=True)
                raise OSError("Хеш скопированной UVR-модели не совпадает с исходником.")
            temporary.replace(self.model_path)
        return self.model_path

    def deploy_worker(self) -> Path:
        if getattr(sys, "frozen", False):
            source = Path(getattr(sys, "_MEIPASS")) / "creator_assistant" / "workers" / "audio_separator_worker.py"
        else:
            source = Path(__file__).resolve().parents[1] / "workers" / "audio_separator_worker.py"
        if not source.is_file():
            raise FileNotFoundError(f"Wrapper Audio Separator не найден: {source}")
        self.worker_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, self.worker_path)
        return self.worker_path

    def environment_info(self) -> Dict[str, str]:
        if not self.cli_executable.is_file():
            return {"status": "not_installed"}
        result = self.runner.run([str(self.cli_executable), "--env_info"], timeout=180, check=False)
        version = self.runner.run([str(self.cli_executable), "--version"], timeout=30, check=False).output.strip()
        providers = self.runner.run(
            [
                str(self.python_executable),
                "-c",
                "import json,onnxruntime as o,torch; print(json.dumps({'onnxruntime':o.__version__,'providers':o.get_available_providers(),'torch':torch.__version__,'cuda_available':torch.cuda.is_available(),'gpu':torch.cuda.get_device_name(0) if torch.cuda.is_available() else None}))",
            ],
            timeout=60,
            check=False,
        ).stdout.strip()
        return {
            "status": "ready",
            "version": version,
            "python": str(self.python_executable),
            "runtime": str(self.root),
            "env_info": result.output,
            "onnxruntime": providers,
        }

    @staticmethod
    def sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
