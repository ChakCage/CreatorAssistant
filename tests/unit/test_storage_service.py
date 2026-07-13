import shutil
from pathlib import Path

import pytest

from creator_assistant.domain.errors import DiskSpaceError, GpuOutOfMemoryError, SystemMemoryError
from creator_assistant.services.storage_service import (
    GIB,
    JobTempManager,
    StoragePolicy,
    StorageService,
    classify_resource_failure,
)


def test_disk_check_accounts_for_estimate_and_reserve(tmp_path: Path):
    usage = shutil._ntuple_diskusage(total=20 * GIB, used=14 * GIB, free=6 * GIB)
    storage = StorageService(StoragePolicy(reserve_bytes=5 * GIB), disk_usage=lambda _path: usage)

    storage.require("small", tmp_path, GIB)
    with pytest.raises(DiskSpaceError) as caught:
        storage.require("large", tmp_path, 2 * GIB)

    assert caught.value.operation == "large"
    assert caught.value.required_bytes == 2 * GIB
    assert caught.value.free_bytes == 6 * GIB
    assert caught.value.reserve_bytes == 5 * GIB


def test_job_temp_environment_and_cleanup_are_scoped_to_one_job(tmp_path: Path):
    first = JobTempManager(tmp_path, "first")
    second = JobTempManager(tmp_path, "second")
    env = first.environment()
    second.create().joinpath("keep.bin").write_bytes(b"keep")
    first.path.joinpath("delete.bin").write_bytes(b"delete")

    assert env == {"TEMP": str(first.path), "TMP": str(first.path), "TMPDIR": str(first.path)}
    first.mark_completed()
    first.cleanup_current()

    assert not first.path.exists()
    assert second.path.joinpath("keep.bin").read_bytes() == b"keep"


@pytest.mark.parametrize(
    ("message", "error_type"),
    [
        ("OSError: [WinError 112] There is not enough space on the disk", DiskSpaceError),
        ("RuntimeError: CUDA out of memory", GpuOutOfMemoryError),
        ("MemoryError: cannot allocate memory", SystemMemoryError),
    ],
)
def test_resource_failures_are_classified(message, error_type):
    assert isinstance(classify_resource_failure(message), error_type)


def test_separator_estimate_uses_pcm_and_safety_factor():
    storage = StorageService(StoragePolicy(separator_safety_factor=4.0))
    assert storage.estimate_separator(10, sample_rate=48000, channels=2, bytes_per_sample=4) == 15_360_000
