from pathlib import Path
import json

import pytest

from creator_assistant.domain.job import CancellationToken
from creator_assistant.domain.errors import JobCancelledError
from creator_assistant.domain.errors import DiskSpaceError
from creator_assistant.domain.models import ProjectOptions, ProxyFpsPolicy, VideoFormat, VideoMetadata
from creator_assistant.infrastructure.job_store import JobStore
from creator_assistant.infrastructure.windows_paths import NamingTemplates, safe_file_name
from creator_assistant.services.project_service import ProjectService
from creator_assistant.services.reaper_service import ReaperService
from creator_assistant.services.storage_service import GIB, StoragePolicy, StorageService
from creator_assistant.services.vegas_service import VegasProjectResult
from creator_assistant.infrastructure.manifest_store import LEGACY_MANIFEST_NAME, MANIFEST_NAME, ManifestLoader, ManifestStatus
from creator_assistant.domain.errors import ValidationError


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

    def create_proxy(self, source, target, cancellation, transcode_video=True, **kwargs):
        target.write_bytes(b"proxy")
        return target

    def remux_maximum_to_mp4(self, source, target, cancellation, **kwargs):
        target.write_bytes(b"mp4")
        return target


class FakeValidator:
    def __init__(self, executable: Path):
        self.ffprobe_path = str(executable)

    def validate_video(self, path, cancellation):
        assert Path(path).stat().st_size > 0
        return {
            "streams": [
                {"codec_type": "video", "codec_name": "vp9", "width": 2560, "height": 1440, "avg_frame_rate": "60/1", "color_transfer": "bt709"},
                {"codec_type": "audio", "codec_name": "aac"},
            ],
            "format": {"duration": "12", "format_name": "mov,mp4,m4a,3gp,3g2,mj2"},
        }

    def validate_audio(self, path, cancellation):
        assert Path(path).stat().st_size > 0
        return {"streams": [{"codec_type": "audio"}], "format": {"duration": "12"}}

    def validate_expected_video(self, path, cancellation, **kwargs):
        return self.validate_video(path, cancellation)

    def validate_maximum_mp4(self, path, cancellation, **kwargs):
        data = self.validate_video(path, cancellation)
        assert Path(path).suffix.casefold() == ".mp4"
        return data

    def validate_expected_audio(self, path, cancellation, **kwargs):
        return self.validate_audio(path, cancellation)

    @staticmethod
    def duration(data):
        return float(data["format"]["duration"])


class LegacyContentValidator(FakeValidator):
    def probe(self, path, cancellation=None):
        name = Path(path).name.casefold()
        if "inst" in name:
            return {"streams": [{"codec_type": "audio", "codec_name": "flac", "sample_rate": "48000", "channels": 2}], "format": {"duration": "12", "bit_rate": "320000"}}
        if "voice" in name or Path(path).suffix.casefold() in {".m4a", ".mp3", ".wav", ".opus"}:
            return {"streams": [{"codec_type": "audio", "codec_name": "aac", "sample_rate": "48000", "channels": 2}], "format": {"duration": "12", "bit_rate": "192000"}}
        if "proxy" in name:
            return {
                "streams": [
                    {"codec_type": "video", "codec_name": "h264", "width": 1280, "height": 720, "avg_frame_rate": "60/1", "color_transfer": "bt709"},
                    {"codec_type": "audio", "codec_name": "aac", "sample_rate": "48000", "channels": 2},
                ],
                "format": {"duration": "12", "bit_rate": "2500000"},
            }
        return {
            "streams": [
                {"codec_type": "video", "codec_name": "vp9", "width": 2560, "height": 1440, "avg_frame_rate": "60/1", "color_transfer": "bt709"},
                {"codec_type": "audio", "codec_name": "opus", "sample_rate": "48000", "channels": 2},
            ],
            "format": {"duration": "12", "bit_rate": "8000000"},
        }


class ProxyProfileValidator(FakeValidator):
    def probe(self, path, cancellation=None):
        name = Path(path).name.casefold()
        height = 480 if "480p" in name else (720 if "720p" in name else 1080)
        width = {480: 854, 720: 1280, 1080: 1920}[height]
        return {
            "streams": [
                {"codec_type": "video", "codec_name": "h264", "width": width, "height": height, "avg_frame_rate": "60/1", "color_transfer": "bt709"},
                {"codec_type": "audio", "codec_name": "aac", "sample_rate": "48000", "channels": 2},
            ],
            "format": {"duration": "12", "bit_rate": "2500000"},
        }

    def validate_video(self, path, cancellation):
        return self.probe(path, cancellation)

    def validate_expected_video(self, path, cancellation, **kwargs):
        data = self.probe(path, cancellation)
        video = next(item for item in data["streams"] if item["codec_type"] == "video")
        if kwargs.get("height") is not None:
            assert video["height"] == kwargs["height"]
        return data


class FakeThumbnail:
    pass


class RecordingFfmpeg(FakeFfmpeg):
    def __init__(self, executable: Path):
        super().__init__(executable)
        self.calls = []
        self.remux_calls = []

    def create_proxy(self, source, target, cancellation, transcode_video=True, **kwargs):
        self.calls.append({"source": Path(source), "target": Path(target), **kwargs})
        target.write_bytes(b"proxy-480")
        return target

    def remux_maximum_to_mp4(self, source, target, cancellation, **kwargs):
        self.remux_calls.append({"source": Path(source), "target": Path(target), **kwargs})
        target.write_bytes(b"mp4")
        return target


class FakeSeparator:
    def available(self):
        return True

    def separate(self, source, expected_output, cancellation, on_message=None):
        expected_output.write_bytes(b"flac")
        return expected_output


class CancelSeparator(FakeSeparator):
    def separate(self, source, expected_output, cancellation, on_message=None):
        raise JobCancelledError("Операция отменена пользователем.")


class FakeVegas:
    available = True

    def __init__(self):
        self.calls = []

    @staticmethod
    def project_path(project_root, title):
        return Path(project_root) / f"{title}.veg"

    @staticmethod
    def validate_project(path):
        return Path(path).is_file() and Path(path).stat().st_size > 0

    def create_project(self, *, output, max_video, instrumental, **kwargs):
        self.calls.append((Path(max_video), Path(instrumental), dict(kwargs)))
        output.write_bytes(b"veg")
        return VegasProjectResult(
            output, 1, 1, 1, 1, 0, str(max_video), str(instrumental),
            0, 0, 2160, 3840, 60.0,
            {"success": True, "vegas_version": "22.0.248"},
        )


class FailingThenWorkingVegas(FakeVegas):
    def create_project(self, *, output, max_video, instrumental, **kwargs):
        self.calls.append((Path(max_video), Path(instrumental), dict(kwargs)))
        if len(self.calls) == 1:
            from creator_assistant.domain.errors import VegasProjectError
            raise VegasProjectError("MAX media has no video stream.")
        output.write_bytes(b"veg")
        return VegasProjectResult(
            output, 1, 1, 1, 1, 0, str(max_video), str(instrumental),
            0, 0, 1440, 2560, 60.0,
            {"success": True, "vegas_version": "22.0.248"},
        )


def test_pipeline_passes_max_and_only_instrumental_to_vegas(tmp_path: Path):
    yt = tmp_path / "yt-dlp.exe"
    ffmpeg = tmp_path / "ffmpeg.exe"
    ffprobe = tmp_path / "ffprobe.exe"
    for executable in (yt, ffmpeg, ffprobe):
        executable.write_bytes(b"exe")
    jobs = JobStore(tmp_path / "state")
    separator = FakeSeparator()
    vegas = FakeVegas()
    service = ProjectService(
        FakeYtDlp(yt), FakeFfmpeg(ffmpeg), FakeValidator(ffprobe), FakeThumbnail(),
        ReaperService(), separator, separator, jobs, NamingTemplates(), vegas=vegas,
    )
    metadata = VideoMetadata(
        "vegas-id", "Тест VEGAS", 12, "https://youtu.be/abcdefghijk",
        formats=[
            VideoFormat("maximum", "webm", height=2160, fps=60, vcodec="vp9"),
            VideoFormat("proxy", "mp4", height=720, fps=60, vcodec="avc1"),
            VideoFormat("audio", "webm", acodec="opus", abr=160),
        ],
    )
    options = ProjectOptions(create_vegas_project=True, job_id="vegas-job")

    result = service.execute(tmp_path, metadata, options, CancellationToken(), lambda _event: None)

    assert len(vegas.calls) == 1
    maximum, instrumental, arguments = vegas.calls[0]
    assert maximum == result.files["maximum"]
    assert maximum != result.files["proxy"]
    assert instrumental == result.files["instrumental"]
    assert arguments["job_id"] == "vegas-job"
    assert result.files["vegas"].is_file()
    manifest = ManifestLoader().load(result.project_path / MANIFEST_NAME).manifest
    assert manifest.files["vegas"]["role"] == "VEGAS_PROJECT"
    assert manifest.files["vegas"]["video_path"] == str(maximum)
    assert manifest.files["vegas"]["instrumental_path"] == str(instrumental)


def test_vegas_media_failure_transcodes_max_video_only_as_fallback(tmp_path: Path):
    yt = tmp_path / "yt-dlp.exe"
    ffmpeg_exe = tmp_path / "ffmpeg.exe"
    ffprobe = tmp_path / "ffprobe.exe"
    for executable in (yt, ffmpeg_exe, ffprobe):
        executable.write_bytes(b"exe")
    project = tmp_path / "Vegas Fallback"
    materials = project / "РњР°С‚РµСЂРёР°Р»С‹"
    materials.mkdir(parents=True)
    maximum = materials / "Vegas Fallback [MAX 1440p].mp4"
    instrumental = materials / "Vegas Fallback [Instrumental].flac"
    materials = project / "РњР°С‚РµСЂРёР°Р»С‹"
    materials.mkdir(parents=True, exist_ok=True)
    maximum = materials / f"{project.name} [MAX 1440p].mp4"
    instrumental = materials / f"{project.name} [Instrumental].flac"
    maximum.write_bytes(b"vp9 mp4")
    instrumental.write_bytes(b"flac")
    jobs = JobStore(tmp_path / "jobs-vegas-fallback")
    jobs.save("vegas-fallback", {
        "created_by": "CreatorAssistant",
        "status": "bound",
        "project_path": str(project),
        "files": {"maximum": str(maximum), "instrumental": str(instrumental)},
    })
    ffmpeg = RecordingFfmpeg(ffmpeg_exe)
    vegas = FailingThenWorkingVegas()
    service = ProjectService(
        FakeYtDlp(yt), ffmpeg, FakeValidator(ffprobe), FakeThumbnail(),
        ReaperService(), FakeSeparator(), FakeSeparator(), jobs, NamingTemplates(), vegas=vegas,
    )
    metadata = VideoMetadata(
        "vegas-fallback", "Vegas Fallback", 12, "https://youtu.be/vegas-fallback",
        formats=[
            VideoFormat("max", "mp4", height=1440, fps=60, vcodec="vp9", acodec="aac"),
            VideoFormat("proxy", "mp4", height=720, fps=60, vcodec="h264", acodec="aac"),
            VideoFormat("audio", "m4a", acodec="aac"),
        ],
    )
    options = ProjectOptions(
        download_maximum=False,
        create_proxy=False,
        download_audio=False,
        create_instrumental=False,
        create_reaper_project=False,
        create_vegas_project=True,
        job_id="vegas-fallback-job",
    )

    result = service.execute(tmp_path, metadata, options, CancellationToken(), lambda _event: None, project)

    assert result.files["maximum"] == maximum
    assert len(vegas.calls) == 2
    assert vegas.calls[0][0] == maximum
    assert vegas.calls[1][0] == maximum
    assert ffmpeg.remux_calls[0]["source"] == maximum
    assert ffmpeg.remux_calls[0]["target"] == maximum
    assert ffmpeg.remux_calls[0]["transcode_video"] is True
    assert jobs.load("vegas-fallback")["stages"]["maximum"]["source"] == "vegas_video_transcode_fallback"
    assert jobs.load("vegas-fallback")["stages"]["vegas"]["video_path"] == str(maximum)


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
        "Материалы", "Тест- проект.rpp", ".creator-assistant"
    }
    assert (root / MANIFEST_NAME).is_file()
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
    materials = project / "РњР°С‚РµСЂРёР°Р»С‹"
    materials.mkdir(parents=True)
    maximum = materials / "Тестовый ролик [MAX 1440p].mkv"
    materials = project / "РњР°С‚РµСЂРёР°Р»С‹"
    materials.mkdir(parents=True, exist_ok=True)
    maximum = materials / f"{project.name} [MAX 1440p].mkv"
    maximum.write_bytes(b"already downloaded")
    original_stat = maximum.stat()
    jobs = JobStore(tmp_path / "jobs")
    jobs.save("video-id", {"created_by": "CreatorAssistant", "status": "failed", "project_path": str(project), "files": {"maximum": str(maximum)}})
    yt = FakeYtDlp(yt_exe)
    separator = FakeSeparator()
    ffmpeg_service = RecordingFfmpeg(ffmpeg)
    service = ProjectService(yt, ffmpeg_service, FakeValidator(ffprobe), FakeThumbnail(), ReaperService(), separator, separator, jobs, NamingTemplates())
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
    expected_mp4 = ffmpeg_service.remux_calls[0]["target"]
    assert yt.download_count == 0
    assert result.files["maximum"] == expected_mp4
    assert expected_mp4.is_file()
    assert maximum.stat().st_mtime_ns == original_stat.st_mtime_ns
    assert ffmpeg_service.remux_calls[0]["source"] == maximum
    assert any("готовое максимальное" in event.message.casefold() for event in events)


def test_resume_detects_legacy_media_by_content_and_skips_downloads(tmp_path: Path):
    yt_exe = tmp_path / "yt-dlp.exe"
    ffmpeg = tmp_path / "ffmpeg.exe"
    ffprobe = tmp_path / "ffprobe.exe"
    for executable in (yt_exe, ffmpeg, ffprobe):
        executable.write_bytes(b"exe")
    project = tmp_path / "Beppo" / "Doing" / "Legacy Human Names"
    media = project / "Материалы"
    media.mkdir(parents=True)
    ready = {
        "maximum": media / "raw capture source.mp4",
        "proxy": media / "edit proxy.mp4",
        "audio": media / "original audio.m4a",
        "instrumental": media / "music inst.flac",
    }
    for path in ready.values():
        path.write_bytes(b"ready")
    jobs = JobStore(tmp_path / "jobs")
    jobs.save("legacy-id", {"created_by": "CreatorAssistant", "status": "failed", "project_path": str(project), "files": {}})
    yt = FakeYtDlp(yt_exe)
    separator = FakeSeparator()
    service = ProjectService(
        yt,
        FakeFfmpeg(ffmpeg),
        LegacyContentValidator(ffprobe),
        FakeThumbnail(),
        ReaperService(),
        separator,
        separator,
        jobs,
        NamingTemplates(),
    )
    metadata = VideoMetadata(
        "legacy-id", "Legacy Human Names", 12, "https://youtu.be/legacy-id",
        formats=[
            VideoFormat("maximum", "webm", height=1440, fps=60, vcodec="vp9"),
            VideoFormat("proxy", "mp4", height=720, fps=60, vcodec="avc1"),
            VideoFormat("audio", "webm", acodec="opus"),
            VideoFormat("aac", "m4a", acodec="mp4a.40.2"),
        ],
    )
    options = ProjectOptions(download_maximum=True, create_proxy=True, download_audio=True, create_instrumental=True, create_reaper_project=False)

    result = service.execute(tmp_path, metadata, options, CancellationToken(), lambda _event: None, project)

    assert yt.download_count == 0
    assert result.files == ready
    record = jobs.load("legacy-id")
    assert record["files"] == {key: str(value) for key, value in ready.items()}
    assert record["stages"]["maximum"]["source"] == "mp4_ready"
    manifest = ManifestLoader().load(project / MANIFEST_NAME).manifest
    assert manifest.files["maximum"]["path"] == str(ready["maximum"])


def test_only_vegas_mode_uses_existing_max_and_instrumental_without_other_stages(tmp_path: Path):
    yt_exe = tmp_path / "yt-dlp.exe"
    ffmpeg = tmp_path / "ffmpeg.exe"
    ffprobe = tmp_path / "ffprobe.exe"
    for executable in (yt_exe, ffmpeg, ffprobe):
        executable.write_bytes(b"exe")
    project = tmp_path / "Beppo" / "Делаю" / "Legacy Only VEGAS"
    materials = project / "Материалы"
    materials.mkdir(parents=True)
    maximum = materials / "Legacy MAX source.mp4"
    instrumental = materials / "Legacy Instrumental.flac"
    ambiguous_audio_a = materials / "A Original Audio.m4a"
    ambiguous_audio_b = materials / "B Original Audio.m4a"
    for path in (maximum, instrumental, ambiguous_audio_a, ambiguous_audio_b):
        path.write_bytes(b"ready")
    jobs = JobStore(tmp_path / "jobs-only-vegas")
    jobs.save("only-vegas", {"created_by": "CreatorAssistant", "status": "bound", "project_path": str(project), "files": {}})
    yt = FakeYtDlp(yt_exe)
    separator = FakeSeparator()
    vegas = FakeVegas()
    service = ProjectService(
        yt, FakeFfmpeg(ffmpeg), LegacyContentValidator(ffprobe), FakeThumbnail(),
        ReaperService(), separator, separator, jobs, NamingTemplates(), vegas=vegas,
    )
    item = VideoMetadata(
        "only-vegas", "Legacy Only VEGAS", 12, "https://youtu.be/only-vegas",
        formats=[
            VideoFormat("maximum", "mp4", height=1440, fps=60, vcodec="vp9"),
            VideoFormat("proxy", "mp4", height=720, fps=60, vcodec="avc1"),
            VideoFormat("audio", "m4a", acodec="aac"),
        ],
    )
    options = ProjectOptions(
        download_maximum=False,
        create_proxy=False,
        download_audio=False,
        create_instrumental=False,
        create_reaper_project=False,
        create_vegas_project=True,
    )

    result = service.execute(tmp_path, item, options, CancellationToken(), lambda _event: None, project)

    assert yt.download_count == 0
    assert len(vegas.calls) == 1
    assert vegas.calls[0][0] == maximum
    assert vegas.calls[0][1] == instrumental
    assert "proxy" not in result.files
    assert "audio" not in result.files
    assert "reaper" not in result.files
    assert result.files["vegas"].is_file()


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
    legacy_manifest_path = project / LEGACY_MANIFEST_NAME
    manifest_path = project / MANIFEST_NAME
    legacy_bytes = json.dumps({
        "schema_version": 1,
        "video_id": "5nTuu0FzAUg",
        "url": "https://youtu.be/5nTuu0FzAUg",
        "title": "100 Players Simulate Minecraft's Magical Purge",
        "project_path": str(project),
        "status": "running",
    }, ensure_ascii=False).encode("utf-8")
    legacy_manifest_path.write_bytes(legacy_bytes)
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
    assert (manifest_path.parent / "backups" / LEGACY_MANIFEST_NAME).read_bytes() == legacy_bytes
    assert not legacy_manifest_path.exists()


def test_proxy_profile_change_creates_480_from_existing_max_and_preserves_720(tmp_path: Path):
    yt = tmp_path / "yt-dlp.exe"
    ffmpeg_exe = tmp_path / "ffmpeg.exe"
    ffprobe = tmp_path / "ffprobe.exe"
    for executable in (yt, ffmpeg_exe, ffprobe):
        executable.write_bytes(b"exe")
    downloader = FakeYtDlp(yt)
    ffmpeg = RecordingFfmpeg(ffmpeg_exe)
    service = ProjectService(
        downloader, ffmpeg, ProxyProfileValidator(ffprobe), FakeThumbnail(),
        ReaperService(), FakeSeparator(), FakeSeparator(), JobStore(tmp_path / "jobs"),
        NamingTemplates(), vegas=FakeVegas(),
    )
    project = tmp_path / "Profile Project"
    materials = project / "Материалы"
    materials.mkdir(parents=True)
    metadata = VideoMetadata(
        "profile-change", project.name, 12, "https://youtu.be/profile",
        formats=[
            VideoFormat("max", "mp4", width=1920, height=1080, fps=60, vcodec="h264", acodec="aac"),
            VideoFormat("720", "mp4", width=1280, height=720, fps=60, vcodec="h264", acodec="aac"),
            VideoFormat("480", "mp4", width=854, height=480, fps=60, vcodec="h264", acodec="aac"),
            VideoFormat("audio", "m4a", acodec="aac"),
        ],
    )
    maximum = materials / safe_file_name(project.name, service.naming.maximum, "mp4", height=1080)
    proxy720 = materials / safe_file_name(project.name, service.naming.proxy, "mp4", proxy_height=720)
    maximum.write_bytes(b"maximum-1080")
    proxy720.write_bytes(b"preserve-720")
    options = ProjectOptions(
        download_maximum=False, create_proxy=True, download_audio=False,
        create_instrumental=False, create_reaper_project=False,
        create_vegas_project=False, reaper_proxy_height=480, job_id="profile-480",
    )

    before = service.inspect_existing(metadata, options, project, CancellationToken())
    assert before["proxy"]["status"] == "MISSING"
    assert "720" in before["proxy_variants"]["variants"]
    result = service.execute(
        project.parent, metadata, options, CancellationToken(), lambda _event: None, project
    )

    proxy480 = result.files["proxy"]
    assert "480p" in proxy480.name
    assert proxy480.read_bytes() == b"proxy-480"
    assert proxy720.read_bytes() == b"preserve-720"
    assert downloader.download_count == 0
    assert len(ffmpeg.calls) == 1
    assert ffmpeg.calls[0]["source"] == maximum
    assert ffmpeg.calls[0]["target"] == proxy480
    assert ffmpeg.calls[0]["maximum_height"] == 480
    manifest = ManifestLoader().load(project / MANIFEST_NAME).manifest
    assert set(manifest.reaper_proxies) >= {"480", "720"}
    assert manifest.reaper_proxies["480"]["path"] == str(proxy480)
    assert manifest.reaper_proxies["720"]["path"] == str(proxy720)
    repeated = service.inspect_existing(metadata, options, project, CancellationToken())
    assert repeated["proxy"]["status"] == "VALID"


def test_recovery_registers_existing_60fps_proxy_without_transcode(tmp_path: Path):
    yt = tmp_path / "yt-dlp.exe"
    ffmpeg_exe = tmp_path / "ffmpeg.exe"
    ffprobe = tmp_path / "ffprobe.exe"
    for executable in (yt, ffmpeg_exe, ffprobe):
        executable.write_bytes(b"exe")

    class StrictProxyValidator(ProxyProfileValidator):
        def validate_expected_video(self, path, cancellation, **kwargs):
            data = self.probe(path, cancellation)
            video = next(item for item in data["streams"] if item["codec_type"] == "video")
            if kwargs.get("fps_policy") == ProxyFpsPolicy.CAP_30:
                raise ValidationError(
                    "Proxy создан с 60 FPS, но текущий профиль требует не более 30 FPS"
                )
            if kwargs.get("height") is not None:
                assert video["height"] == kwargs["height"]
            return data

    downloader = FakeYtDlp(yt)
    ffmpeg = RecordingFfmpeg(ffmpeg_exe)
    service = ProjectService(
        downloader,
        ffmpeg,
        StrictProxyValidator(ffprobe),
        FakeThumbnail(),
        ReaperService(),
        FakeSeparator(),
        FakeSeparator(),
        JobStore(tmp_path / "jobs"),
        NamingTemplates(),
        vegas=FakeVegas(),
    )
    project = tmp_path / "Кириллица и длинный путь" / "Recovery Project"
    materials = project / "Материалы"
    materials.mkdir(parents=True)
    metadata = VideoMetadata(
        "recover-proxy",
        project.name,
        12,
        "https://youtu.be/recover",
        formats=[
            VideoFormat("max", "mp4", width=1920, height=1080, fps=60, vcodec="h264", acodec="aac"),
            VideoFormat("proxy", "mp4", width=854, height=480, fps=60, vcodec="h264", acodec="aac"),
            VideoFormat("audio", "m4a", acodec="aac"),
        ],
    )
    proxy = materials / safe_file_name(
        project.name, service.naming.proxy, "mp4", proxy_height=480
    )
    proxy.write_bytes(b"existing-60fps-proxy")
    options = ProjectOptions(
        download_maximum=False,
        create_proxy=True,
        download_audio=False,
        create_instrumental=False,
        create_reaper_project=False,
        reaper_proxy_height=480,
    )

    before = service.inspect_existing(metadata, options, project, CancellationToken())
    assert before["proxy"]["status"] == "INVALID"
    recovered = service.recover_ready_dependencies(
        metadata,
        options,
        project,
        use_existing_proxy=True,
    )

    assert recovered["proxy"]["status"] == "VALID"
    assert recovered["proxy"]["artifact_status"] == "READY"
    assert recovered["proxy"]["fps_policy"] == "PRESERVE"
    assert recovered["proxy"]["source"] == "REUSED_EXISTING"
    assert downloader.download_count == 0
    assert ffmpeg.calls == []
    manifest = ManifestLoader().load(project / MANIFEST_NAME).manifest
    assert manifest.reaper_proxies["480"]["path"] == str(proxy)
    repeated = service.inspect_existing(metadata, options, project, CancellationToken())
    assert repeated["proxy"]["status"] == "VALID"
    assert repeated["proxy"]["fps_policy"] == "PRESERVE"


def test_proxy_validation_failure_does_not_block_audio_or_uvr(tmp_path: Path):
    yt = tmp_path / "yt-dlp.exe"
    ffmpeg_exe = tmp_path / "ffmpeg.exe"
    ffprobe = tmp_path / "ffprobe.exe"
    for executable in (yt, ffmpeg_exe, ffprobe):
        executable.write_bytes(b"exe")

    class RejectCapProxyValidator(FakeValidator):
        def validate_expected_video(self, path, cancellation, **kwargs):
            if kwargs.get("fps_policy") == ProxyFpsPolicy.CAP_30:
                raise ValidationError(
                    "Proxy создан с 60 FPS, но текущий профиль требует не более 30 FPS"
                )
            return super().validate_expected_video(path, cancellation, **kwargs)

    downloader = FakeYtDlp(yt)
    jobs = JobStore(tmp_path / "jobs")
    service = ProjectService(
        downloader,
        FakeFfmpeg(ffmpeg_exe),
        RejectCapProxyValidator(ffprobe),
        FakeThumbnail(),
        ReaperService(),
        FakeSeparator(),
        FakeSeparator(),
        jobs,
        NamingTemplates(),
        vegas=FakeVegas(),
    )
    metadata = VideoMetadata(
        "continue-audio",
        "Continue Audio",
        12,
        "https://youtu.be/continue",
        formats=[
            VideoFormat("max", "mp4", height=1080, fps=60, vcodec="h264", acodec="aac"),
            VideoFormat("proxy", "mp4", height=480, fps=60, vcodec="h264", acodec="aac"),
            VideoFormat("audio", "m4a", acodec="aac"),
        ],
    )
    options = ProjectOptions(
        download_maximum=False,
        create_proxy=True,
        download_audio=True,
        create_instrumental=True,
        create_reaper_project=False,
        reaper_proxy_height=480,
    )

    with pytest.raises(ValidationError, match="proxy"):
        service.execute(
            tmp_path,
            metadata,
            options,
            CancellationToken(),
            lambda _event: None,
        )

    record = jobs.load(metadata.video_id)
    assert record["stages"]["proxy"]["status"] == "INVALID"
    assert Path(record["files"]["audio"]).is_file()
    assert Path(record["files"]["instrumental"]).is_file()
