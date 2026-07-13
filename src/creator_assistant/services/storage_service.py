from __future__ import annotations

import errno
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from creator_assistant.domain.errors import DiskSpaceError, GpuOutOfMemoryError, SystemMemoryError
from creator_assistant.infrastructure.settings_store import local_data_root


GIB = 1024 ** 3


@dataclass(frozen=True)
class StoragePolicy:
    reserve_bytes: int = 5 * GIB
    separator_safety_factor: float = 4.0
    download_safety_factor: float = 1.35


@dataclass(frozen=True)
class StorageInfo:
    path: Path
    volume: str
    free: int
    total: int
    writable: bool = False


def default_temp_root(youtube_root: Path) -> Path:
    root = Path(youtube_root)
    preferred = root / "Инструменты" / "CreatorAssistant_Temp"
    try:
        if root.exists() and os.access(str(root), os.W_OK):
            return preferred
    except OSError:
        pass
    return local_data_root() / "temp"


def classify_resource_failure(value) -> Optional[BaseException]:
    if isinstance(value, OSError) and (value.errno == errno.ENOSPC or getattr(value, "winerror", None) == 112):
        return DiskSpaceError("Запись файла", "", details=str(value))
    text = str(getattr(value, "details", "") or value).casefold()
    disk_markers = (
        "no space left on device", "not enough space on the disk", "disk full", "enospc",
        "winerror 112", "error_disk_full", "недостаточно места на диске",
        "невозможно записать файл из-за нехватки места",
    )
    gpu_markers = (
        "cuda out of memory", "cudnn_status_alloc_failed", "cuda allocation failure",
        "onnxruntime cuda", "failed to allocate memory for requested buffer",
    )
    ram_markers = ("memoryerror", "bad allocation", "cannot allocate memory", "insufficient memory")
    if any(marker in text for marker in gpu_markers):
        return GpuOutOfMemoryError("Недостаточно видеопамяти для выбранных параметров Audio Separator.")
    if any(marker in text for marker in disk_markers):
        return DiskSpaceError("Запись файла", "", details=str(value))
    if any(marker in text for marker in ram_markers):
        return SystemMemoryError("Недостаточно оперативной памяти.")
    return None


class StorageService:
    def __init__(self, policy: StoragePolicy = StoragePolicy(), disk_usage: Callable = shutil.disk_usage) -> None:
        self.policy = policy
        self.disk_usage = disk_usage

    @staticmethod
    def volume_for(path: Path) -> str:
        resolved = Path(path).resolve(strict=False)
        return resolved.drive or str(resolved.anchor or resolved)

    def info(self, path: Path, test_write: bool = False) -> StorageInfo:
        path = Path(path)
        probe = path
        while not probe.exists() and probe.parent != probe:
            probe = probe.parent
        usage = self.disk_usage(probe)
        writable = os.access(str(probe), os.W_OK)
        if test_write:
            path.mkdir(parents=True, exist_ok=True)
            handle, raw = tempfile.mkstemp(prefix="ca_storage_", dir=str(path))
            os.close(handle)
            marker = Path(raw)
            try:
                marker.write_bytes(b"CreatorAssistant")
                writable = marker.read_bytes() == b"CreatorAssistant"
            finally:
                marker.unlink(missing_ok=True)
        return StorageInfo(path, self.volume_for(path), usage.free, usage.total, writable)

    def require(self, operation: str, path: Path, estimated_bytes: int, reserve_bytes: Optional[int] = None) -> StorageInfo:
        reserve = self.policy.reserve_bytes if reserve_bytes is None else int(reserve_bytes)
        info = self.info(path)
        required = max(0, int(estimated_bytes))
        if info.free < required + reserve:
            raise DiskSpaceError(operation, path, required, info.free, reserve)
        return info

    def estimate_download(self, *sizes: Optional[int], existing_part: int = 0) -> int:
        raw = max(0, sum(int(value or 0) for value in sizes) - int(existing_part or 0))
        return int(raw * self.policy.download_safety_factor) if raw else GIB

    def estimate_separator(self, duration: float, sample_rate: int = 44100, channels: int = 2, bytes_per_sample: int = 4) -> int:
        pcm = max(1.0, float(duration or 0)) * sample_rate * channels * bytes_per_sample
        return int(pcm * self.policy.separator_safety_factor)


class JobTempManager:
    COMPLETED_MARKER = ".completed"

    def __init__(self, root: Path, job_id: str) -> None:
        self.root = Path(root)
        self.job_id = job_id
        self.path = self.root / job_id

    def create(self) -> Path:
        self.path.mkdir(parents=True, exist_ok=True)
        return self.path

    def environment(self) -> dict[str, str]:
        path = str(self.create())
        return {"TEMP": path, "TMP": path, "TMPDIR": path}

    def mark_completed(self) -> None:
        self.create()
        (self.path / self.COMPLETED_MARKER).write_text("completed", encoding="ascii")

    def cleanup_current(self) -> None:
        if not self.path.is_dir():
            return
        resolved = self.path.resolve()
        root = self.root.resolve()
        if resolved.parent != root or resolved == root:
            raise ValueError("Небезопасный путь временного задания.")
        shutil.rmtree(resolved)

    def size(self) -> int:
        if not self.path.is_dir():
            return 0
        return sum(path.stat().st_size for path in self.path.rglob("*") if path.is_file())
