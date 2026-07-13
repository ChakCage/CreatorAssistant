import json
from pathlib import Path

import pytest

from creator_assistant.infrastructure.manifest_store import (
    CURRENT_SCHEMA_VERSION,
    MANIFEST_NAME,
    LegacyProjectScanner,
    ManifestLoader,
    ManifestMigrator,
    ManifestStatus,
    ManifestWriter,
    ProjectManifest,
)


def valid_manifest(project: Path) -> ProjectManifest:
    return ProjectManifest(
        video_id="5nTuu0FzAUg",
        source_url="https://youtu.be/5nTuu0FzAUg",
        title="100 Players Simulate Minecraft's Magical Purge",
        project_path=str(project),
        materials_path=str(project / "Материалы"),
        author_preset="MylesMC",
        job_id="job-1",
        created_at="2026-07-13T00:00:00+03:00",
        updated_at="2026-07-13T00:00:00+03:00",
    )


def test_missing_and_corrupted_manifest_are_safe(tmp_path: Path):
    loader = ManifestLoader()
    path = tmp_path / MANIFEST_NAME
    assert loader.load(path).status == ManifestStatus.MISSING
    path.write_text("{broken", encoding="utf-8")
    result = loader.load(path)
    assert result.status == ManifestStatus.CORRUPTED
    assert result.manifest is None


def test_legacy_incomplete_and_unsupported_statuses(tmp_path: Path):
    loader = ManifestLoader()
    path = tmp_path / MANIFEST_NAME
    path.write_text(json.dumps({"schema_version": 1, "video_id": "id"}), encoding="utf-8")
    assert loader.load(path).status == ManifestStatus.LEGACY
    path.write_text(json.dumps({"schema_version": 2, "video_id": "id"}), encoding="utf-8")
    assert loader.load(path).status == ManifestStatus.INCOMPLETE
    path.write_text(json.dumps({"schema_version": 999}), encoding="utf-8")
    assert loader.load(path).status == ManifestStatus.UNSUPPORTED_VERSION


def test_optional_fields_use_defaults_without_key_error(tmp_path: Path):
    project = tmp_path / "Проект"
    data = valid_manifest(project).to_dict()
    for key in ("author_preset", "job_id", "stages", "files", "discovered_files", "created_at"):
        data.pop(key)
    path = tmp_path / MANIFEST_NAME
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    result = ManifestLoader().load(path)
    assert result.status == ManifestStatus.VALID
    assert result.manifest.stages == {}
    assert result.manifest.files == {}


def test_atomic_write_round_trip_unicode_apostrophe_and_schema(tmp_path: Path):
    project = tmp_path / "Автор" / "Делаю" / "Проект"
    project.mkdir(parents=True)
    path = project / MANIFEST_NAME
    result = ManifestWriter().write(path, valid_manifest(project))
    assert result.status == ManifestStatus.VALID
    assert result.manifest.schema_version == CURRENT_SCHEMA_VERSION
    assert result.manifest.project_path == str(project)
    assert result.manifest.title == "100 Players Simulate Minecraft's Magical Purge"
    assert not path.with_name(path.name + ".tmp").exists()


def test_existing_manifest_is_backed_up_before_replace(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    path = project / MANIFEST_NAME
    original = b'{"schema_version":1,"video_id":"old"}'
    path.write_bytes(original)
    ManifestWriter().write(path, valid_manifest(project), backup_existing=True)
    assert path.with_name(path.name + ".bak").read_bytes() == original
    assert ManifestLoader().load(path).status == ManifestStatus.VALID


def test_failed_atomic_replace_leaves_original_untouched(monkeypatch, tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    path = project / MANIFEST_NAME
    original = valid_manifest(project)
    ManifestWriter().write(path, original, backup_existing=False)
    original_bytes = path.read_bytes()
    import creator_assistant.infrastructure.manifest_store as module
    real_replace = module.os.replace

    def fail_manifest_replace(source, target):
        if Path(target) == path:
            raise OSError("simulated replace failure")
        return real_replace(source, target)

    monkeypatch.setattr(module.os, "replace", fail_manifest_replace)
    changed = valid_manifest(project)
    changed.state = "RUNNING"
    with pytest.raises(OSError):
        ManifestWriter().write(path, changed, backup_existing=True)
    assert path.read_bytes() == original_bytes
    assert not path.with_name(path.name + ".tmp").exists()


def test_scanner_classifies_unknown_without_renaming(tmp_path: Path):
    project = tmp_path / "legacy"
    materials = project / "Материалы"
    materials.mkdir(parents=True)
    unknown = project / "мой важный файл.xyz"
    image = project / "обложка.png"
    media = materials / "произвольное имя.mkv"
    unknown.write_text("keep", encoding="utf-8")
    image.write_bytes(b"png")
    media.write_bytes(b"media")
    before = sorted(str(path.relative_to(project)) for path in project.rglob("*") if path.is_file())
    discovered = LegacyProjectScanner(lambda _path: {"streams": [{"codec_type": "video"}]}).scan(project)
    classifications = {Path(item.path).name: item.classification for item in discovered}
    assert classifications[unknown.name] == "UNKNOWN"
    assert classifications[image.name] == "IMAGE"
    assert classifications[media.name] == "VIDEO"
    after = sorted(str(path.relative_to(project)) for path in project.rglob("*") if path.is_file())
    assert after == before


def test_probe_failure_becomes_media_unknown(tmp_path: Path):
    media = tmp_path / "custom.flac"
    media.write_bytes(b"not-real")
    discovered = LegacyProjectScanner(lambda _path: (_ for _ in ()).throw(ValueError("bad media"))).scan(tmp_path)
    assert discovered[0].classification == "MEDIA_UNKNOWN"
    assert "probe_error" in discovered[0].details


def test_migration_is_repeatable_and_preserves_discovered_paths(tmp_path: Path):
    project = tmp_path / "старый проект"
    project.mkdir()
    user_file = project / "user.data"
    user_file.write_bytes(b"important")
    loader = ManifestLoader()
    missing = loader.load(project / MANIFEST_NAME)
    discovered = LegacyProjectScanner().scan(project)
    migrator = ManifestMigrator()
    first = migrator.migrate(
        missing, video_id="id", source_url="url", title="It's a title",
        project_path=project, author_preset="Автор", job_id="job",
        discovered_files=discovered,
    )
    writer = ManifestWriter(loader)
    writer.write(project / MANIFEST_NAME, first)
    loaded = loader.load(project / MANIFEST_NAME)
    second = migrator.migrate(
        loaded, video_id="id", source_url="url", title="It's a title",
        project_path=project, author_preset="Автор", job_id="job",
        discovered_files=LegacyProjectScanner().scan(project),
    )
    writer.write(project / MANIFEST_NAME, second)
    final = loader.load(project / MANIFEST_NAME)
    assert final.status == ManifestStatus.VALID
    assert final.manifest.video_id == "id"
    assert any(value["path"] == str(user_file) for value in final.manifest.files.values())
    assert user_file.read_bytes() == b"important"
