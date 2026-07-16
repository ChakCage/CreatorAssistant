import json
import os
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from creator_assistant.domain.automation.models import AutomationJobSource, AutomationMode
from creator_assistant.domain.shorts.models import Candidate
from creator_assistant.infrastructure.automation_job_store import AutomationJobStore
from creator_assistant.services.automation.cli import run_automation_cli
from creator_assistant.services.automation.engine import AutomationEngine
from creator_assistant.services.automation.publishing import TikTokPublishingConnector, YouTubePublishingConnector
from creator_assistant.services.automation.shorts_pipeline import ExistingShortsAutomationPipeline
from creator_assistant.services.shorts.channel_assets import ChannelAssetStore
from creator_assistant.ui.autopilot_tab import AutopilotTab

from test_automation_engine import FakePipeline


def app():
    return QApplication.instance() or QApplication([])


def test_cli_and_ui_use_same_automation_engine(tmp_path):
    qt = app()
    engine = AutomationEngine(FakePipeline(), AutomationJobStore(tmp_path / "jobs"))
    container = SimpleNamespace(automation_engine=engine, channel_assets=SimpleNamespace(profiles=lambda: []))
    tab = AutopilotTab(container)
    assert tab.engine is engine
    engine.create_job(["one.mp4"])
    tab.refresh_jobs()
    assert engine.store.list()
    output = []
    assert run_automation_cli(["--list-jobs"], engine, output.append) == 0
    assert engine.store.list()[0].job_id in output[0]
    tab.close()
    assert qt is QApplication.instance()


def test_cli_run_job_uses_engine_and_stops_for_approval(tmp_path):
    engine = AutomationEngine(FakePipeline(), AutomationJobStore(tmp_path / "jobs"))
    spec = tmp_path / "job.json"
    spec.write_text(json.dumps({"sources": ["one.mp4"], "mode": "APPROVAL_REQUIRED"}), encoding="utf-8")
    output = []
    assert run_automation_cli(["--run-job", str(spec)], engine, output.append) == 0
    assert '"status": "WAITING_FOR_APPROVAL"' in output[0]


def test_pipeline_uses_custom_subtitle_preset_and_auto_channel_profile(tmp_path):
    source_file = tmp_path / "Beppo" / "Project" / "video.mp4"
    source_file.parent.mkdir(parents=True)
    source_file.write_bytes(b"video")
    project = source_file.parent / "Shorts"
    analysis = project / "Analysis"; analysis.mkdir(parents=True)
    candidate = Candidate(
        "short_001", 0, 40, 95, "text", final_score=95, candidate_rank=1,
        branding_settings={"translated_video_title": "Русское название", "short_hook_title": "Сильный момент"},
    )
    (analysis / "candidates.json").write_text(json.dumps([candidate.__dict__], ensure_ascii=False), encoding="utf-8")
    (analysis / "transcript.json").write_text("{}", encoding="utf-8")
    (project / "shorts_manifest.json").write_text(json.dumps({
        "schema_version": 1, "shorts_project_id": "x", "source_path": str(source_file),
        "source_fingerprint": "x", "source_size": 1, "source_mtime": 1, "source_duration": 40,
        "title_assets": {"translated_title": "Русское название"},
    }), encoding="utf-8")
    asset_root = tmp_path / "assets"
    profile_root = asset_root / "beppo_ru"; profile_root.mkdir(parents=True)
    (profile_root / "banner.png").write_bytes(b"png")
    (profile_root / "profile.json").write_text(json.dumps({
        "id": "beppo_ru", "display_name": "Beppo На Русском", "source_author": "Beppo",
        "aliases": ["Beppo"], "subscribe_banner": "banner.png", "enabled": True,
    }), encoding="utf-8")
    container = SimpleNamespace(settings={
        "shorts_subtitle_presets": {"clean": {"style": "clean", "size": 66, "vertical_offset": -23}},
        "shorts_subtitle_defaults": {"style": "clean", "size": 58},
        "shorts_branding_defaults": {"show_title": True}, "shorts_channel_profile_links": {},
    })
    pipeline = ExistingShortsAutomationPipeline(container)
    pipeline.assets = ChannelAssetStore(asset_root)
    engine = AutomationEngine(pipeline, AutomationJobStore(tmp_path / "jobs"))
    job = engine.create_job([str(source_file)], mode=AutomationMode.APPROVAL_REQUIRED.value, selection_settings={"minimum_score": 80})
    job.sources[0].shorts_project_path = str(project)
    job.sources[0].source_author = "Beppo"
    engine.store.save(job)
    pipeline.validate(job); pipeline.analyze(job); pipeline.select(job); pipeline.prepare_titles(job); pipeline.prepare_composition(job)
    short = job.shorts[0]
    assert short.subtitle_settings["size"] == 66
    assert short.subtitle_settings["vertical_offset"] == -23
    assert short.profile_id == "beppo_ru"
    assert Path(short.branding_settings["channel_banner_path"]).is_file()
    assert short.branding_settings["final_title_text"] == "Русское название"


def test_mock_connectors_never_contact_remote_service():
    for connector in (YouTubePublishingConnector(), TikTokPublishingConnector()):
        assert connector.authenticate()
        assert connector.validate_account()["mock"] is True
        upload = connector.upload("short.mp4", {"short_id": "one"})
        operation = connector.schedule(upload, "2026-07-20T13:00:00+03:00")
        assert connector.get_status(operation)["remote"] is False


def test_channel_id_has_priority_over_saved_link_and_ambiguity_never_guesses(tmp_path):
    root = tmp_path / "assets"
    for identifier, channel_ids, aliases in (
        ("beppo", ["UC-BEPPO"], ["Shared"]),
        ("myles", ["UC-MYLES"], ["Shared"]),
    ):
        folder = root / identifier; folder.mkdir(parents=True)
        (folder / "banner.png").write_bytes(b"png")
        (folder / "profile.json").write_text(json.dumps({
            "id": identifier, "display_name": identifier, "source_author": identifier,
            "aliases": aliases, "channel_ids": channel_ids,
            "subscribe_banner": "banner.png", "enabled": True,
        }), encoding="utf-8")
    store = ChannelAssetStore(root)
    assert store.resolve(channel_id="UC-BEPPO", saved_profile_id="myles").id == "beppo"
    assert store.resolve(source_author="Shared") is None
