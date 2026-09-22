import json
from datetime import datetime, timezone
import pytest
from ai_editor_copilot.narrative.evidence import (EvidenceSource, RawSegment, Annotation, reference,
    resolve, validate_annotation, file_sha256, load_evidence)
from ai_editor_copilot.domain.models import TimeRange
from ai_editor_copilot.narrative.chunks import make_chunks, coverage
from ai_editor_copilot.narrative.contracts import (Candidate, Decision, Chosen, StoryEdge, Refusal,
    BlockResponse, deduplicate, validate_graph)
from ai_editor_copilot.narrative.pipeline import Calls, HierarchicalPlanner, candidate_from_wire
from ai_editor_copilot.narrative.baseline import contiguous_baseline
from ai_editor_copilot.narrative.evaluation import metrics, variance, blind_package, load_tasks, HumanRating
from ai_editor_copilot.tools.registry import Provenance


@pytest.fixture
def evidence():
    return EvidenceSource(id="source", sha256="a" * 64, transcript_sha256="b" * 64,
        duration=3000.0, language="en", backend="fixture", model="none", ingested_at=datetime.now(timezone.utc),
        segments=[RawSegment(id=f"s{i}", range=TimeRange(start=float(i * 10), end=float(i * 10 + 10)),
            text=f"Event {i}. We found the answer and explained the problem.") for i in range(300)])


class Backend:
    provenance = Provenance(backend="test", model="qwen3.6:35b-a3b", prompt_version="test")
    def __init__(self, outputs):
        self.outputs = iter(outputs)
    def generate(self, context, prompt, schema):
        return next(self.outputs)


def test_coverage_and_overlap(evidence):
    chunks = make_chunks(evidence, max_chars=1000)
    assert chunks[0]["segments"][-1]["id"] in {s["id"] for s in chunks[1]["segments"]}
    report = coverage(evidence, chunks, [c["id"] for c in chunks])
    assert report["source_time_coverage_percent"] == 100
    assert report["segment_coverage_percent"] == 100
    assert report["full_transcript_analyzed"]
    partial = coverage(evidence, chunks, [chunks[0]["id"]])
    assert not partial["full_transcript_analyzed"]
    assert partial["unprocessed_segment_ids"]
    assert chunks == make_chunks(evidence, max_chars=1000)


def test_no_silent_truncation(evidence):
    evidence.segments[0].text = "a" * 3000
    with pytest.raises(ValueError, match="no silent truncation"):
        make_chunks(evidence, max_chars=500)


def test_annotation_and_raw_reconstruction(evidence):
    ref = reference(evidence.segments[:2])
    assert resolve(evidence, ref) == evidence.segments[:2]
    ann = Annotation(type="transcript_quote", value=evidence.segments[0].text, status="OBSERVED",
        confidence=1.0, evidence=ref, generator="test", model="none", created_at=datetime.now(timezone.utc))
    validate_annotation(evidence, ann)
    ann.value = "invented quotation"
    with pytest.raises(ValueError):
        validate_annotation(evidence, ann)
    with pytest.raises(ValueError):
        Annotation(type="visual", value="a red car", status="UNKNOWN", confidence=.9,
            evidence=ref, generator="test", model="none", created_at=datetime.now(timezone.utc))
    ref.evidence_end += 1
    with pytest.raises(ValueError):
        resolve(evidence, ref)


def test_fingerprint_and_immutable_ingest(tmp_path):
    raw = tmp_path / "transcript.json"
    raw.write_text(json.dumps({"segments": [{"id": 0, "start": 0.0, "end": 10.0, "text": "hello"}]}))
    digest = file_sha256(raw)
    manifest = {"source_sha256": "a" * 64, "duration": 20.0, "raw_transcript": raw.name,
        "transcript_sha256": digest, "language": "en", "backend": "test", "model": "none",
        "ingested_at": datetime.now(timezone.utc).isoformat()}
    (tmp_path / "input_manifest.json").write_text(json.dumps(manifest))
    assert load_evidence(tmp_path).segments[0].text == "hello"
    raw.write_text("{}")
    with pytest.raises(ValueError, match="hash mismatch"):
        load_evidence(tmp_path)


@pytest.mark.parametrize("bad", [{"text": "missing timestamps"}, {"start": 10., "end": 1., "text": "reversed"}])
def test_malformed_segment(bad):
    with pytest.raises((ValueError, KeyError)):
        RawSegment(id="x", range=TimeRange(start=bad["start"], end=bad["end"]), text=bad["text"])


def test_candidate_dedup_and_references(evidence):
    chunk = make_chunks(evidence)[0]
    value = BlockResponse.model_validate({"candidates": [{"start_segment_id": "s1", "end_segment_id": "s2",
        "role": "setup", "summary": "Event", "entities": [], "topics": ["Minecraft"], "confidence": .8}]})
    a = candidate_from_wire(evidence, chunk, value.candidates[0], Backend([]))
    unique, removed = deduplicate([a, a])
    assert len(unique) == len(removed) == 1
    value.candidates[0].start_segment_id = "s3"
    value.candidates[0].end_segment_id = "s4"
    b = candidate_from_wire(evidence, chunk, value.candidates[0], Backend([]))
    assert len(deduplicate([a, b])[0]) == 2  # same theme != duplicate
    value.candidates[0].start_segment_id = "fake"
    with pytest.raises(ValueError):
        candidate_from_wire(evidence, chunk, value.candidates[0], Backend([]))


def test_contiguous_baseline_uniform_validation(evidence):
    plan, context, decision = contiguous_baseline(evidence, "find answer", 60., 45.)
    from ai_editor_copilot.planner.validation import validate_plan
    validate_plan(plan, context)
    m = metrics(plan, evidence)
    assert m["source_jumps"] == 0
    assert m["duration"] == 60
    assert m["overlap_violations"] == 0
    assert variance([plan, plan, plan])["selection_pairwise_jaccard"] == 1
    public, key = blind_package("case", evidence, plan, plan, 123)
    assert set(key["mapping"].values()) == {"baseline", "hierarchical"}
    assert "generator" not in json.dumps(public)
    assert "baseline" not in json.dumps(public)


def test_typed_refusal(evidence):
    result, _, _ = contiguous_baseline(evidence, "story", 2., 1.)
    assert isinstance(result, Refusal)
    assert metrics(result, evidence)["refusal"]
    assert variance([result] * 3)["refusal_rate"] == 1


def test_bounded_repair_and_records():
    good = '{"candidates": [], "no_candidate_reason": "No evidence"}'
    calls = Calls(Backend(['{"candidates":', good]))
    value = calls.request("test", "prompt", BlockResponse, lambda x: None)
    assert not value.candidates
    assert len(calls.records) == 2
    assert calls.records[0]["valid"] is False
    assert calls.records[1]["latency_seconds"] >= 0
    with pytest.raises(ValueError, match="CONTEXT_BUDGET"):
        calls.request("test", "x" * 60000, BlockResponse, lambda x: None)


def test_held_out_loading(tmp_path):
    path = tmp_path / "tasks.json"
    path.write_text(json.dumps([dict(id="a", source_id="src", split="held_out", scenario="far payoff",
        request="Make narrative", target_duration=60., minimum_duration=45.)]))
    assert load_tasks(path) == []
    with pytest.raises(ValueError, match="held-out"):
        load_tasks(path, "held_out")
    assert len(load_tasks(path, "held_out", True)) == 1


def test_invalid_human_rating():
    with pytest.raises(ValueError):
        HumanRating(evaluator_id="x", case_id="x", label="A", hook=6)


def narrative_candidates(evidence):
    candidates = []
    for start, role in [(0, "hook"), (100, "setup"), (280, "payoff")]:
        chunk = {"id": "chunk", "segments": [{"id": s.id} for s in evidence.segments]}
        value = BlockResponse.model_validate({"candidates": [{"start_segment_id": f"s{start}",
            "end_segment_id": f"s{start+1}", "role": role, "summary": "Evidence-based inference",
            "entities": [], "topics": [], "confidence": .8}]})
        candidates.append(candidate_from_wire(evidence, chunk, value.candidates[0], Backend([])))
    choices = [Chosen(candidate_id=c.id, role=c.role, reason="test reason") for c in candidates]
    edges = [StoryEdge(from_candidate=a.id, to_candidate=b.id, relation="explains",
        evidence_segment_ids=[a.evidence.source_segment_ids[0], b.evidence.source_segment_ids[0]],
        rationale="Test dependency inference", confidence=.7) for a, b in zip(candidates, candidates[1:])]
    decision = Decision(status="PLAN", story="synthetic test story", selected=choices, edges=edges,
        reason_codes=[], explanation="")
    return candidates, decision


def test_hierarchical_selection_and_exact_evidence(evidence, tmp_path):
    candidates, decision = narrative_candidates(evidence)
    backend = Backend([decision.model_dump_json()] * 2)
    planner = HierarchicalPlanner(backend, tmp_path)
    plan, context, _ = planner.select(evidence, candidates, "test narrative")
    assert len(plan.sequence) == 3
    assert metrics(plan, evidence)["source_jumps"] == 2
    assert all(s.transcript == " ".join(x.text for x in resolve(evidence, c.evidence))
        for s, c in zip(context.timeline.segments, candidates))
    assert len(planner.calls.records) == 2
    assert "RAW EVIDENCE" in planner.calls.records[1]["prompt"]


@pytest.mark.parametrize("mutation", ["missing", "unknown", "unsupported", "future_context", "contradiction", "duplicate"])
def test_story_dependency_validation(evidence, mutation):
    candidates, decision = narrative_candidates(evidence)
    if mutation == "missing":
        decision.edges = []
    elif mutation == "unknown":
        decision.edges[0].to_candidate = "unknown"
    elif mutation == "unsupported":
        decision.edges[0].evidence_segment_ids = ["s90"]
    elif mutation == "future_context":
        decision.edges[0].relation = "requires_context"
    elif mutation == "contradiction":
        decision.edges[0].relation = "contradicts"
    else:
        decision.selected.append(decision.selected[0])
    with pytest.raises(ValueError):
        validate_graph(decision, candidates)


def test_model_refusal_no_forced_story(evidence, tmp_path):
    candidates, _ = narrative_candidates(evidence)
    decision = Decision(status="NO_COHERENT_STORY", story="", selected=[], edges=[],
        reason_codes=["MISSING_PAYOFF"], explanation="No supported resolution")
    planner = HierarchicalPlanner(Backend([decision.model_dump_json()]), tmp_path)
    result, context, _ = planner.select(evidence, candidates, "test")
    assert isinstance(result, Refusal) and context is None


def test_analysis_cache_reuses_blocks(evidence, tmp_path):
    outputs = ['{"candidates":[],"no_candidate_reason":"No useful event"}'] * len(make_chunks(evidence))
    planner = HierarchicalPlanner(Backend(outputs), tmp_path)
    candidates, cov = planner.analyze(evidence)
    assert cov["full_transcript_analyzed"]
    cached = HierarchicalPlanner(Backend([]), tmp_path)
    assert cached.analyze(evidence)[1]["full_transcript_analyzed"]
    assert cached.calls.records == []


def test_raw_zero_duration_is_preserved(evidence):
    from ai_editor_copilot.narrative.evidence import RawPoint
    evidence.segments[0].range = RawPoint(start=0., end=0.)
    evidence.segments[0].text = ""
    chunks = make_chunks(evidence)
    report = coverage(evidence, chunks, [c["id"] for c in chunks])
    assert report["full_transcript_analyzed"]
    assert report["zero_duration_raw_segments"] == ["s0"]
    assert report["empty_raw_segments"] == ["s0"]


def test_baseline_preserves_silence(evidence):
    for s in evidence.segments:
        s.range.end -= 1
    plan, _, _ = contiguous_baseline(evidence, "story", 60., 45.)
    assert metrics(plan, evidence)["source_jumps"] == 0
    assert plan.sequence[0].source_range.end == plan.sequence[1].source_range.start


def test_transport_failure_recorded():
    class Broken(Backend):
        def generate(self, *args):
            raise TimeoutError("test timeout")
    calls = Calls(Broken([]))
    with pytest.raises(ValueError, match="MODEL_TRANSPORT_ERROR"):
        calls.request("test", "prompt", BlockResponse, lambda x: None)
    assert "TimeoutError" in calls.records[0]["transport_error"]


def test_index_pages_not_silently_sliced(tmp_path):
    class Ranking(Backend):
        def generate(self, context, prompt, schema):
            page = json.loads(prompt.split("\n", 1)[1])["page"]
            return json.dumps({"candidate_ids": [v["id"] for v in page[:2]], "reason": "test ranking"})
    planner = HierarchicalPlanner(Ranking([]), tmp_path)
    summaries = [{"id": f"c{i}", "summary": "x" * 350} for i in range(100)]
    result = planner.shortlist(summaries, "story")
    assert result
    considered = {c for p in planner.retrieval_audit if p["level"] == 0 for c in p["considered"]}
    assert considered == {c["id"] for c in summaries}
    assert all(set(p["selected"]) | set(p["omitted"]) == set(p["considered"]) for p in planner.retrieval_audit)


def test_invalid_candidate_does_not_erase_valid_sibling(evidence, tmp_path):
    evidence.segments = evidence.segments[:10]
    item = {"start_segment_id": "s0", "end_segment_id": "s1", "role": "setup", "summary": "valid",
        "entities": [], "topics": [], "confidence": .8}
    bad = {**item, "end_segment_id": "s8", "summary": "too long"}
    planner = HierarchicalPlanner(Backend([json.dumps({"candidates": [item, bad]})]), tmp_path)
    candidates, report = planner.analyze(evidence)
    assert len(candidates) == 1
    assert len(report["rejected_candidates"]) == 1
    assert report["full_transcript_analyzed"]
    assert report["candidate_validation_policy"].startswith("v2")


def test_refusal_reason_list_is_bounded_and_unique():
    data = dict(status="NO_COHERENT_STORY", story="", selected=[], edges=[],
        reason_codes=["WEAK_EVIDENCE"] * 4, explanation="No evidence")
    with pytest.raises(ValueError):
        Decision.model_validate(data)
    data["reason_codes"] = ["WEAK_EVIDENCE"] * 2
    with pytest.raises(ValueError, match="duplicate"):
        Decision.model_validate(data)


def test_large_invalid_output_does_not_truncate_source_for_repair(tmp_path):
    bad = "[" + "x" * 25000
    calls = Calls(Backend([bad, '{"candidates":[],"no_candidate_reason":"none"}']), tmp_path)
    calls.request("test", "COMPLETE ORIGINAL EVIDENCE", BlockResponse, lambda x: None)
    assert "COMPLETE ORIGINAL EVIDENCE" in calls.records[1]["prompt"]
    assert "invalid prior output omitted" in calls.records[1]["prompt"]
    records = [json.loads(p.read_text()) for p in tmp_path.glob("*.json")]
    assert any(r.get("response") == bad for r in records)
