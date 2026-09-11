from __future__ import annotations

import logging
import os
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from creator_assistant.domain.models import DependencyInfo

from .process_runner import ProcessRunner
from .settings_store import local_data_root, shared_data_root


SOURCE_SAVED = "сохранённый пользовательский путь"
SOURCE_MANAGED = "управляемая копия Creator Assistant"
SOURCE_STANDALONE = "автоматически найденный standalone EXE"
SOURCE_PATH = "PATH"
SOURCE_STANDARD = "стандартный каталог установки"
SOURCE_FALLBACK = "резервный поиск"

SETTING_KEYS = {
    "yt_dlp": "yt_dlp_path",
    "ffmpeg": "ffmpeg_path",
    "ffprobe": "ffprobe_path",
    "uvr": "uvr_path",
    "reaper": "reaper_path",
    "vegas": "vegas_path",
}


@dataclass(frozen=True)
class DependencyResolution:
    key: str
    path: str = ""
    source: str = ""
    version: str = ""

    @property
    def found(self) -> bool:
        return bool(self.path)


class DependencyDetector:
    def __init__(self, runner: ProcessRunner, logger: Optional[logging.Logger] = None) -> None:
        self.runner = runner
        self.logger = logger or logging.getLogger("creator_assistant")

    @staticmethod
    def executable(configured: str, names: Iterable[str], known: Iterable[Path] = ()) -> str:
        """Compatibility helper retained for callers and small unit tests."""
        if configured and Path(configured).is_file():
            return str(Path(configured))
        for name in names:
            found = shutil.which(name)
            if found:
                return str(Path(found))
        for path in known:
            if path.is_file():
                return str(path)
        return ""

    def discover(self, settings: Dict[str, Any]) -> Dict[str, DependencyResolution]:
        youtube_root = Path(str(settings.get("youtube_root") or r"E:\YouTube"))
        tools_root = youtube_root / "Инструменты"
        managed = shared_data_root() / "SharedRuntime" / "tools"
        app_dir = Path(sys.executable).resolve().parent
        sources = settings.get("dependency_sources", {})
        if not isinstance(sources, dict):
            sources = {}

        result: Dict[str, DependencyResolution] = {}
        result["yt_dlp"] = self._find(
            "yt_dlp",
            str(settings.get("yt_dlp_path", "")),
            str(sources.get("yt_dlp", "")),
            managed=(managed / "yt-dlp.exe", managed / "yt-dlp" / "yt-dlp.exe"),
            standalone=(
                tools_root / "yt-dlp" / "yt-dlp.exe",
                tools_root / "vdl" / "app" / "yt-dlp.exe",
                app_dir / "yt-dlp.exe",
                Path.home() / "Downloads" / "yt-dlp.exe",
            ),
            path_names=("yt-dlp.exe", "yt-dlp"),
        )
        result["ffmpeg"] = self._find(
            "ffmpeg",
            str(settings.get("ffmpeg_path", "")),
            str(sources.get("ffmpeg", "")),
            managed=(managed / "ffmpeg.exe", managed / "ffmpeg" / "bin" / "ffmpeg.exe"),
            standalone=(
                tools_root / "ffmpeg" / "bin" / "ffmpeg.exe",
                app_dir / "ffmpeg.exe",
            ),
            path_names=("ffmpeg.exe", "ffmpeg"),
            fallback=(tools_root / "vdl" / "app" / "ffmpeg.exe",),
        )
        ffmpeg = result["ffmpeg"]
        paired_ffprobe: Tuple[Path, ...] = ()
        paired_source = ""
        if ffmpeg.path:
            paired_ffprobe = (Path(ffmpeg.path).with_name("ffprobe.exe"),)
            paired_source = ffmpeg.source
        result["ffprobe"] = self._find(
            "ffprobe",
            str(settings.get("ffprobe_path", "")),
            str(sources.get("ffprobe", "")),
            managed=(managed / "ffprobe.exe", managed / "ffmpeg" / "bin" / "ffprobe.exe"),
            standalone=paired_ffprobe
            + (
                tools_root / "ffmpeg" / "bin" / "ffprobe.exe",
                app_dir / "ffprobe.exe",
            ),
            path_names=("ffprobe.exe", "ffprobe"),
            fallback=(tools_root / "vdl" / "app" / "ffprobe.exe",),
            standalone_source=paired_source or SOURCE_STANDALONE,
        )
        uvr_default = Path.home() / "AppData" / "Local" / "Programs" / "Ultimate Vocal Remover" / "UVR_Launcher.exe"
        result["uvr"] = self._find(
            "uvr",
            str(settings.get("uvr_path", "")),
            str(sources.get("uvr", "")),
            managed=(managed / "UVR_Launcher.exe",),
            standalone=(app_dir / "UVR_Launcher.exe",),
            standard=(uvr_default,),
        )
        result["reaper"] = self._find(
            "reaper",
            str(settings.get("reaper_path", "")),
            str(sources.get("reaper", "")),
            managed=(managed / "reaper.exe",),
            standalone=(app_dir / "reaper.exe",),
            path_names=("reaper.exe", "reaper"),
            standard=tuple(self._reaper_standard_paths()),
        )
        result["vegas"] = self._find(
            "vegas",
            str(settings.get("vegas_path", "")),
            str(sources.get("vegas", "")),
            managed=(managed / "vegas.exe",),
            standalone=(app_dir / "vegas220.exe", app_dir / "vegas210.exe", app_dir / "vegas200.exe"),
            path_names=("vegas220.exe", "vegas210.exe", "vegas200.exe", "vegas.exe"),
            standard=tuple(self._vegas_standard_paths()),
        )
        return result

    def apply_to_settings(
        self,
        settings: Dict[str, Any],
        resolutions: Dict[str, DependencyResolution],
    ) -> bool:
        changed = False
        source_map = settings.setdefault("dependency_sources", {})
        if not isinstance(source_map, dict):
            source_map = {}
            settings["dependency_sources"] = source_map
            changed = True
        for key, setting_key in SETTING_KEYS.items():
            resolution = resolutions[key]
            old_path = str(settings.get(setting_key, ""))
            new_path = resolution.path
            if old_path != new_path:
                settings[setting_key] = new_path
                changed = True
            if new_path:
                if source_map.get(key) != resolution.source:
                    source_map[key] = resolution.source
                    changed = True
            elif key in source_map:
                source_map.pop(key, None)
                changed = True
        return changed

    def resolve(self, settings: Dict[str, Any]) -> Dict[str, str]:
        return {key: item.path for key, item in self.discover(settings).items()}

    def _find(
        self,
        key: str,
        configured: str,
        recorded_source: str,
        *,
        managed: Sequence[Path] = (),
        standalone: Sequence[Path] = (),
        path_names: Sequence[str] = (),
        standard: Sequence[Path] = (),
        fallback: Sequence[Path] = (),
        standalone_source: str = SOURCE_STANDALONE,
    ) -> DependencyResolution:
        if configured:
            checked = self._check(key, Path(configured))
            if checked:
                source = recorded_source or SOURCE_SAVED
                return DependencyResolution(key, str(Path(configured).resolve()), source, checked)
            self.logger.warning("Сохранённый путь %s не прошёл проверку: %s", key, configured)

        groups: List[Tuple[str, Sequence[Path]]] = [
            (SOURCE_MANAGED, managed),
            (standalone_source, standalone),
        ]
        for source, candidates in groups:
            found = self._best_valid(key, candidates)
            if found:
                return self._log_found(found, source)

        path_candidates: List[Path] = []
        for name in path_names:
            value = shutil.which(name)
            if value:
                path_candidates.append(Path(value))
        found = self._best_valid(key, path_candidates)
        if found:
            return self._log_found(found, SOURCE_PATH)

        for source, candidates in ((SOURCE_STANDARD, standard), (SOURCE_FALLBACK, fallback)):
            found = self._best_valid(key, candidates)
            if found:
                return self._log_found(found, source)
        return DependencyResolution(key)

    def _best_valid(self, key: str, candidates: Sequence[Path]) -> Optional[DependencyResolution]:
        unique: Dict[str, DependencyResolution] = {}
        for candidate in candidates:
            try:
                normalized = str(candidate.expanduser().resolve())
            except OSError:
                normalized = str(candidate.expanduser())
            if normalized.casefold() in unique:
                continue
            version = self._check(key, Path(normalized))
            if version:
                unique[normalized.casefold()] = DependencyResolution(key, normalized, version=version)
        if not unique:
            return None
        if key == "yt_dlp":
            return max(unique.values(), key=lambda item: self._version_key(item.version))
        return next(iter(unique.values()))

    def _check(self, key: str, path: Path) -> str:
        if not path.is_file():
            return ""
        if key in ("uvr", "reaper", "vegas"):
            expected = "uvr" if key == "uvr" else ("reaper" if key == "reaper" else "vegas")
            if expected not in path.name.casefold():
                return ""
            return self._windows_file_version(path) or "найден"
        args = {"yt_dlp": ["--version"], "ffmpeg": ["-version"], "ffprobe": ["-version"]}[key]
        try:
            result = self.runner.run([str(path)] + args, timeout=15, check=False)
        except (OSError, Exception) as exc:
            self.logger.debug("Не удалось проверить %s: %s", path, exc)
            return ""
        output = result.output
        signatures = {
            "yt_dlp": bool(re.search(r"\d{4}[.-]\d{2}[.-]\d{2}", output)),
            "ffmpeg": "ffmpeg version" in output.casefold(),
            "ffprobe": "ffprobe version" in output.casefold(),
        }
        if result.returncode != 0 or not signatures[key]:
            return ""
        return self._parse_version(output)

    def _log_found(self, item: DependencyResolution, source: str) -> DependencyResolution:
        resolved = DependencyResolution(item.key, item.path, source, item.version)
        self.logger.info(
            "Автоматически найден %s:\n%s\nВерсия: %s\nИсточник: %s",
            item.key,
            item.path,
            item.version,
            source,
        )
        return resolved

    @staticmethod
    def _version_key(value: str) -> Tuple[int, ...]:
        numbers = re.findall(r"\d+", value)
        return tuple(int(number) for number in numbers[:6])

    @staticmethod
    def _parse_version(output: str) -> str:
        for line in output.splitlines():
            explicit = re.search(r"\bversion\s+([^\s]+)", line, re.IGNORECASE)
            if explicit:
                return explicit.group(1)
            match = re.search(r"\d{4}[.-]\d{2}[.-]\d{2}", line)
            if match:
                return match.group(0)
            match = re.search(r"\d+(?:\.\d+){1,3}(?:[-+._\w]*)?", line)
            if match:
                return match.group(0)
        return output.strip().splitlines()[0] if output.strip() else ""

    def version(self, command: List[str]) -> str:
        try:
            output = self.runner.run(command, timeout=15, check=False).output
        except OSError:
            return ""
        return self._parse_version(output)

    @staticmethod
    def uvr_model_path(uvr_path: str) -> Path:
        if not uvr_path:
            return Path()
        return Path(uvr_path).parent / "models" / "MDX_Net_Models" / "UVR-MDX-NET-Inst_HQ_3.onnx"

    def nvenc_available(self, ffmpeg_path: str) -> bool:
        if not ffmpeg_path:
            return False
        try:
            result = self.runner.run([ffmpeg_path, "-hide_banner", "-encoders"], timeout=20, check=False)
            return "h264_nvenc" in result.output
        except OSError:
            return False

    def gpu_info(self) -> tuple[str, bool, str]:
        executable = shutil.which("nvidia-smi.exe") or shutil.which("nvidia-smi")
        if not executable:
            return "Не обнаружена", False, ""
        result = self.runner.run([executable, "--query-gpu=name", "--format=csv,noheader"], timeout=15, check=False)
        name = result.output.strip().splitlines()[0] if result.output.strip() else "NVIDIA GPU"
        full = self.runner.run([executable], timeout=15, check=False)
        match = re.search(r"CUDA Version:\s*([\d.]+)", full.output, re.IGNORECASE)
        cuda_version = match.group(1) if match else ""
        return name, result.returncode == 0 and bool(cuda_version), cuda_version

    def diagnose(
        self,
        settings: Dict[str, Any],
        resolutions: Optional[Dict[str, DependencyResolution]] = None,
    ) -> List[DependencyInfo]:
        resolved = resolutions or self.discover(settings)
        items: List[DependencyInfo] = [DependencyInfo("python", "Python", "ok", sys.executable, sys.version.split()[0])]
        names = {"yt_dlp": "yt-dlp", "ffmpeg": "FFmpeg", "ffprobe": "FFprobe"}
        for key, name in names.items():
            item = resolved[key]
            if item.path:
                items.append(DependencyInfo(key, name, "ok", item.path, item.version, source=item.source))
            else:
                items.append(DependencyInfo(key, name, "error", details="Не найден"))
        uvr = resolved["uvr"]
        model = self.uvr_model_path(uvr.path)
        if uvr.path:
            model_ok = model.is_file()
            details = "Модель найдена; GUI fallback доступен" if model_ok else "Модель UVR-MDX-NET Inst HQ 3 не найдена"
            items.append(DependencyInfo("uvr", "Ultimate Vocal Remover", "ok" if model_ok else "warning", uvr.path, uvr.version, details, uvr.source))
            runtime = Path(uvr.path).parent / "python39.dll"
            runtime_version = self._windows_file_version(runtime) if runtime.is_file() else ""
            items.append(DependencyInfo("uvr_runtime", "UVR runtime", "ok" if runtime.is_file() else "warning", str(runtime) if runtime.is_file() else "", runtime_version or "Python 3.9", "Встроенный runtime PyInstaller; отдельный python.exe отсутствует", SOURCE_STANDARD))
            items.append(DependencyInfo("uvr_model", "UVR model", "ok" if model_ok else "error", str(model) if model_ok else "", "UVR-MDX-NET Inst HQ 3", "MDX-Net ONNX model", SOURCE_STANDARD))
        else:
            items.append(DependencyInfo("uvr", "Ultimate Vocal Remover", "error", details="Не найден"))
        gpu_name, cuda, cuda_version = self.gpu_info()
        nvenc = self.nvenc_available(resolved["ffmpeg"].path)
        items.append(DependencyInfo("gpu", "GPU / CUDA / NVENC", "ok" if cuda and nvenc else "warning", details=f"{gpu_name}; CUDA: {cuda_version if cuda else 'нет'}; NVENC: {'да' if nvenc else 'нет'}"))
        reaper = resolved["reaper"]
        if reaper.path:
            items.append(DependencyInfo("reaper", "REAPER", "ok", reaper.path, reaper.version, "Генератор RPP доступен", reaper.source))
        else:
            items.append(DependencyInfo("reaper", "REAPER", "warning", details="Не найден; RPP всё равно можно создать"))
        vegas = resolved.get("vegas", DependencyResolution("vegas"))
        if vegas.path:
            script_api = Path(vegas.path).with_name("ScriptPortal.Vegas.dll")
            details = (
                "ScriptPortal.Vegas.dll найден; /SCRIPT и JSON job доступны; "
                "для VEGAS 22 build 248 используется отдельный PID-защищённый процесс"
                if script_api.is_file()
                else "ScriptPortal.Vegas.dll не найден рядом с EXE"
            )
            items.append(DependencyInfo("vegas", "VEGAS Pro", "ok" if script_api.is_file() else "warning", vegas.path, vegas.version, details, vegas.source))
        else:
            items.append(DependencyInfo("vegas", "VEGAS Pro", "warning", details="Не найден; .veg проекты не будут создаваться"))
        return items

    @staticmethod
    def _reaper_standard_paths() -> Iterable[Path]:
        yielded: List[Path] = []
        if os.name == "nt":
            try:
                import winreg

                for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
                    for key_name in (
                        r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\reaper.exe",
                        r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths\reaper.exe",
                    ):
                        try:
                            with winreg.OpenKey(hive, key_name) as key:
                                value = winreg.QueryValue(key, None)
                                if value:
                                    yielded.append(Path(value))
                        except OSError:
                            pass
            except ImportError:
                pass
        for env_name in ("ProgramFiles", "ProgramFiles(x86)"):
            root = os.environ.get(env_name)
            if root:
                yielded.extend((Path(root) / "REAPER (x64)" / "reaper.exe", Path(root) / "REAPER" / "reaper.exe"))
        return yielded

    @staticmethod
    def _vegas_standard_paths() -> Iterable[Path]:
        yielded: List[Path] = []
        if os.name == "nt":
            try:
                import winreg

                for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
                    for key_name in (
                        r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\vegas220.exe",
                        r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\vegas210.exe",
                        r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\vegas200.exe",
                        r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths\vegas220.exe",
                        r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths\vegas210.exe",
                        r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths\vegas200.exe",
                    ):
                        try:
                            with winreg.OpenKey(hive, key_name) as key:
                                value = winreg.QueryValue(key, None)
                                if value:
                                    yielded.append(Path(value))
                        except OSError:
                            pass
            except ImportError:
                pass
        for env_name in ("ProgramFiles", "ProgramFiles(x86)"):
            root = os.environ.get(env_name)
            if root:
                for version in ("22.0", "21.0", "20.0"):
                    executable = "vegas" + version.split(".", 1)[0] + "0.exe"
                    yielded.append(Path(root) / "VEGAS" / f"VEGAS Pro {version}" / executable)
        return yielded

    @staticmethod
    def _windows_file_version(path: Path) -> str:
        if os.name != "nt":
            return ""
        try:
            import ctypes

            size = ctypes.windll.version.GetFileVersionInfoSizeW(str(path), None)
            if not size:
                return ""
            buffer = ctypes.create_string_buffer(size)
            ctypes.windll.version.GetFileVersionInfoW(str(path), 0, size, buffer)

            class VS_FIXEDFILEINFO(ctypes.Structure):
                _fields_ = [("dwSignature", ctypes.c_uint32), ("dwStrucVersion", ctypes.c_uint32), ("dwFileVersionMS", ctypes.c_uint32), ("dwFileVersionLS", ctypes.c_uint32), ("dwProductVersionMS", ctypes.c_uint32), ("dwProductVersionLS", ctypes.c_uint32), ("dwFileFlagsMask", ctypes.c_uint32), ("dwFileFlags", ctypes.c_uint32), ("dwFileOS", ctypes.c_uint32), ("dwFileType", ctypes.c_uint32), ("dwFileSubtype", ctypes.c_uint32), ("dwFileDateMS", ctypes.c_uint32), ("dwFileDateLS", ctypes.c_uint32)]

            pointer = ctypes.c_void_p()
            length = ctypes.c_uint()
            ctypes.windll.version.VerQueryValueW(buffer, "\\", ctypes.byref(pointer), ctypes.byref(length))
            info = ctypes.cast(pointer, ctypes.POINTER(VS_FIXEDFILEINFO)).contents
            values = [info.dwFileVersionMS >> 16, info.dwFileVersionMS & 0xFFFF, info.dwFileVersionLS >> 16, info.dwFileVersionLS & 0xFFFF]
            while len(values) > 2 and values[-1] == 0:
                values.pop()
            return ".".join(str(value) for value in values)
        except Exception:
            return ""
