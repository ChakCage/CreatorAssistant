from __future__ import annotations

import ctypes
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from creator_assistant.infrastructure.settings_store import shared_data_root
from creator_assistant.product import DEVELOPER_AI_MODEL


OLLAMA_ENDPOINT = "http://127.0.0.1:11434"
OLLAMA_WINDOWS_URL = "https://ollama.com/download/OllamaSetup.exe"
YTDLP_RELEASE_API = "https://api.github.com/repos/yt-dlp/yt-dlp/releases/latest"
FFMPEG_ZIP_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
FFMPEG_SHA256_URL = FFMPEG_ZIP_URL + ".sha256"
MODEL_ESTIMATED_BYTES = 24 * 1024**3


@dataclass
class ComponentStatus:
    key: str
    label: str
    status: str
    value: str = ""
    details: str = ""
    automatic: bool = False


@dataclass
class DeveloperComputerReport:
    windows: str = ""
    architecture: str = ""
    cpu: str = ""
    ram_total: int = 0
    ram_available: int = 0
    gpu: str = "Не обнаружен"
    vram_total: int = 0
    nvidia_driver: str = ""
    cuda: str = ""
    system_free: int = 0
    model_free: int = 0
    ollama_path: str = ""
    ollama_models_path: str = ""
    ollama_version: str = ""
    ollama_api: bool = False
    model_installed: bool = False
    installed_models: list[dict[str, Any]] = field(default_factory=list)
    components: list[ComponentStatus] = field(default_factory=list)

    def safe_dict(self) -> dict[str, Any]:
        return asdict(self)


def _memory_status() -> tuple[int, int]:
    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]
    status = MEMORYSTATUSEX()
    status.dwLength = ctypes.sizeof(status)
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
        return subprocess.run(
            command, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


class DeveloperSetupService:
    """Standalone Developer Preview bootstrap; it has no licensing or server dependency."""

    def __init__(self, settings: dict[str, Any], dependency_resolutions: dict[str, Any] | None = None) -> None:
        self.settings = settings
        self.resolutions = dependency_resolutions or {}
        self.endpoint = OLLAMA_ENDPOINT
        self.tools_root = shared_data_root() / "SharedRuntime" / "tools"
        self.downloads_root = shared_data_root() / "SharedRuntime" / "downloads"

    @staticmethod
    def ollama_executable() -> str:
        found = shutil.which("ollama.exe") or shutil.which("ollama")
        standard = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local"))) / "Programs/Ollama/ollama.exe"
        return str(Path(found).resolve()) if found else (str(standard) if standard.is_file() else "")

    def _api(self, path: str, payload: dict[str, Any] | None = None, timeout: float = 15) -> dict[str, Any]:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(
            self.endpoint + path, data=data, headers={"Content-Type": "application/json"},
            method="POST" if data is not None else "GET",
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8").strip()
        result = json.loads(raw) if raw else {}
        if not isinstance(result, dict):
            raise RuntimeError("Ollama вернула некорректный ответ")
        return result

    def installed_models(self) -> list[dict[str, Any]]:
        models = self._api("/api/tags", timeout=10).get("models", [])
        return [item for item in models if isinstance(item, dict)] if isinstance(models, list) else []

    def inspect(self) -> DeveloperComputerReport:
        total_ram, available_ram = _memory_status()
        models_path = os.environ.get("OLLAMA_MODELS") or str(Path.home() / ".ollama" / "models")
        report = DeveloperComputerReport(
            windows=platform.platform(), architecture=platform.machine(),
            cpu=platform.processor() or os.environ.get("PROCESSOR_IDENTIFIER", "Не определён"),
            ram_total=total_ram, ram_available=available_ram,
            system_free=_free_bytes(os.environ.get("SystemDrive", "C:") + "\\"),
            model_free=_free_bytes(models_path), ollama_models_path=models_path,
            ollama_path=self.ollama_executable(),
        )
        smi = shutil.which("nvidia-smi.exe") or shutil.which("nvidia-smi")
        if smi:
            result = _run([smi, "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader,nounits"])
            if result and result.returncode == 0 and result.stdout.strip():
                fields = [item.strip() for item in result.stdout.splitlines()[0].split(",")]
                report.gpu = fields[0]
                if len(fields) > 1:
                    try:
                        report.vram_total = int(float(fields[1])) * 1024**2
                    except ValueError:
                        pass
                report.nvidia_driver = fields[2] if len(fields) > 2 else ""
                full = _run([smi])
                match = re.search(r"CUDA Version:\s*([\d.]+)", (full.stdout + full.stderr) if full else "")
                report.cuda = match.group(1) if match else ""
        try:
            report.ollama_version = str(self._api("/api/version", timeout=5).get("version", ""))
            report.ollama_api = True
            report.installed_models = self.installed_models()
            report.model_installed = any(
                str(item.get("name") or item.get("model")) == DEVELOPER_AI_MODEL
                for item in report.installed_models
            )
        except (OSError, ValueError, RuntimeError, urllib.error.URLError):
            pass

        labels = {"yt_dlp": "yt-dlp", "ffmpeg": "FFmpeg", "ffprobe": "FFprobe"}
        for key, label in labels.items():
            item = self.resolutions.get(key)
            path = str(getattr(item, "path", "") or self.settings.get(key + "_path", ""))
            report.components.append(ComponentStatus(
                key, label, "ready" if path and Path(path).is_file() else "action", path,
                "Управляемая установка доступна" if key != "ffprobe" else "Устанавливается вместе с FFmpeg",
                automatic=True,
            ))
        whisper_ready = bool(self.settings.get("whisper_python") or self.settings.get("whisper_model_dir"))
        report.components.append(ComponentStatus(
            "whisper", "Whisper", "ready" if whisper_ready else "warning",
            str(self.settings.get("whisper_model", "large-v3-turbo")),
            "Managed Whisper можно установить в настройках; не блокирует запуск.", True,
        ))
        report.components.extend(self._optional_nle_status())
        report.components.append(ComponentStatus(
            "ollama", "Ollama", "ready" if report.ollama_api else "action",
            report.ollama_version or report.ollama_path,
            f"Локальный API; модель {DEVELOPER_AI_MODEL}", True,
        ))
        report.components.append(ComponentStatus(
            "ai_model", "AI-модель", "ready" if report.model_installed else "action",
            DEVELOPER_AI_MODEL,
            f"Ориентировочная загрузка {MODEL_ESTIMATED_BYTES / 1024**3:.0f} ГБ", True,
        ))
        return report

    def _optional_nle_status(self) -> list[ComponentStatus]:
        values: list[ComponentStatus] = []
        for key, label in (("reaper", "REAPER"), ("vegas", "VEGAS Pro")):
            item = self.resolutions.get(key)
            path = str(getattr(item, "path", "") or self.settings.get(key + "_path", ""))
            values.append(ComponentStatus(
                key, label, "ready" if path and Path(path).is_file() else "optional", path,
                "Не устанавливается автоматически; путь можно выбрать вручную.", False,
            ))
        roots = [os.environ.get("ProgramFiles", ""), os.environ.get("ProgramFiles(x86)", "")]
        resolve = next((Path(root) / "Blackmagic Design/DaVinci Resolve/Resolve.exe" for root in roots
                        if root and (Path(root) / "Blackmagic Design/DaVinci Resolve/Resolve.exe").is_file()), None)
        values.append(ComponentStatus(
            "davinci", "DaVinci Resolve", "ready" if resolve else "optional", str(resolve or ""),
            "Обнаружение доступно; автоматическая установка не выполняется.", False,
        ))
        return values

    def install_yt_dlp(self, progress: Callable[[dict[str, Any]], None], cancelled: Callable[[], bool]) -> Path:
        metadata = self._json_url(YTDLP_RELEASE_API)
        assets = {str(item.get("name")): item for item in metadata.get("assets", []) if isinstance(item, dict)}
        binary = assets.get("yt-dlp.exe")
        sums = assets.get("SHA2-256SUMS")
        if not binary or not sums:
            raise RuntimeError("Официальный release yt-dlp не содержит ожидаемые assets")
        checksum_text = self._read_url(str(sums["browser_download_url"])).decode("utf-8")
        expected = next((line.split()[0] for line in checksum_text.splitlines() if line.rstrip().endswith("yt-dlp.exe")), "")
        if not re.fullmatch(r"[0-9a-fA-F]{64}", expected):
            raise RuntimeError("Не найден официальный SHA-256 для yt-dlp.exe")
        destination = self.tools_root / "yt-dlp.exe"
        self._download_verified(str(binary["browser_download_url"]), destination, expected, progress, cancelled)
        return destination

    def install_ffmpeg(self, progress: Callable[[dict[str, Any]], None], cancelled: Callable[[], bool]) -> tuple[Path, Path]:
        expected_text = self._read_url(FFMPEG_SHA256_URL).decode("utf-8").strip()
        match = re.search(r"[0-9a-fA-F]{64}", expected_text)
        if not match:
            raise RuntimeError("Источник FFmpeg не предоставил корректный SHA-256")
        archive = self.downloads_root / "ffmpeg-release-essentials.zip"
        self._download_verified(FFMPEG_ZIP_URL, archive, match.group(0), progress, cancelled)
        target = self.tools_root / "ffmpeg" / "bin"
        target.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(archive) as package:
            members = {Path(name).name.casefold(): name for name in package.namelist() if "/bin/" in name.replace("\\", "/")}
            for filename in ("ffmpeg.exe", "ffprobe.exe", "ffplay.exe"):
                member = members.get(filename)
                if not member:
                    raise RuntimeError(f"Архив FFmpeg не содержит {filename}")
                temporary = target / (filename + ".tmp")
                with package.open(member) as source, temporary.open("wb") as output:
                    shutil.copyfileobj(source, output)
                os.replace(temporary, target / filename)
        return target / "ffmpeg.exe", target / "ffprobe.exe"

    def install_ollama(self, progress: Callable[[dict[str, Any]], None], cancelled: Callable[[], bool]) -> str:
        installer = self.downloads_root / "OllamaSetup.exe"
        self._download(OLLAMA_WINDOWS_URL, installer, progress, cancelled)
        if os.name != "nt":
            raise RuntimeError("Автоматическая установка Ollama поддерживается только в Windows")
        escaped_installer = str(installer).replace("'", "''")
        command = [
            "powershell", "-NoProfile", "-NonInteractive", "-Command",
            f"(Get-AuthenticodeSignature -LiteralPath '{escaped_installer}').Status",
        ]
        signature = _run(command, timeout=30)
        if not signature or signature.returncode or signature.stdout.strip().casefold() != "valid":
            raise RuntimeError("Цифровая подпись официального OllamaSetup.exe не прошла проверку")
        result = _run([str(installer), "/SILENT", "/NORESTART"], timeout=600)
        if not result or result.returncode:
            raise RuntimeError("Официальный установщик Ollama завершился с ошибкой")
        executable = self.ollama_executable()
        if not executable:
            raise RuntimeError("Ollama установлена, но ollama.exe не найден")
        return executable

    def start_ollama(self) -> str:
        executable = self.ollama_executable()
        if not executable:
            raise RuntimeError("Ollama не установлена")
        subprocess.Popen(
            [executable, "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0),
        )
        return executable

    def pull_model(self, progress: Callable[[dict[str, Any]], None], cancelled: Callable[[], bool]) -> None:
        executable = self.ollama_executable()
        if not executable:
            raise RuntimeError("Ollama не установлена")
        process = subprocess.Popen(
            [executable, "pull", DEVELOPER_AI_MODEL], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            for line in iter(process.stdout.readline, ""):
                if cancelled():
                    process.terminate()
                    raise InterruptedError("Загрузка отменена; Ollama сохранит уже полученные слои")
                message = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", line).strip()
                percent = re.search(r"(\d{1,3})%", message)
                progress({"message": message, "percent": min(100, int(percent.group(1))) if percent else None})
            code = process.wait()
            if code:
                raise RuntimeError(f"ollama pull завершился с кодом {code}")
        finally:
            if process.poll() is None:
                process.terminate()

    def ai_smoke_test(self, timeout: float = 900) -> dict[str, Any]:
        started = time.monotonic()
        payload = {
            "model": DEVELOPER_AI_MODEL, "stream": False, "think": False, "keep_alive": "60m",
            "format": {"type": "object", "properties": {"ready": {"type": "boolean"}, "language": {"type": "string"}},
                       "required": ["ready", "language"]},
            "messages": [{"role": "user", "content": 'Верни JSON: {"ready":true,"language":"ru"}'}],
            "options": {"temperature": 0, "num_predict": 32},
        }
        response = self._api("/api/chat", payload, timeout=timeout)
        content = str((response.get("message") or {}).get("content", ""))
        try:
            parsed = json.loads(content)
        except ValueError as exc:
            raise RuntimeError("Локальная модель вернула некорректный JSON") from exc
        if parsed.get("ready") is not True or str(parsed.get("language", "")).casefold() != "ru":
            raise RuntimeError("AI smoke test не подтверждён")
        return {
            "model": DEVELOPER_AI_MODEL,
            "duration_seconds": round(time.monotonic() - started, 3),
            "tested_at": datetime.now(timezone.utc).isoformat(),
            "response": parsed,
            "eval_count": response.get("eval_count", 0),
            "eval_duration_ns": response.get("eval_duration", 0),
        }

    @staticmethod
    def _json_url(url: str) -> dict[str, Any]:
        raw = DeveloperSetupService._read_url(url)
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, dict):
            raise RuntimeError("Источник вернул некорректный JSON")
        return value

    @staticmethod
    def _read_url(url: str) -> bytes:
        request = urllib.request.Request(url, headers={"User-Agent": "CreatorAssistant-Developer-Preview"})
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.read()

    def _download_verified(
        self, url: str, destination: Path, expected_sha256: str,
        progress: Callable[[dict[str, Any]], None], cancelled: Callable[[], bool],
    ) -> None:
        self._download(url, destination, progress, cancelled)
        actual = self._sha256(destination)
        if actual.casefold() != expected_sha256.casefold():
            destination.unlink(missing_ok=True)
            raise RuntimeError("SHA-256 загруженного компонента не совпадает с опубликованным")
        progress({"message": f"SHA-256 подтверждён: {destination.name}", "percent": 100})

    def _download(
        self, url: str, destination: Path, progress: Callable[[dict[str, Any]], None],
        cancelled: Callable[[], bool],
    ) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".part")
        request = urllib.request.Request(url, headers={"User-Agent": "CreatorAssistant-Developer-Preview"})
        try:
            with urllib.request.urlopen(request, timeout=60) as response, temporary.open("wb") as output:
                total = int(response.headers.get("Content-Length", "0") or 0)
                current = 0
                while True:
                    if cancelled():
                        raise InterruptedError("Загрузка отменена")
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    output.write(chunk)
                    current += len(chunk)
                    progress({
                        "message": f"{destination.name}: {current / 1024**2:.1f} МБ",
                        "percent": int(current * 100 / total) if total else None,
                    })
            os.replace(temporary, destination)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
