"""Deterministic contiguous density/relevance baseline, not a semantic quality oracle."""
import re
from datetime import datetime, timezone
from ai_editor_copilot.narrative.evidence import Annotation, reference
from ai_editor_copilot.narrative.contracts import Candidate, Chosen, Decision, StoryEdge, Refusal
from ai_editor_copilot.narrative.pipeline import build_plan
from ai_editor_copilot.planner.service import stable_id, compile_plan
from ai_editor_copilot.domain.models import PlannerInput, SemanticTimeline, SemanticSegment, SourceAsset, TimeRange, UserCommand
from ai_editor_copilot.domain.plan import PlannerProposal, Selection
from ai_editor_copilot.style.profiles import profile
from ai_editor_copilot.tools.registry import Provenance


def contiguous_baseline(source, task, target=60.0, minimum=45.0):
    terms = set(re.findall(r"\w{4,}", task.casefold()))
    best = None
    for a in range(len(source.segments)):
        for b in range(a + 2, len(source.segments)):
            span = source.segments[a:b + 1]
            duration = span[-1].range.end - span[0].range.start
            if duration > target:
                break
            if duration < minimum:
                continue
            # Zero-duration empty ASR entries are evidence, but not standalone clip boundaries.
            if not span[0].text.strip() or not span[-1].text.strip():
                continue
            if any(y.range.start < x.range.end for x, y in zip(span, span[1:])):
                continue
            text = " ".join(s.text for s in span)
            tokens = re.findall(r"\w+", text.casefold())
            score = len(tokens) / duration + 2 * len(terms & set(tokens))
            key = (score, -abs(target - duration), -span[0].range.start)
            if best is None or key > best[0]:
                best = key, span
    if best is None:
        return Refusal(reason_codes=["DURATION_IMPOSSIBLE"],
            explanation="No contiguous complete-segment window satisfies duration and timing constraints"), None, None
    span = best[1]
    # Three contiguous portions give both planners identical structural validation.
    # Role names here are positional proxies, NOT evidence of hook/setup/payoff quality.
    groups = [span[:1], span[1:-1], span[-1:]]
    candidates, chosen, edges = [], [], []
    for role, group in zip(["hook", "setup", "payoff"], groups):
        ref = reference(group)
        ident = stable_id("baseline_", [source.sha256, ref.model_dump()])
        ann = Annotation(type="positional_role_proxy", value=role, status="INFERRED", confidence=0.0,
            evidence=ref, generator="contiguous-density-v1", model="none", created_at=datetime.now(timezone.utc))
        candidates.append(Candidate(id=ident, evidence=ref, annotation=ann, role=role,
            entities=[], topics=[], analyzed_chunk_ids=[]))
        chosen.append(Chosen(candidate_id=ident, role=role, reason="Deterministic positional role; human narrative quality unknown"))
    for a, b in zip(candidates, candidates[1:]):
        edges.append(StoryEdge(from_candidate=a.id, to_candidate=b.id, relation="follows",
            evidence_segment_ids=a.evidence.source_segment_ids[-1:] + b.evidence.source_segment_ids[:1],
            rationale="Adjacent in original source; causality not asserted", confidence=0.0))
    decision = Decision(status="PLAN", story="Best contiguous lexical/density window", selected=chosen,
        edges=edges, reason_codes=[], explanation="No model, no annotation labels used in scoring")
    provenance = Provenance(backend="contiguous-baseline", model="none", prompt_version="density-v1")
    segments, selections = [], []
    for i, (c, group, choice) in enumerate(zip(candidates, groups, chosen)):
        # Preserve source silence between groups, never jump over it in a contiguous baseline.
        end = groups[i + 1][0].range.start if i < 2 else group[-1].range.end
        span_range = TimeRange(start=group[0].range.start, end=end)
        segments.append(SemanticSegment(id=c.id, source_asset_id=source.id, range=span_range,
            transcript=" ".join(s.text for s in group)))
        selections.append(Selection(segment_id=c.id, source_range=span_range,
            narrative_role=choice.role, reason=choice.reason, confidence=0.0))
    context = PlannerInput(timeline=SemanticTimeline(id=source.id,
        sources=[SourceAsset(id=source.id, kind="video", uri="sha256:" + source.sha256,
            duration=source.duration, rights="local research only")], segments=segments),
        style=profile(), command=UserCommand(text=task), target_duration=target, minimum_duration=minimum)
    plan = compile_plan(PlannerProposal(story=decision.story, clips=selections,
        notes="Continuous source interval includes inter-segment silence. Roles are positional proxies only."), context, provenance)
    return plan, context, decision
