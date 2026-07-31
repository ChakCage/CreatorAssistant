from __future__ import annotations

import hashlib
import json
import shutil
import wave
from datetime import datetime, timezone
from pathlib import Path

from creator_assistant.domain.errors import JobCancelledError
from creator_assistant.domain.job import CancellationToken
from creator_assistant.infrastructure.manifest_store import (
    ManifestLoader,
    ManifestStatus,
    ManifestWriter,
    ProjectManifest,
    project_manifest_path,
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _make_media(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(8_000)
        stream.writeframes(b"\0\0" * 2_000)


def run_packaged_project_fixture(feature_gate, report_path: Path) -> dict:
    """Exercise deterministic project persistence from inside the packaged EXE."""
    root = report_path.parent / "packaged-project-fixture"
    project = root / "Проект fixture"
    materials = project / "Materials"
    source = root / "fixture.wav"
    destination = materials / source.name
    report = {"success": False, "root": str(root)}

    def import_media(path: Path, token: CancellationToken) -> str:
        token.raise_if_cancelled()
        if not path.is_file():
            return "missing"
        materials.mkdir(parents=True, exist_ok=True)
        if not destination.exists():
            shutil.copy2(path, destination)
        elif _sha256(destination) != _sha256(path):
            raise RuntimeError("Existing fixture media differs from source")
        return "ready"

    try:
        feature_gate.require("project_preparation")
        _make_media(source)
        first = import_media(source, CancellationToken())
        now = datetime.now(timezone.utc).isoformat()
        manifest = ProjectManifest(
            video_id="packaged-fixture-001",
            source_url="local://packaged-fixture",
            title="Короткий packaged fixture",
            project_path=str(project),
            materials_path=str(materials),
            state="COMPLETED",
            files={"source_audio": str(destination), "source_sha256": _sha256(source)},
            created_at=now,
            updated_at=now,
        )
        ManifestWriter().write(project_manifest_path(project), manifest)
        second = import_media(source, CancellationToken())
        loaded = ManifestLoader().load(project_manifest_path(project))

        cancelled = CancellationToken()
        cancelled.cancel()
        cancellation_ok = False
        try:
            import_media(source, cancelled)
        except JobCancelledError:
            cancellation_ok = True
        missing = import_media(root / "missing.wav", CancellationToken())
        media_files = list(materials.glob("fixture*.wav"))
        report.update({
            "success": (
                first == second == "ready"
                and loaded.status is ManifestStatus.VALID
                and len(media_files) == 1
                and cancellation_ok
                and missing == "missing"
            ),
            "manifest": str(project_manifest_path(project)),
            "manifest_status": loaded.status.value,
            "materials": str(materials),
            "media_count_after_rerun": len(media_files),
            "cancellation": "handled" if cancellation_ok else "failed",
            "missing_source": missing,
        })
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report
