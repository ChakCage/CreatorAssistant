from __future__ import annotations

import hashlib
import json
import os
import shutil
import wave
from datetime import datetime, timezone
from pathlib import Path

from creator_assistant.domain.errors import JobCancelledError
from creator_assistant.domain.job import CancellationToken
from creator_assistant.domain.models import ProjectOptions, VideoFormat, VideoMetadata
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


def run_live_free_project_fixture(container, report_path: Path) -> dict:
    """Run the production project/quota boundary from a packaged staging EXE."""
    if os.environ.get("CREATOR_ASSISTANT_E2E_LIVE_FREE") != "1":
        raise RuntimeError("Live FREE fixture requires CREATOR_ASSISTANT_E2E_LIVE_FREE=1")
    workspace_value = os.environ.get("CREATOR_ASSISTANT_E2E_WORKSPACE", "").strip()
    identity = os.environ.get("CREATOR_ASSISTANT_E2E_PROJECT_IDENTITY", "").strip()
    mode = os.environ.get("CREATOR_ASSISTANT_E2E_PROJECT_MODE", "success").strip().casefold()
    if not workspace_value or not identity or mode not in {"success", "retry", "fail"}:
        raise RuntimeError("Live FREE fixture environment is incomplete")
    if any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for char in identity):
        raise RuntimeError("Unsafe live FREE project identity")
    workspace = Path(workspace_value).resolve()
    report_path = report_path.resolve()
    try:
        report_path.relative_to(workspace)
    except ValueError as exc:
        raise RuntimeError("Live FREE report must stay inside its isolated workspace") from exc
    project_path = workspace / identity
    operation_key = hashlib.sha256(f"project:{identity}".encode("utf-8")).hexdigest()
    report = {
        "success": False,
        "mode": mode,
        "identity": identity,
        "project_path": str(project_path),
        "quota_operation_key_hash": operation_key[:16],
    }
    reservation = ""
    try:
        container.require_entitlement("project_preparation")
        reservation = container.acquire_free_quota("PROJECT", operation_key)
        report["reservation_created"] = bool(reservation)
        if mode == "fail":
            raise RuntimeError("Intentional pre-completion E2E failure")
        if mode == "retry":
            loaded = ManifestLoader().load(project_manifest_path(project_path))
            if loaded.status is not ManifestStatus.VALID:
                raise RuntimeError("Existing project manifest is not valid")
            result_path = project_path
        else:
            metadata = VideoMetadata(
                video_id=identity,
                title=identity,
                duration=8.0,
                webpage_url="local://creator-assistant-free-e2e",
                formats=[
                    VideoFormat("fixture-video", "mp4", 1280, 720, 30.0, "h264", "none"),
                    VideoFormat("fixture-audio", "m4a", acodec="aac"),
                ],
                channel="Creator Assistant E2E",
                uploader="Creator Assistant E2E",
            )
            options = ProjectOptions(
                download_maximum=False,
                create_proxy=False,
                download_audio=False,
                create_instrumental=False,
                create_reaper_project=False,
                create_vegas_project=False,
                temp_root=str(workspace / "temp"),
                job_id="free-e2e-" + identity.casefold(),
            )
            result = container.projects.execute(
                workspace, metadata, options, CancellationToken(), lambda _progress: None,
                new_project_path=project_path,
            )
            result_path = Path(result.project_path or "")
        container.finish_free_quota(reservation, True)
        loaded = ManifestLoader().load(project_manifest_path(result_path))
        report.update({
            "success": loaded.status is ManifestStatus.VALID,
            "manifest": str(project_manifest_path(result_path)),
            "manifest_status": loaded.status.value,
            "materials_exists": (result_path / "Материалы").is_dir(),
        })
    except Exception as exc:
        if reservation:
            try:
                container.finish_free_quota(reservation, False)
                report["reservation_released"] = True
            except Exception as release_exc:
                report["release_error"] = f"{type(release_exc).__name__}: {release_exc}"
        report["error"] = f"{type(exc).__name__}: {exc}"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report
