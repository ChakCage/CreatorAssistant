import copy
import importlib.util
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
import pytest
from pydantic import ValidationError
from ai_editor_copilot.domain.models import SemanticTimeline, TimelineState, SourceAsset, AssetIndex
from ai_editor_copilot.domain.plan import EditPlan, PlannerProposal
from ai_editor_copilot.tools.registry import ACTION_ADAPTER, PARAMETERS, capabilities
from ai_editor_copilot.planner.service import EditPlanner
from ai_editor_copilot.planner.validation import PlanError, validate_plan
from ai_editor_copilot.adapters.mock import MockPlannerBackend
from ai_editor_copilot.adapters.local_creator_assistant import LocalCreatorAssistantLLMBackend
from ai_editor_copilot.executors.base import DryRunExecutor, DaVinciExecutor
from ai_editor_copilot.feedback.events import FeedbackEvent, save_feedback
from ai_editor_copilot.ingest.transcript import TranscriptDocument, from_document
from ai_editor_copilot.assets.index import search

ROOT = Path(__file__).resolve().parents[1]


def proposal(context):
    return json.loads(MockPlannerBackend().generate(context, "", {}))


def test_semantic_timeline_roundtrip(context):
    assert SemanticTimeline.model_validate_json(context.timeline.model_dump_json()) == context.timeline
    assert context.timeline.segments[0].faces is None


@pytest.mark.parametrize("bounds", [(-1, 2), (4, 3), (3, 3), (0, 3601), (0, float("inf")), (float("nan"), 2)])
def test_invalid_timestamps(context, bounds):
    data = context.timeline.model_dump()
    data["segments"][0]["range"] = dict(start=bounds[0], end=bounds[1])
    with pytest.raises(ValidationError):
        SemanticTimeline.model_validate(data)


def test_valid_plan_multiple_noncontiguous_roles_duration(context):
    plan = EditPlanner(MockPlannerBackend()).plan(context)
    assert len(plan.sequence) == 5
    assert sum(c.source_range.end - c.source_range.start for c in plan.sequence) == 58
    assert [c.narrative_role for c in plan.sequence] == ["hook", "setup", "development", "climax", "payoff"]
    assert plan.sequence[-1].source_range.start > 3400
    assert all(b.source_range.start - a.source_range.end > 100 for a, b in zip(plan.sequence, plan.sequence[1:]))
    assert validate_plan(EditPlan.model_validate_json(plan.model_dump_json()), context) == plan


def test_source_order_may_change(context):
    data = proposal(context)
    # Re-use the final reveal as hook and the early intrigue as payoff: ordering is explicit.
    a, b = data["clips"][0], data["clips"][-1]
    a["segment_id"], b["segment_id"] = b["segment_id"], a["segment_id"]
    a["source_range"], b["source_range"] = b["source_range"], a["source_range"]
    plan = EditPlanner(MockPlannerBackend([json.dumps(data)])).plan(context)
    assert plan.sequence[0].source_range.start > plan.sequence[-1].source_range.start


@pytest.mark.parametrize("mutation", ["duplicate", "overlap", "source", "unknown", "role", "duration"])
def test_semantic_rejection(context, mutation):
    data = proposal(context)
    if mutation == "duplicate":
        data["clips"][1] = copy.deepcopy(data["clips"][0])
    elif mutation == "overlap":
        data["clips"][1]["segment_id"] = "h"
        data["clips"][1]["source_range"] = {"start": 44, "end": 48}
    elif mutation == "source":
        data["clips"][0]["source_range"]["end"] = 4000
    elif mutation == "unknown":
        data["clips"][0]["segment_id"] = "hallucinated"
    elif mutation == "role":
        data["clips"][0]["narrative_role"] = "development"
    else:
        context.target_duration = 55
    with pytest.raises(PlanError, match="after repair"):
        EditPlanner(MockPlannerBackend([json.dumps(data)])).plan(context)


@pytest.mark.parametrize("raw", ["not JSON", "```json\n{}\n```", '{"story":"a","story":"b"}', '{"clips":NaN}', "[]"])
def test_malformed_json_fails_clearly(context, raw):
    backend = MockPlannerBackend([raw])
    with pytest.raises(PlanError):
        EditPlanner(backend).plan(context)
    assert backend.calls == 2


@pytest.mark.parametrize("first", ["syntax", "semantic"])
def test_repair(context, first):
    good = json.dumps(proposal(context))
    bad = proposal(context)
    bad["clips"][0]["source_range"]["start"] = 1
    backend = MockPlannerBackend(["{" if first == "syntax" else json.dumps(bad), good])
    service = EditPlanner(backend)
    plan = service.plan(context)
    assert len(plan.sequence) == 5 and backend.calls == 2
    assert "REPAIR" in backend.prompts[-1]
    assert service.attempts[0]["valid"] is False


def test_deterministic_mock_and_stable_ids(context):
    a = EditPlanner(MockPlannerBackend()).plan(context)
    b = EditPlanner(MockPlannerBackend()).plan(context)
    assert a.model_dump_json() == b.model_dump_json()


def test_action_schema_rejects_unknown_and_bad_parameters(context):
    action = EditPlanner(MockPlannerBackend()).plan(context).actions[0].model_dump()
    action["action"] = "RUN_SHELL"
    with pytest.raises(ValidationError):
        ACTION_ADAPTER.validate_python(action)
    action["action"] = "ZOOM"
    action["parameters"] = {"scale": 12}
    with pytest.raises(ValidationError):
        ACTION_ADAPTER.validate_python(action)
    action["parameters"] = {"scale": 1.3, "command": "rm -rf"}
    with pytest.raises(ValidationError):
        ACTION_ADAPTER.validate_python(action)
    assert len(PARAMETERS) == 31
    assert not any(v["davinci_verified"] for v in capabilities().values())


@pytest.mark.parametrize("kind", ["dependency", "target", "duration", "source", "constraint", "missing_insert"])
def test_executor_revalidates_mutated_plan(context, kind):
    plan = EditPlanner(MockPlannerBackend()).plan(context)
    if kind == "dependency": plan.actions[0].dependencies = [plan.actions[-1].id]
    if kind == "target": plan.actions[0].target.id = "unknown"
    if kind == "duration": plan.actions[0].timing.duration = 100
    if kind == "source": plan.actions[0].source.asset_id = "unknown"
    if kind == "constraint": plan.target_duration = 100
    if kind == "missing_insert": plan.actions.pop()
    with pytest.raises(PlanError):
        DryRunExecutor().execute(plan, context)


def test_dry_run_no_side_effects(context, tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("executor attempted process or file write")
    plan = EditPlanner(MockPlannerBackend()).plan(context)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(Path, "write_text", forbidden)
    result = DryRunExecutor().execute(plan, context)
    assert len(result["operations"]) == 5
    assert result["media_modified"] is False
    assert not list(tmp_path.iterdir())
    with pytest.raises(NotImplementedError): DaVinciExecutor().execute(plan, context)


def test_current_timeline_explicitly_unsupported(context):
    context.current_timeline = TimelineState(id="existing", revision="r1", duration=60)
    backend = MockPlannerBackend()
    with pytest.raises(PlanError, match="future"): EditPlanner(backend).plan(context)
    assert backend.calls == 0


def test_raw_ingest_does_not_invent_annotations():
    doc = TranscriptDocument(source_id="raw", source_uri="fixture://raw", duration=100,
        segments=[dict(id="s1", start=1, end=5, text="Привет")])
    timeline = from_document(doc)
    assert timeline.segments[0].topic is None
    assert timeline.segments[0].annotation_origin == "transcript_only"


def test_local_adapter_transport_contract(context):
    from creator_assistant.services.shorts.semantic_backend import OllamaSemanticScorer
    import io
    captured = []
    content = json.dumps(proposal(context))
    def opener(request, timeout):
        captured.append(json.loads(request.data))
        return io.BytesIO((json.dumps({"message": {"content": content}, "done": True}) + "\n").encode())
    backend = LocalCreatorAssistantLLMBackend(OllamaSemanticScorer(opener=opener))
    assert EditPlanner(backend).plan(context).generated_by.model == "qwen3.6:35b-a3b"
    assert captured[0]["format"] == PlannerProposal.model_json_schema()
    assert captured[0]["keep_alive"]
    assert captured[0]["model"] == "qwen3.6:35b-a3b"
    with pytest.raises(ValueError):
        LocalCreatorAssistantLLMBackend(OllamaSemanticScorer(model="other"))


def test_schemas_match_canonical_models():
    spec = importlib.util.spec_from_file_location("export_schemas", ROOT / "scripts/export_schemas.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name, schema in module.generated().items():
        assert json.loads((ROOT / "schemas" / (name + ".schema.json")).read_text(encoding="utf-8")) == schema


def test_feedback_and_no_overwrite(context, tmp_path):
    action = EditPlanner(MockPlannerBackend()).plan(context).actions[0]
    event = FeedbackEvent(id="f1", plan_id="p1", action_id=action.id, participant_id="anon1",
        occurred_at=datetime(2026, 9, 15, 10, tzinfo=timezone.utc), decision="accepted", ai_proposal=action,
        final_edit_decision=action, context_digest="sha256:fixture", timeline_revision_before="r1", timeline_revision_after="r2")
    saved = save_feedback(tmp_path, event)
    assert FeedbackEvent.model_validate_json(saved.read_text()).decision == "accepted"
    with pytest.raises(FileExistsError): save_feedback(tmp_path, event)


def test_asset_index_local_only():
    index = AssetIndex(assets=[SourceAsset(id="gta", kind="broll", uri="fixture://gta", tags=["GTA", "driving"])])
    assert [a.id for a in search(index, "GTA driving")] == ["gta"]
    assert search(index, "ocean") == []


@pytest.mark.parametrize("action_name", list(PARAMETERS))
def test_each_action_has_typed_parameters(context, action_name):
    examples = {
        "Empty": {}, "Trim": {"source_range": {"start": 1, "end": 2}},
        "Silence": {"threshold_db": -40, "minimum_silence": 0.5},
        "Move": {"destination": 4}, "Insert": {"fit": "cover"},
        "TextParams": {"text": "Текст", "font": "Segoe UI", "size": 65},
        "Subtitles": {"language": "ru", "style_id": "clean"}, "Zoom": {"scale": 1.35},
        "Pan": {"x": 0.2, "y": 0}, "Crop": {"left": 0.1, "right": 0.1, "top": 0, "bottom": 0},
        "Reframe": {"aspect_ratio": "9:16"}, "SpeedUp": {"rate": 2}, "SlowMotion": {"rate": 0.5},
        "Freeze": {"source_time": 1}, "Chroma": {"color": "#00FF00", "tolerance": 0.2},
        "Mask": {"description": "face"}, "Track": {"object_description": "car"}, "Blur": {"radius": 10},
        "Gain": {"gain_db": -12}, "Duck": {"reduction_db": -10, "attack": 0.1, "release": 0.5},
        "Fade": {"direction": "out"}, "Normalize": {"target_lufs": -14},
        "Transition": {"kind": "dissolve"}, "Stabilize": {"strength": 0.5},
    }
    data = EditPlanner(MockPlannerBackend()).plan(context).actions[0].model_dump()
    data["action"] = action_name
    data["parameters"] = examples[PARAMETERS[action_name].__name__]
    assert ACTION_ADAPTER.validate_python(data).action == action_name
    data["parameters"] = {**data["parameters"], "unexpected": True}
    with pytest.raises(ValidationError): ACTION_ADAPTER.validate_python(data)


def test_numeric_strings_not_silently_coerced(context):
    data = context.timeline.model_dump()
    data["segments"][0]["range"]["start"] = "0"
    with pytest.raises(ValidationError): SemanticTimeline.model_validate(data)


def test_missing_assets_remain_unresolved(context):
    data = proposal(context)
    data["asset_requests"] = [{"type": "asset_search", "media_kind": "meme", "query": "disbelief"}]
    plan = EditPlanner(MockPlannerBackend([json.dumps(data)])).plan(context)
    assert "MEME_SEARCH: disbelief" in plan.unresolved_requirements
    assert all(a.action == "INSERT_VIDEO" for a in plan.actions)


def test_cli_bad_plan_does_not_emit_success(context, tmp_path):
    from ai_editor_copilot.cli import main
    result = main(["--transcript", str(ROOT / "examples/narrative_short/transcript.json"),
                   "--prompt", "необычный отель", "--duration", "2", "--output", str(tmp_path)])
    assert result == 2
    assert not (tmp_path / "edit_plan.json").exists()
    assert json.loads((tmp_path / "validation.json").read_text())["valid"] is False


def test_lab_import_does_not_start_creator_ui():
    code = "import ai_editor_copilot.cli,sys; assert not any(m.startswith('creator_assistant') for m in sys.modules)"
    subprocess.run([sys.executable, "-c", code], check=True)


def test_cli_preserves_previous_evidence(tmp_path):
    from ai_editor_copilot.cli import main
    sentinel = tmp_path / "edit_plan.json"
    sentinel.write_text("old evidence")
    assert main(["--transcript", str(ROOT / "examples/narrative_short/transcript.json"),
                 "--prompt", "hotel", "--output", str(tmp_path)]) == 2
    assert sentinel.read_text() == "old evidence"


@pytest.mark.parametrize("folder", ["mock_output", "local_output"])
def test_committed_demo_revalidates(folder):
    from ai_editor_copilot.domain.models import PlannerInput
    path = ROOT / "examples/narrative_short" / folder
    plan = EditPlan.model_validate_json((path / "edit_plan.json").read_text(encoding="utf-8"))
    context = PlannerInput.model_validate_json((path / "input.json").read_text(encoding="utf-8"))
    report = DryRunExecutor().execute(plan, context)
    assert report == json.loads((path / "dry_run.json").read_text(encoding="utf-8"))
