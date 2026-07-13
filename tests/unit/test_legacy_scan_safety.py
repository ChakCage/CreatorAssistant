import json
from pathlib import Path

from creator_assistant.domain.models import VideoFormat, VideoMetadata
from creator_assistant.domain.errors import MediaRoleResolutionRequired
from creator_assistant.infrastructure.manifest_store import (
    LEGACY_MANIFEST_NAME,
    MANIFEST_NAME,
    LegacyProjectScanner,
    ManifestLoader,
    ManifestMigrator,
    ManifestStatus,
    ManifestWriter,
    ProjectManifest,
    DiscoveredFile,
)
from creator_assistant.services.format_selector import build_format_plan
from creator_assistant.services.legacy_media_inspector import LegacyProjectMediaInspector
from creator_assistant.services.project_service import ProjectService
from creator_assistant.ui.workers import FunctionWorker


def metadata() -> VideoMetadata:
    return VideoMetadata(
        "legacy", "Legacy", 120.0, "https://youtu.be/legacy",
        formats=[
            VideoFormat("max", "mp4", width=2560, height=1440, fps=60, vcodec="vp9"),
            VideoFormat("proxy", "mp4", width=1280, height=720, fps=60, vcodec="h264"),
            VideoFormat("audio", "m4a", acodec="aac"),
        ],
    )


def media_probe(path: Path, _token=None):
    name = path.name.casefold()
    if path.suffix.casefold() in {".flac", ".m4a", ".wav", ".mp3", ".opus"}:
        return {
            "streams": [{"codec_type": "audio", "codec_name": path.suffix.lstrip("."), "sample_rate": "48000", "channels": 2}],
            "format": {"duration": "120", "format_name": path.suffix.lstrip("."), "bit_rate": "320000"},
        }
    height = 720 if "proxy" in name else 1440
    width = 1280 if height == 720 else 2560
    return {
        "streams": [
            {"codec_type": "video", "codec_name": "h264", "width": width, "height": height, "avg_frame_rate": "60/1", "color_transfer": "bt709"},
            {"codec_type": "audio", "codec_name": "aac", "sample_rate": "48000", "channels": 2},
        ],
        "format": {"duration": "120", "format_name": "mp4", "bit_rate": "8000000"},
    }


def touch(path: Path, data: bytes = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def test_sidecar_denylist_precedes_media_detection_and_ffprobe(tmp_path: Path):
    materials = tmp_path / "Материалы"
    real = touch(materials / "audio.flac")
    sidecars = [
        "audio.flac.sfk", "audio.wav.sfk", "audio.mp3.sfk", "video.mp4.sfk",
        "video.mkv.sfk", "video.mp4.sfvp0", "audio.wav.sfap0", "video.mp4.sfl",
        "video.mp4.sfdecprop", "project.rpp-bak", "project.veg.bak", "AUDIO.FLAC.SFK",
    ]
    for name in sidecars:
        touch(materials / name)
    touch(materials / "peaks" / "hidden.wav")
    calls = []
    scanner = LegacyProjectScanner(lambda path: calls.append(path) or media_probe(path))

    discovered = scanner.scan(tmp_path)

    assert [Path(item.path) for item in discovered] == [real]
    assert calls == [real]
    # Windows paths are case-insensitive, so AUDIO.FLAC.SFK overwrites audio.flac.sfk.
    assert scanner.stats["ignored_sidecar_files"] == len({name.casefold() for name in sidecars})
    assert scanner.stats["ffprobe_calls"] == 1
    assert scanner.stats["excluded_directories"] >= 1


def test_real_flac_and_sfk_produce_one_instrumental_candidate(tmp_path: Path):
    real = touch(tmp_path / "Материалы" / "Episode Instrumental.flac")
    touch(tmp_path / "Материалы" / "Episode Instrumental.flac.sfk")
    calls = []
    inspector = LegacyProjectMediaInspector(lambda path, token=None: calls.append(path) or media_probe(path))

    result = inspector.inspect(
        tmp_path, metadata(), build_format_plan(metadata().formats), proxy_height=720,
        required_roles={"instrumental"},
    )

    assert result["instrumental"].status == "VALID"
    assert Path(result["instrumental"].path) == real
    assert result["audio"].status == "NOT_REQUIRED"
    assert calls == [real]
    assert inspector.stats["ignored_sidecar_files"] == 1


def test_scanner_never_enters_media_backups_shorts_or_arbitrary_folders(tmp_path: Path):
    allowed = [touch(tmp_path / f"root-{index}.txt") for index in range(5)]
    allowed += [touch(tmp_path / "Материалы" / f"material-{index}.m4a") for index in range(6)]
    for index in range(700):
        touch(tmp_path / "Media" / f"media-{index}.wav")
    for index in range(100):
        touch(tmp_path / "Backups" / f"backup-{index}.veg")
        touch(tmp_path / "Shorts" / f"short-{index}.mp4")
    touch(tmp_path / "Arbitrary" / "nested.mp4")
    touch(tmp_path / "Материалы" / "Nested" / "nested-audio.m4a")
    calls = []
    scanner = LegacyProjectScanner(lambda path: calls.append(path) or media_probe(path))

    discovered = scanner.scan(tmp_path)

    assert {Path(item.path) for item in discovered} == set(allowed)
    assert scanner.stats["allowed_files"] == 11
    assert len(calls) == 6
    assert all("Media" not in str(path) and "Backups" not in str(path) and "Shorts" not in str(path) for path in calls)
    assert all("Nested" not in str(path) for path in calls)


def test_dependency_driven_scan_only_probes_vegas_inputs(tmp_path: Path):
    maximum = touch(tmp_path / "Материалы" / "Episode MAX source.mp4")
    instrumental = touch(tmp_path / "Материалы" / "Episode Instrumental.flac")
    touch(tmp_path / "Материалы" / "Episode Original Audio.m4a")
    touch(tmp_path / "Материалы" / "Episode proxy 720p.mp4")
    calls = []
    inspector = LegacyProjectMediaInspector(lambda path, token=None: calls.append(path) or media_probe(path))

    result = inspector.inspect(
        tmp_path, metadata(), build_format_plan(metadata().formats), proxy_height=720,
        required_roles={"maximum", "instrumental"},
    )

    assert result["maximum"].status == "VALID"
    assert result["instrumental"].status == "VALID"
    assert result["audio"].status == "NOT_REQUIRED"
    assert result["proxy"].status == "NOT_REQUIRED"
    assert set(calls) == {maximum, instrumental}


def test_original_audio_scoring_rejects_instrumental_voiceover_and_final_mix(tmp_path: Path):
    materials = tmp_path / "Материалы"
    original = touch(materials / "Episode [Audio].m4a")
    touch(materials / "Episode Instrumental.flac")
    touch(materials / "Voiceover.wav")
    touch(materials / "Final Mix.wav")
    inspector = LegacyProjectMediaInspector(media_probe)

    result = inspector.inspect(
        tmp_path, metadata(), build_format_plan(metadata().formats), proxy_height=720,
        required_roles={"audio", "instrumental"},
    )

    assert result["audio"].status == "VALID"
    assert Path(result["audio"].path) == original
    assert all("Instrumental" not in item["path"] for item in result["audio"].candidates)


def test_two_equal_original_audio_candidates_are_ambiguous(tmp_path: Path):
    touch(tmp_path / "Материалы" / "A Original Audio.m4a")
    touch(tmp_path / "Материалы" / "B Original Audio.m4a")
    result = LegacyProjectMediaInspector(media_probe).inspect(
        tmp_path, metadata(), build_format_plan(metadata().formats), proxy_height=720,
        required_roles={"audio"},
    )
    assert result["audio"].status == "AMBIGUOUS"
    assert len(result["audio"].candidates) == 2


def test_persistent_probe_cache_uses_size_and_mtime(tmp_path: Path):
    source = touch(tmp_path / "Материалы" / "Episode Instrumental.flac")
    cache = tmp_path / ".creator-assistant" / "media_probe_cache.json"
    calls = []
    for _ in range(2):
        inspector = LegacyProjectMediaInspector(lambda path, token=None: calls.append(path) or media_probe(path), cache)
        inspector.inspect(
            tmp_path, metadata(), build_format_plan(metadata().formats), proxy_height=720,
            required_roles={"instrumental"},
        )
    assert calls == [source]
    assert cache.is_file()


def manifest(project: Path) -> ProjectManifest:
    return ProjectManifest(
        video_id="id", source_url="url", title="Title", project_path=str(project),
        materials_path=str(project / "Материалы"), created_at="now", updated_at="now",
    )


def test_metadata_is_consolidated_and_legacy_manifest_is_backed_up(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    legacy = project / LEGACY_MANIFEST_NAME
    legacy.write_text(json.dumps({"schema_version": 1, "video_id": "id"}), encoding="utf-8")
    loaded = ManifestLoader().load(project / MANIFEST_NAME)
    migrated = ManifestMigrator().migrate(
        loaded, video_id="id", source_url="url", title="Title", project_path=project,
        author_preset="Author", job_id="job", discovered_files=[],
    )

    saved = ManifestWriter().write(project / MANIFEST_NAME, migrated)

    assert saved.status == ManifestStatus.VALID
    assert (project / MANIFEST_NAME).is_file()
    assert not legacy.exists()
    assert (project / ".creator-assistant" / "backups" / LEGACY_MANIFEST_NAME).is_file()


def test_legacy_creator_assistant_file_is_preserved_inside_backups(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    legacy_state = project / ".creator-assistant"
    legacy_state.write_text(json.dumps({"status": "running"}), encoding="utf-8")

    ManifestWriter().write(project / MANIFEST_NAME, manifest(project))

    metadata_dir = project / ".creator-assistant"
    assert metadata_dir.is_dir()
    assert json.loads((metadata_dir / "state.json").read_text(encoding="utf-8"))["status"] == "running"
    assert (metadata_dir / "backups" / "legacy-state").is_file()


def test_corrupted_legacy_manifest_is_not_deleted(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    legacy = project / LEGACY_MANIFEST_NAME
    legacy.write_text("{broken", encoding="utf-8")

    ManifestWriter().write(project / MANIFEST_NAME, manifest(project))

    assert legacy.read_text(encoding="utf-8") == "{broken"
    assert ManifestLoader().load(project / MANIFEST_NAME).status == ManifestStatus.VALID


def test_media_resolution_required_is_not_reported_as_failed_traceback():
    worker = FunctionWorker(lambda _progress: (_ for _ in ()).throw(MediaRoleResolutionRequired("audio", [{"path": "a.m4a"}])))
    required = []
    failed = []
    worker.media_resolution_required.connect(required.append)
    worker.failed.connect(lambda message, details: failed.append((message, details)))

    worker.run()

    assert len(required) == 1
    assert required[0].role == "audio"
    assert failed == []


def test_only_vegas_dependency_graph_does_not_require_original_audio_or_proxy():
    from creator_assistant.domain.models import ProjectOptions

    roles = ProjectService.required_roles(ProjectOptions(
        download_maximum=False,
        create_proxy=False,
        download_audio=False,
        create_instrumental=False,
        create_reaper_project=False,
        create_vegas_project=True,
    ))

    assert roles == {"maximum", "instrumental", "vegas"}


def test_scan_index_rebuild_removes_only_index_and_probe_cache(tmp_path: Path):
    project = tmp_path / "project"
    metadata_dir = project / ".creator-assistant"
    metadata_dir.mkdir(parents=True)
    manifest_path = metadata_dir / "manifest.json"
    manifest_path.write_text("keep", encoding="utf-8")
    ProjectService._write_scan_index(
        project,
        [DiscoveredFile(str(touch(project / "Preview.jpg")), "IMAGE", 1)],
        {"allowed_files": 1},
    )
    (metadata_dir / "media_probe_cache.json").write_text("{}", encoding="utf-8")

    removed = ProjectService.clear_media_index(project)

    assert removed == 2
    assert manifest_path.read_text(encoding="utf-8") == "keep"
    assert not (metadata_dir / "scan_index.json").exists()
    assert not (metadata_dir / "media_probe_cache.json").exists()
