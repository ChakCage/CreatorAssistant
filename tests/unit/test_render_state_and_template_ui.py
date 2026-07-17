import json
import os
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from creator_assistant.domain.automation.models import AutomationJob, AutomationJobSource
from creator_assistant.domain.shorts.models import (
    Candidate, SubtitleCue, Transcript, TranscriptSegment, TranscriptWord,
)
from creator_assistant.infrastructure.automation_job_store import AutomationJobStore
from creator_assistant.services.automation.engine import AutomationEngine
from creator_assistant.services.automation.selection import AutomaticCandidateSelector
from creator_assistant.services.shorts.render_state import RenderStateStore
from creator_assistant.services.shorts.subtitle_service import SubtitleService
from creator_assistant.ui.shorts.subtitle_editor import SubtitleEditor


def app():
    return QApplication.instance() or QApplication([])


def test_legacy_render_sidecar_is_atomically_migrated_away_from_renders(tmp_path):
    renders = tmp_path / "Renders"
    renders.mkdir()
    video = renders / "[01] short_001 [autopilot].mp4"
    video.write_bytes(b"mp4")
    legacy = video.with_suffix(".render.json")
    legacy.write_text(json.dumps({"render_key": "abc123", "job_id": "job-one"}), encoding="utf-8")

    store = RenderStateStore(tmp_path)
    assert store.migrate_legacy() == 1
    assert not legacy.exists()
    assert video.is_file()
    assert store.path_for("abc123").is_file()
    assert store.load("abc123")["output_path"] == str(video)


def test_delete_job_removes_private_state_but_preserves_mp4(tmp_path):
    project = tmp_path / "Shorts"
    video = project / "Renders" / "ready.mp4"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"mp4")
    state = RenderStateStore(project)
    state.save("key", {"job_id": "job-one", "output_path": str(video)})
    store = AutomationJobStore(tmp_path / "jobs")
    engine = AutomationEngine(SimpleNamespace(), store)
    job = AutomationJob("job-one", [AutomationJobSource("source.mp4", shorts_project_path=str(project))], status="CANCELLED")
    store.save(job)

    assert engine.delete_job("job-one")
    assert not state.path_for("key").exists()
    assert video.is_file()


def test_project_template_controls_are_visible_inside_vertical_editor(tmp_path):
    qt = app()
    editor = SubtitleEditor()
    editor.resize(1200, 800)
    editor.show()
    qt.processEvents()
    assert editor.template_save.isVisible()
    assert editor.template_apply_all.isVisible()
    assert editor.template_reset_short.isVisible()
    assert editor.template_view.isVisible()
    assert "шаблон проекта" in editor.template_save.text().casefold()
    editor.close()


def test_score_details_explain_threshold_and_weakest_candidate():
    candidates = [
        Candidate("short_001", 0, 40, 91.1, "one", candidate_rank=1, final_score=91.1),
        Candidate("short_002", 100, 140, 79.0, "two", candidate_rank=2, final_score=79.0),
        Candidate("short_003", 200, 240, 77.3, "three", candidate_rank=3, final_score=77.3),
        Candidate("short_004", 300, 340, 76.9, "four", candidate_rank=4, final_score=76.9),
    ]
    selected, summary, details = AutomaticCandidateSelector().evaluate(candidates, {
        "minimum_score": 80, "maximum_per_source": 10,
        "minimum_duration": 25, "maximum_duration": 75,
        "minimum_temporal_distance": 30,
    })
    assert [item.id for item in selected] == ["short_001"]
    assert "Выбрано 1 из 4: только один кандидат прошёл порог 80" in summary
    assert [item["reason"] for item in details] == ["выбран", "ниже порога", "ниже порога", "ниже порога"]


def test_canonical_track_removes_context_after_fragment_and_keeps_short_reply():
    words = [
        TranscriptWord(8.0, 8.4, "Да!"),
        TranscriptWord(9.0, 9.8, "детка"),
        TranscriptWord(10.5, 10.8, "И"),
        TranscriptWord(10.81, 11.2, "как"),
    ]
    segment = TranscriptSegment(1, 8.0, 11.2, "Да! детка И как", words=words)
    transcript = Transcript("ru", 20, segment.text, [segment])
    candidate = Candidate("short_001", 8.0, 10.0, 90, "Да! детка")
    service = SubtitleService()
    canonical = service.generate(transcript, candidate, maximum=20, lines=2)
    stored = [*canonical, SubtitleCue(1.9, 2.0, "И как только я всё...")]
    cleaned = service.track(transcript, candidate, 20, 2, stored).cues
    text = " ".join(cue.text for cue in cleaned)
    assert "Да!" in text and "детка" in text
    assert "как только" not in text
    assert service.suggested_candidate_end(transcript, candidate) == 10.2
