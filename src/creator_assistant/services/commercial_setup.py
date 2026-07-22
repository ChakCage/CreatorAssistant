from __future__ import annotations

import ctypes
import json
import os
import platform
import shutil
import subprocess
import time
import re
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable


OLLAMA_ENDPOINT = "http://127.0.0.1:11434"


@dataclass(frozen=True)
class CommercialAIProfile:
    id: str
    display_name: str
    model_id: str
    estimated_bytes: int
    quality: str
    requirements: str


AI_PROFILES = {
    "compact": CommercialAIProfile(
        "compact", "Компактная модель", "qwen3:14b", 10 * 1024**3,
        "Быстрее скачивается; анализ длинных видео может быть менее точным.",
        "Желательно 16 ГБ RAM; может работать на CPU, но медленно.",
    ),
    "maximum_quality": CommercialAIProfile(
        "maximum_quality", "Максимальное качество", "qwen3.6:35b-a3b", 24 * 1024**3,
        "Лучше понимает сюжет длинных роликов, но работает медленнее.",
        "Рекомендуется 32 ГБ RAM, NVIDIA GPU и около 16 ГБ VRAM.",
    ),
}


@dataclass
class ComponentStatus:
    key: str
    label: str
    status: str
    value: str = ""
    details: str = ""


@dataclass
class ComputerReport:
    windows: str = ""
    architecture: str = ""
    cpu: str = ""
    ram_total: int = 0
    ram_available: int = 0
    gpu: str = "Не обнаружен"
    gpu_vendor: str = ""
    vram_total: int = 0
    nvidia_driver: str = ""
    cuda: str = ""
    system_free: int = 0
    ollama_free: int = 0
    projects_free: int = 0
    ollama_path: str = ""
    ollama_models_path: str = ""
    ollama_version: str = ""
    ollama_api: bool = False
    installed_models: list[dict[str, Any]] = field(default_factory=list)
    components: list[ComponentStatus] = field(default_factory=list)
    recommendation: str = "compact"

    def safe_dict(self) -> dict[str, Any]:
        value = asdict(self)
        # Executable/model paths are useful; user video paths, secrets and content are never collected.
        return value


def _memory_status() -> tuple[int, int]:
    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]
    status = MEMORYSTATUSEX(); status.dwLength = ctypes.sizeof(status)
    try:
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return int(status.ullTotalPhys), int(status.ullAvailPhys)
    except (AttributeError, OSError):
        pass
    return 0, 0


def _free_bytes(path: str | Path) -> int:
    try:
        candidate = Path(path).expanduser()
        while not candidate.exists() and candidate != candidate.parent:
            candidate = candidate.parent
        return int(shutil.disk_usage(candidate).free)
    except OSError:
        return 0


def _run(command: list[str], timeout: float = 15) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired):
        return None


class CommercialSetupService:
    """Commercial-only component discovery and explicit local-AI setup operations."""

    def __init__(self, settings: dict[str, Any], dependency_resolutions: dict[str, Any] | None = None) -> None:
        self.settings = settings
        self.resolutions = dependency_resolutions or {}
        self.endpoint = OLLAMA_ENDPOINT

    @staticmethod
    def profile(profile_id: str) -> CommercialAIProfile:
        return AI_PROFILES.get(profile_id, AI_PROFILES["maximum_quality"])

    @staticmethod
    def profile_for_model(model_id: str) -> CommercialAIProfile:
        return next((item for item in AI_PROFILES.values() if item.model_id == model_id), AI_PROFILES["maximum_quality"])

    def selected_profile(self) -> CommercialAIProfile:
        setup = self.settings.get("commercial_setup", {})
        return self.profile(str(setup.get("model_profile", "maximum_quality")))

    @staticmethod
    def ollama_executable() -> str:
        found = shutil.which("ollama.exe") or shutil.which("ollama")
        standard = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local"))) / "Programs/Ollama/ollama.exe"
        return str(Path(found).resolve()) if found else (str(standard) if standard.is_file() else "")

    def _api(self, path: str, payload: dict[str, Any] | None = None, timeout: float = 15, method: str = "") -> dict[str, Any]:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(self.endpoint + path, data=data,
                                         headers={"Content-Type": "application/json"},
                                         method=method or ("POST" if data is not None else "GET"))
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8").strip()
            result = json.loads(raw) if raw else {}
        if not isinstance(result, dict):
            raise RuntimeError("Ollama вернула некорректный ответ")
        return result

    def api_version(self) -> str:
        return str(self._api("/api/version", timeout=5).get("version", ""))

    def installed_models(self) -> list[dict[str, Any]]:
        models = self._api("/api/tags", timeout=10).get("models", [])
        return [item for item in models if isinstance(item, dict)] if isinstance(models, list) else []

    def inspect(self) -> ComputerReport:
        total_ram, available_ram = _memory_status()
        setup = self.settings.get("commercial_setup", {})
        projects = setup.get("projects_folder") or self.settings.get("youtube_root") or Path.home()
        models_path = os.environ.get("OLLAMA_MODELS", "")
        if not models_path:
            models_path = str(Path.home() / ".ollama" / "models")
        system_drive = os.environ.get("SystemDrive", "C:") + "\\"
        report = ComputerReport(
            windows=platform.platform(), architecture=platform.machine(),
            cpu=platform.processor() or os.environ.get("PROCESSOR_IDENTIFIER", "Не определён"),
            ram_total=total_ram, ram_available=available_ram,
            system_free=_free_bytes(system_drive), ollama_free=_free_bytes(models_path),
            projects_free=_free_bytes(projects), ollama_path=self.ollama_executable(),
            ollama_models_path=models_path,
        )
        smi = shutil.which("nvidia-smi.exe") or shutil.which("nvidia-smi")
        if smi:
            result = _run([smi, "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader,nounits"])
            if result and result.returncode == 0 and result.stdout.strip():
                fields = [part.strip() for part in result.stdout.splitlines()[0].split(",")]
                report.gpu = fields[0]; report.gpu_vendor = "NVIDIA"
                if len(fields) > 1:
                    try: report.vram_total = int(float(fields[1])) * 1024**2
                    except ValueError: pass
                report.nvidia_driver = fields[2] if len(fields) > 2 else ""
                full = _run([smi])
                if full:
                    import re
                    match = re.search(r"CUDA Version:\s*([\d.]+)", full.stdout + full.stderr)
                    report.cuda = match.group(1) if match else ""
        try:
            report.ollama_version = self.api_version(); report.ollama_api = True
            report.installed_models = self.installed_models()
        except (OSError, ValueError, RuntimeError, urllib.error.URLError):
            report.ollama_api = False
        for key, label in (("ffmpeg", "FFmpeg"), ("ffprobe", "FFprobe")):
            item = self.resolutions.get(key)
            path = str(getattr(item, "path", "") or self.settings.get(key + "_path", ""))
            report.components.append(ComponentStatus(key, label, "ready" if path and Path(path).is_file() else "action", path))
        whisper_ready = bool(self.settings.get("whisper_python") or self.settings.get("whisper_model_dir"))
        report.components.append(ComponentStatus("whisper", "Whisper", "ready" if whisper_ready else "warning",
                                                 str(self.settings.get("whisper_model", ""))))
        report.components.append(ComponentStatus("ollama", "Ollama API", "ready" if report.ollama_api else "action",
                                                 report.ollama_version or report.ollama_path))
        enough_ram = total_ram >= 28 * 1024**3
        enough_vram = report.vram_total >= 14 * 1024**3
        enough_space = report.ollama_free >= 32 * 1024**3
        report.recommendation = "maximum_quality" if enough_ram and enough_vram and enough_space else "compact"
        return report

    def apply_profile(self, profile_id: str) -> CommercialAIProfile:
        profile = self.profile(profile_id)
        setup = self.settings.setdefault("commercial_setup", {})
        setup["model_profile"] = profile.id
        ai = self.settings.setdefault("shorts_ai", {})
        ai.update({"enabled": True, "backend": "ollama", "endpoint": self.endpoint,
                   "model": profile.model_id, "strict_model": True, "fallback": False})
        return profile

    def model_installed(self, model_id: str) -> bool:
        return any(str(item.get("name") or item.get("model")) == model_id for item in self.installed_models())

    def pull_model(self, model_id: str, progress: Callable[[dict[str, Any]], None], cancelled: Callable[[], bool]) -> None:
        if model_id not in {item.model_id for item in AI_PROFILES.values()}:
            raise ValueError(f"Неподдерживаемая Commercial модель: {model_id}")
        executable = self.ollama_executable()
        if not executable:
            raise RuntimeError("Ollama не установлена")
        process = subprocess.Popen([executable, "pull", model_id], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True, encoding="utf-8", errors="replace", bufsize=1,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            for line in iter(process.stdout.readline, ""):
                if cancelled():
                    process.terminate(); raise InterruptedError("Загрузка отменена; уже загруженные слои сохранены Ollama")
                message = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", line).strip()
                match = re.search(r"(\d{1,3})%", message)
                progress({"model": model_id, "message": message,
                          "percent": min(100, int(match.group(1))) if match else None,
                          "free_bytes": _free_bytes(os.environ.get("OLLAMA_MODELS", Path.home()))})
            code = process.wait()
            if code:
                raise RuntimeError(f"ollama pull завершился с кодом {code}")
        finally:
            if process.poll() is None: process.terminate()

    def start_ollama(self) -> str:
        executable = self.ollama_executable()
        if not executable: raise RuntimeError("Ollama не установлена")
        subprocess.Popen([executable, "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0))
        return executable

    def remove_model(self, model_id: str) -> None:
        if model_id not in {item.model_id for item in AI_PROFILES.values()}:
            raise ValueError("Удаление разрешено только для выбранной поддерживаемой модели")
        running = self._api("/api/ps", timeout=10).get("models", [])
        if any(str(item.get("name") or item.get("model")) == model_id for item in running if isinstance(item, dict)):
            raise RuntimeError("Модель сейчас используется активным процессом; завершите анализ или выгрузите её")
        self._api("/api/delete", {"model": model_id}, timeout=60, method="DELETE")

    def structured_preflight(self, model_id: str, timeout: float = 900) -> dict[str, Any]:
        if model_id not in {item.model_id for item in AI_PROFILES.values()}:
            raise ValueError("Выбрана неподдерживаемая модель")
        started = time.monotonic()
        payload = {"model": model_id, "stream": False, "think": False, "keep_alive": "60m",
                   "format": {"type": "object", "properties": {"ready": {"type": "boolean"}, "language": {"type": "string"}},
                              "required": ["ready", "language"]},
                   "messages": [{"role": "user", "content": 'Верни JSON, где ready=true и language="ru".'}],
                   "options": {"temperature": 0, "num_predict": 32}}
        response = self._api("/api/chat", payload, timeout=timeout)
        content = str((response.get("message") or {}).get("content", ""))
        try: parsed = json.loads(content)
        except ValueError as exc: raise RuntimeError("Модель вернула некорректный JSON") from exc
        if parsed.get("ready") is not True:
            raise RuntimeError("Structured-output проверка не подтверждена")
        runtime = {}
        try:
            running = self._api("/api/ps", timeout=10).get("models", [])
            runtime = next((item for item in running if str(item.get("name") or item.get("model")) == model_id), {})
        except (OSError, ValueError, urllib.error.URLError):
            pass
        return {"model_id": model_id, "duration_seconds": round(time.monotonic() - started, 3),
                "tested_at": datetime.now(timezone.utc).isoformat(), "response": parsed,
                "runtime": runtime, "total_duration_ns": response.get("total_duration", 0),
                "eval_count": response.get("eval_count", 0), "eval_duration_ns": response.get("eval_duration", 0)}

    @staticmethod
    def write_report(report: dict[str, Any], destination: Path) -> tuple[Path, Path]:
        destination.mkdir(parents=True, exist_ok=True)
        json_path = destination / "commercial-diagnostics.json"
        text_path = destination / "commercial-diagnostics.txt"
        json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        lines = [f"{key}: {value}" for key, value in report.items() if not isinstance(value, (dict, list))]
        text_path.write_text("\n".join(lines), encoding="utf-8")
        return json_path, text_path
