from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import asdict
from pathlib import Path

from creator_assistant.domain.shorts.models import Candidate
from creator_assistant.services.licensing import LicenseClientError
from creator_assistant.services.shorts.manifest import ShortsManifestStore
from creator_assistant.services.shorts.shorts_project_store import ShortsProjectStore
from creator_assistant.services.shorts.source_service import ShortsSourceService, quota_source_fingerprint


ALLOWED_MODES = {"success", "retry", "fail", "blocked"}


def _inside(path: Path, root: Path, label: str) -> Path:
    resolved = path.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise RuntimeError(f"Live FREE {label} must stay inside its isolated workspace") from exc
    return resolved


def _atomic_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(str(temporary), str(path))


def _fixture_candidates(duration: float) -> list[Candidate]:
    end = max(0.4, min(duration, 9.5))
    step = end / 3.0
    return [
        Candidate(
            id=f"short_{index:03d}", start=round((index - 1) * step, 3),
            end=round(index * step, 3), score=90.0 - index,
            text=f"Synthetic FREE E2E candidate {index}", reasons=["packaged-e2e"],
        )
        for index in range(1, 4)
    ]


def run_live_free_shorts_fixture(container, source_path: Path, report_path: Path) -> dict:
    """Exercise the real packaged FREE Shorts quota boundary without expensive AI stages."""
    if os.environ.get("CREATOR_ASSISTANT_E2E_LIVE_FREE") != "1":
        raise RuntimeError("Live FREE fixture requires CREATOR_ASSISTANT_E2E_LIVE_FREE=1")
    workspace_value = os.environ.get("CREATOR_ASSISTANT_E2E_WORKSPACE", "").strip()
    mode = os.environ.get("CREATOR_ASSISTANT_E2E_SHORTS_MODE", "success").strip().casefold()
    if not workspace_value or mode not in ALLOWED_MODES:
        raise RuntimeError("Live FREE Shorts fixture environment is incomplete")

    workspace = Path(workspace_value).resolve()
    source_path = _inside(source_path, workspace, "source")
    report_path = _inside(report_path, workspace, "report")
    report: dict = {
        "success": False,
        "mode": mode,
        "source": str(source_path),
        "expensive_stages_started": False,
    }
    reservation = ""
    committed = False
    try:
        container.require_entitlement("shorts_analysis")
        source = ShortsSourceService(container.runner, container.paths["ffprobe"]).probe(source_path)
        identity = quota_source_fingerprint(source)
        operation_key = hashlib.sha256(f"shorts-source:{identity}".encode("utf-8")).hexdigest()
        project_path = workspace / "shorts-results" / f"source-{identity[:16]}"
        report.update({
            "source_content_sha256": identity,
            "quota_operation_key_hash": operation_key[:16],
            "project_path": str(project_path),
            "media": {
                "duration": source.duration, "width": source.width, "height": source.height,
                "fps": source.fps, "video_codec": source.video_codec, "audio_codec": source.audio_codec,
            },
        })
        try:
            reservation = container.acquire_free_quota("SHORTS_SOURCE", operation_key)
        except LicenseClientError as exc:
            if mode != "blocked" or exc.code != "FREE_QUOTA_EXHAUSTED":
                raise
            report.update({
                "success": True,
                "blocked_as_expected": True,
                "error_code": exc.code,
                "message": str(exc),
                "quota_details": {
                    key: exc.details.get(key) for key in ("kind", "used", "limit")
                    if key in exc.details
                },
                "workspace_created": project_path.exists(),
            })
            return _write_report(report_path, report)

        if mode == "blocked":
            raise RuntimeError("Expected FREE quota denial, but a reservation was granted")
        report["reservation_created"] = bool(reservation)
        marker = report_path.with_suffix(report_path.suffix + ".reserved.json")
        _atomic_json(marker, {"reserved": True, "operation_key_hash": operation_key[:16]})
        hold = max(0.0, min(float(os.environ.get("CREATOR_ASSISTANT_E2E_SHORTS_HOLD_SECONDS", "0")), 30.0))
        if hold:
            time.sleep(hold)
        if mode == "fail":
            raise RuntimeError("Intentional pre-processing E2E failure")

        store = ShortsProjectStore()
        paths = store.paths(project_path)
        if mode == "retry":
            manifest = ShortsManifestStore(paths.manifest).load()
            source_info_path = paths.analysis / "source_info.json"
            if not manifest or not source_info_path.is_file():
                raise RuntimeError("Existing Shorts fixture is not valid")
            saved_source = json.loads(source_info_path.read_text(encoding="utf-8"))
            if saved_source.get("content_fingerprint") != identity:
                raise RuntimeError("Existing Shorts fixture belongs to different source content")
            candidates = list(manifest.candidates)
        else:
            paths = store.open_or_create(project_path, source)
            _atomic_json(paths.analysis / "source_info.json", asdict(source))
            candidate_models = _fixture_candidates(source.duration)
            candidates = [asdict(item) for item in candidate_models]
            manifest = ShortsManifestStore(paths.manifest).load()
            if not manifest:
                raise RuntimeError("Shorts fixture manifest was not created")
            manifest.candidates = candidates
            manifest.completed_stages = ["packaged_free_quota_e2e"]
            ShortsManifestStore(paths.manifest).save(manifest)
            _atomic_json(paths.analysis / "candidates.json", candidates)

        container.finish_free_quota(reservation, True)
        committed = True
        report.update({
            "success": len(candidates) == 3,
            "candidate_count": len(candidates),
            "candidate_ids": [str(item.get("id", "")) for item in candidates],
            "manifest": str(paths.manifest),
            "quota_finished": "COMMITTED",
            "workspace_created": project_path.exists(),
        })
    except Exception as exc:
        if reservation and not committed:
            try:
                container.finish_free_quota(reservation, False)
                report["reservation_released"] = True
            except Exception as release_exc:
                report["release_error"] = f"{type(release_exc).__name__}: {release_exc}"
        report["error"] = f"{type(exc).__name__}: {exc}"
    return _write_report(report_path, report)


def _write_report(report_path: Path, report: dict) -> dict:
    _atomic_json(report_path, report)
    return report
