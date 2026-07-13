from pathlib import Path
import json

import pytest

from creator_assistant.domain.job import CancellationToken
from creator_assistant.domain.errors import JobCancelledError
from creator_assistant.domain.errors import DiskSpaceError
from creator_assistant.domain.models import ProjectOptions, VideoFormat, VideoMetadata
from creator_assistant.infrastructure.job_store import JobStore
from creator_assistant.infrastructure.windows_paths import NamingTemplates
from creator_assistant.services.project_service import ProjectService
from creator_assistant.services.reaper_service import ReaperService
from creator_assistant.services.storage_service import GIB, StoragePolicy, StorageService
from creator_assistant.infrastructure.manifest_store import MANIFEST_NAME, ManifestLoader, ManifestStatus


class FakeYtDlp:
    def __init__(self, executable: Path):
        self.executable = str(executable)
        self.download_count = 0
        self.selectors = []

    def download(self, url, selector, output_template, cancellation, stage, merge_container=None, on_progress=None, **kwargs):
        self.download_count += 1
        self.selectors.append(selector)
        extension = "webm" if selector == "audio" else (merge_container or "mp4")
        target = Path(str(output_template).replace("%(ext)s", extension))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"media")
        return target


class FakeFfmpeg:
    def __init__(self, executable: Path):
        self.ffmpeg_path = str(executable)

    def create_proxy(self, source, target, cancellation, transcode_video=True):
        target.write_bytes(b"proxy")
        return target


class FakeValidator:
    def __init__(self, executable: Path):
        self.ffprobe_path = str(executable)

    def validate_video(self, path, cancellation):
        assert Path(path).stat().st_size > 0
        return {"streams": [{"codec_type": "video"}], "format": {"duration": "12"}}

    def validate_audio(self, path, cancellation):
        assert Path(path).stat().st_size > 0
        return {"streams": [{"codec_type": "audio"}], "format": {"duration": "12"}}

    def validate_expected_video(self, path, cancellation, **kwargs):
        return self.validate_video(path, cancellation)

    def validate_expected_audio(self, path, cancellation, **kwargs):
        return self.validate_audio(path, cancellation)

    @staticmethod
    def duration(data):
        return float(data["format"]["duration"])


class FakeThumbnail:
    pass


class FakeSeparator:
    def available(self):
        return True

    def separate(self, source, expected_output, cancellation, on_message=None):
        expected_output.write_bytes(b"flac")
        return expected_output


class CancelSeparator(FakeSeparator):
    def separate(self, source, expected_output, cancellation, on_message=None):
        raise JobCancelledError("Операция отменена пользователем.")


def test_fake_disk_full_sets_waiting_state_and_preserves_job_temp(tmp_path: Path):
    yt = tmp_path / "yt-dlp.exe"
    ffmpeg = tmp_path / "ffmpeg.exe"
    ffprobe = tmp_path / "ffprobe.exe"
    for executable in (yt, ffmpeg, ffprobe):
        executable.write_bytes(b"exe")
    usage = __import__("shutil")._ntuple_diskusage(total=10 * GIB, used=5 * GIB, free=5 * GIB)
    storage = StorageService(StoragePolicy(reserve_bytes=5 * GIB), disk_usage=lambda _path: usage)
    jobs = JobStore(tmp_path / "state")
    separator = FakeSeparator()
    service = ProjectService(
        FakeYtDlp(yt), FakeFfmpeg(ffmpeg), FakeValidator(ffprobe), FakeThumbnail(),
        ReaperService(), separator, separator, jobs, NamingTemplates(), storage=storage,
    )
    item = VideoMetadata(
        "disk-full", "Disk full", 12, "https://youtu.be/abcdefghijk",
        formats=[
            VideoFormat("max", "mp4", height=1080, fps=30, vcodec="avc1"),
            VideoFormat("proxy", "mp4", height=480, fps=30, vcodec="avc1"),
            VideoFormat("audio", "m4a", acodec="mp4a.40.2"),
        ],
    )
    options = ProjectOptions(
        download_maximum=False, create_proxy=False, download_audio=False,
        create_instrumental=False, create_reaper_project=False,
        temp_root=str(tmp_path / "job-temp"), job_id="job-one",
    )

    with pytest.raises(DiskSpaceError):
        service.execute(tmp_path, item, options, CancellationToken(), lambda _event: None)

    record = jobs.load("disk-full")
    assert record["status"] == "waiting_for_disk_space"
    assert record["disk_space"]["reserve_bytes"] == 5 * GIB
    assert Path(record["temp_path"]).is_dir()


def test_full_pipeline_creates_only_expected_structure(tmp_path: Path):
    yt = tmp_path / "yt-dlp.exe"
    ffmpeg = tmp_path / "ffmpeg.exe"
    ffprobe = tmp_path / "ffprobe.exe"
    for executable in (yt, ffmpeg, ffprobe):
        executable.write_bytes(b"exe")
    jobs = JobStore(tmp_path / "state")
    separator = FakeSeparator()
    service = ProjectService(
        FakeYtDlp(yt),
        FakeFfmpeg(ffmpeg),
        FakeValidator(ffprobe),
        FakeThumbnail(),
        ReaperService(),
        separator,
        separator,
        jobs,
        NamingTemplates(),
    )
    metadata = VideoMetadata(
        "video-id",
        "Тест: проект",
        12,
        "https://youtu.be/abcdefghijk",
        formats=[
            VideoFormat("maximum", "webm", height=2160, fps=60, vcodec="vp9"),
            VideoFormat("proxy", "mp4", height=720, fps=60, vcodec="avc1"),
            VideoFormat("audio", "webm", acodec="opus", abr=160),
            VideoFormat("aac", "m4a", acodec="mp4a.40.2", abr=128),
        ],
    )
    events = []
    result = service.execute(tmp_path, metadata, ProjectOptions(), CancellationToken(), events.append)
    assert result.project_path is not None
    root = result.project_path
    assert {path.name for path in root.iterdir()} == {
        "Материалы", "Тест- проект.rpp", ".creator-assistant.json"
    }
    assert len(list((root / "Материалы").iterdir())) == 4
    assert set(result.files) == {"maximum", "proxy", "audio", "instrumental", "reaper"}
    assert events[-1].percent == 100
    assert jobs.load("video-id")["status"] == "completed"


def test_resume_finds_valid_existing_maximum_and_does_not_download_again(tmp_path: Path):
    yt_exe = tmp_path / "yt-dlp.exe"
    ffmpeg = tmp_path / "ffmpeg.exe"
    ffprobe = tmp_path / "ffprobe.exe"
    for executable in (yt_exe, ffmpeg, ffprobe):
        executable.write_bytes(b"exe")
    project = tmp_path / "Делаю" / "Тестовый ролик"
    materials = project / "Материалы"
    materials.mkdir(parents=True)
    maximum = materials / "Тестовый ролик [MAX 1440p].mkv"
    maximum.write_bytes(b"already downloaded")
    original_stat = maximum.stat()
    jobs = JobStore(tmp_path / "jobs")
    jobs.save("video-id", {"created_by": "CreatorAssistant", "status": "failed", "project_path": str(project), "files": {}})
    yt = FakeYtDlp(yt_exe)
    separator = FakeSeparator()
    service = ProjectService(yt, FakeFfmpeg(ffmpeg), FakeValidator(ffprobe), FakeThumbnail(), ReaperService(), separator, separator, jobs, NamingTemplates())
    metadata = VideoMetadata(
        "video-id",
        "Тестовый ролик",
        12,
        "https://youtu.be/abcdefghijk",
        formats=[
            VideoFormat("maximum", "webm", height=1440, fps=60, vcodec="vp9"),
            VideoFormat("audio", "webm", acodec="opus"),
            VideoFormat("proxy", "mp4", height=720, fps=60, vcodec="avc1"),
            VideoFormat("aac", "m4a", acodec="mp4a.40.2"),
        ],
    )
    options = ProjectOptions(download_maximum=True, create_proxy=False, download_audio=False, create_instrumental=False, create_reaper_project=False)
    events = []
    result = service.execute(tmp_path, metadata, options, CancellationToken(), events.append, project)
    assert yt.download_count == 0
    assert result.files["maximum"] == maximum
    assert maximum.stat().st_mtime_ns == original_stat.st_mtime_ns
    assert any("готовое максимальное" in event.message.casefold() for event in events)


def test_resume_preserves_last_compatible_alternative_format_id(tmp_path: Path):
    yt_exe = tmp_path / "yt-dlp.exe"
    ffmpeg = tmp_path / "ffmpeg.exe"
    ffprobe = tmp_path / "ffprobe.exe"
    for executable in (yt_exe, ffmpeg, ffprobe):
        executable.write_bytes(b"exe")
    project = tmp_path / "Делаю" / "Тестовый ролик"
    materials = project / "Материалы"
    materials.mkdir(parents=True)
    partial = materials / "Тестовый ролик [MAX 1440p].f400.mp4.part"
    partial.write_bytes(b"saved AV1 bytes")
    jobs = JobStore(tmp_path / "jobs")
    jobs.save("video-id", {
        "created_by": "CreatorAssistant",
        "status": "media_forbidden",
        "project_path": str(project),
        "files": {},
        "stages": {"maximum": {
            "status": "PARTIAL",
            "video_id": "video-id",
            "role": "MAX_VIDEO",
            "format_id": "400+251",
            "output_template": str(materials / "Тестовый ролик [MAX 1440p].%(ext)s"),
        }},
    })
    yt = FakeYtDlp(yt_exe)
    separator = FakeSeparator()
    service = ProjectService(yt, FakeFfmpeg(ffmpeg), FakeValidator(ffprobe), FakeThumbnail(), ReaperService(), separator, separator, jobs, NamingTemplates())
    metadata = VideoMetadata(
        "video-id", "Тестовый ролик", 12, "https://youtu.be/abcdefghijk",
        formats=[
            VideoFormat("308", "webm", height=1440, fps=60, vcodec="vp9", dynamic_range="SDR"),
            VideoFormat("400", "mp4", height=1440, fps=60, vcodec="av01", dynamic_range="SDR"),
            VideoFormat("298", "mp4", height=720, fps=60, vcodec="avc1", dynamic_range="SDR"),
            VideoFormat("251", "webm", acodec="opus"),
            VideoFormat("140", "m4a", acodec="mp4a.40.2"),
        ],
    )
    options = ProjectOptions(download_maximum=True, create_proxy=False, download_audio=False, create_instrumental=False, create_reaper_project=False)
    service.execute(tmp_path, metadata, options, CancellationToken(), lambda _event: None, project)
    assert yt.selectors[0] == "400+251"
    assert partial.is_file()


def test_uvr_cancellation_preserves_ready_files_and_job_state(tmp_path: Path):
    yt_exe = tmp_path / "yt-dlp.exe"
    ffmpeg = tmp_path / "ffmpeg.exe"
    ffprobe = tmp_path / "ffprobe.exe"
    for executable in (yt_exe, ffmpeg, ffprobe):
        executable.write_bytes(b"exe")
    project = tmp_path / "Делаю" / "Тестовый ролик"
    materials = project / "Материалы"
    materials.mkdir(parents=True)
    ready = {
        "maximum": materials / "Тестовый ролик [MAX 1440p].mkv",
        "proxy": materials / "Тестовый ролик [720p].mp4",
        "audio": materials / "Тестовый ролик [Audio].webm",
    }
    for path in ready.values():
        path.write_bytes(b"ready media")
    stats = {key: (path.stat().st_size, path.stat().st_mtime_ns) for key, path in ready.items()}
    jobs = JobStore(tmp_path / "jobs")
    jobs.save("video-id", {"created_by": "CreatorAssistant", "status": "failed", "project_path": str(project), "files": {}})
    yt = FakeYtDlp(yt_exe)
    separator = CancelSeparator()
    service = ProjectService(yt, FakeFfmpeg(ffmpeg), FakeValidator(ffprobe), FakeThumbnail(), ReaperService(), separator, separator, jobs, NamingTemplates())
    metadata = VideoMetadata(
        "video-id", "Тестовый ролик", 12, "https://youtu.be/abcdefghijk",
        formats=[
            VideoFormat("maximum", "webm", height=1440, fps=60, vcodec="vp9"),
            VideoFormat("proxy", "mp4", height=720, fps=60, vcodec="avc1"),
            VideoFormat("audio", "webm", acodec="opus"),
            VideoFormat("aac", "m4a", acodec="mp4a.40.2"),
        ],
    )
    options = ProjectOptions(download_maximum=True, create_proxy=True, download_audio=True, create_instrumental=True, create_reaper_project=False)
    with pytest.raises(JobCancelledError):
        service.execute(tmp_path, metadata, options, CancellationToken(), lambda _event: None, project)
    assert yt.download_count == 0
    assert jobs.load("video-id")["status"] == "cancelled"
    assert jobs.load("video-id")["stages"]["instrumental"]["status"] == "CANCELLED"
    for key, path in ready.items():
        assert (path.stat().st_size, path.stat().st_mtime_ns) == stats[key]


def test_legacy_manifest_migrate_and_resume_five_times_without_download_or_data_loss(tmp_path: Path):
    yt_exe = tmp_path / "yt-dlp.exe"
    ffmpeg = tmp_path / "ffmpeg.exe"
    ffprobe = tmp_path / "ffprobe.exe"
    for executable in (yt_exe, ffmpeg, ffprobe):
        executable.write_bytes(b"exe")
    project = tmp_path / "MylesMC" / "Делаю" / "100 Players Simulate Minecraft's Magical Purge"
    project.mkdir(parents=True)
    user_file = project / "Mylesoru.psd"
    user_file.write_bytes(b"real-user-data")
    manifest_path = project / MANIFEST_NAME
    legacy_bytes = json.dumps({
        "schema_version": 1,
        "video_id": "5nTuu0FzAUg",
        "url": "https://youtu.be/5nTuu0FzAUg",
        "title": "100 Players Simulate Minecraft's Magical Purge",
        "project_path": str(project),
        "status": "running",
    }, ensure_ascii=False).encode("utf-8")
    manifest_path.write_bytes(legacy_bytes)
    jobs = JobStore(tmp_path / "jobs")
    yt = FakeYtDlp(yt_exe)
    separator = FakeSeparator()
    service = ProjectService(
        yt, FakeFfmpeg(ffmpeg), FakeValidator(ffprobe), FakeThumbnail(),
        ReaperService(), separator, separator, jobs, NamingTemplates(),
    )
    metadata = VideoMetadata(
        "5nTuu0FzAUg", "100 Players Simulate Minecraft's Magical Purge", 12,
        "https://youtu.be/5nTuu0FzAUg",
        formats=[
            VideoFormat("maximum", "webm", height=1440, fps=60, vcodec="vp9"),
            VideoFormat("proxy", "mp4", height=720, fps=60, vcodec="avc1"),
            VideoFormat("audio", "webm", acodec="opus"),
            VideoFormat("aac", "m4a", acodec="mp4a.40.2"),
        ],
    )
    options = ProjectOptions(
        download_maximum=False, create_proxy=False, download_audio=False,
        create_instrumental=False, create_reaper_project=False,
    )
    for index in range(5):
        service.migrate_existing(
            metadata, project, job_id=f"migration-{index}", author_preset="MylesMC",
            cancellation=CancellationToken(), on_progress=lambda _event: None,
        )
        assert ManifestLoader().load(manifest_path).status == ManifestStatus.VALID
        result = service.execute(
            project.parent, metadata, options, CancellationToken(), lambda _event: None, project
        )
        assert result.project_path == project
        assert user_file.read_bytes() == b"real-user-data"
    assert yt.download_count == 0
    assert manifest_path.with_name(manifest_path.name + ".bak").read_bytes() == legacy_bytes
