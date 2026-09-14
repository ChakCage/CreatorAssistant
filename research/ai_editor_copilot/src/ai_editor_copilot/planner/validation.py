"""Cross-reference and temporal validation; never trust a deserialized plan."""
from ai_editor_copilot.domain.models import PlannerInput
from ai_editor_copilot.domain.plan import EditPlan


class PlanError(ValueError):
    pass


def validate_plan(plan: EditPlan, context: PlannerInput) -> EditPlan:
    # Revalidate even a mutable model that came from another caller.
    plan = EditPlan.model_validate_json(plan.model_dump_json())
    context = PlannerInput.model_validate_json(context.model_dump_json())
    if context.current_timeline is not None:
        raise PlanError("editing an existing timeline is not implemented; no silent replacement")
    if (plan.user_request != context.command or plan.target_format != context.target_format
            or plan.target_duration != context.target_duration
            or plan.minimum_duration != context.minimum_duration or plan.style_profile != context.style):
        raise PlanError("plan changed caller constraints")
    if plan.source_references != context.timeline.sources + context.assets.assets:
        raise PlanError("plan changed source metadata")
    segments = {s.id: s for s in context.timeline.segments}
    sources = {s.id: s for s in plan.source_references}
    clips = {c.id: c for c in plan.sequence}
    if len(clips) != len(plan.sequence):
        raise PlanError("duplicate clip ID")
    offset = 0.0
    ranges = []
    for c in plan.sequence:
        s = segments.get(c.segment_id)
        if s is None or c.source_asset_id != s.source_asset_id:
            raise PlanError("unknown segment or mismatched source")
        r = c.source_range
        if r.start < s.range.start or r.end > s.range.end:
            raise PlanError("selection outside transcript segment/source boundaries")
        if abs(c.timeline_start - offset) > 1e-6:
            raise PlanError("sequence must be gap-free and non-overlapping in output time")
        for asset_id, previous in ranges:
            if asset_id == c.source_asset_id and min(r.end, previous.end) > max(r.start, previous.start) + 1e-6:
                raise PlanError("duplicate/overlapping source selection")
        ranges.append((c.source_asset_id, r))
        offset += r.end - r.start
    if not context.minimum_duration - 1e-6 <= offset <= context.target_duration + 1e-6:
        raise PlanError(f"duration {offset:.3f} outside [{context.minimum_duration}, {context.target_duration}]")
    roles = [c.narrative_role for c in plan.sequence]
    if roles[0] != "hook" or roles[-1] != "payoff" or "setup" not in roles:
        raise PlanError("narrative requires first hook, setup, final payoff")
    expected = [(c.narrative_role, c.id, c.reason) for c in plan.sequence]
    if [(b.role, b.clip_id, b.reason) for b in plan.narrative_structure] != expected:
        raise PlanError("narrative beats must match sequence")
    actions = {a.id: a for a in plan.actions}
    if len(actions) != len(plan.actions):
        raise PlanError("duplicate action ID")
    seen = set()
    inserted = set()
    for a in plan.actions:
        if len(set(a.dependencies)) != len(a.dependencies) or not set(a.dependencies) <= seen:
            raise PlanError("dependencies must exist earlier; no cycles/forward references")
        seen.add(a.id)
        if a.target.kind != "clip" or a.target.id not in clips:
            raise PlanError("v1 dry-run supports only known output clip targets")
        clip = clips[a.target.id]
        end = a.timing.timeline_start + a.timing.duration
        clip_end = clip.timeline_start + clip.source_range.end - clip.source_range.start
        if a.timing.timeline_start < clip.timeline_start - 1e-6 or end > clip_end + 1e-6:
            raise PlanError("action outside target clip")
        if a.action != "CUT" and a.timing.duration <= 0:
            raise PlanError("non-CUT action needs positive duration")
        if a.action == "CUT" and a.timing.duration != 0:
            raise PlanError("CUT is instantaneous")
        if a.source is not None and a.source.type == "asset":
            asset = sources.get(a.source.asset_id)
            if asset is None:
                raise PlanError("unknown action source asset")
            if a.source.range and (asset.duration is None or a.source.range.end > asset.duration):
                raise PlanError("action source range outside asset")
        if a.action.startswith("INSERT_") or a.action in {"ADD_MUSIC", "ADD_SFX"}:
            if a.source is None:
                raise PlanError("media action requires source")
        if a.action == "INSERT_VIDEO" and a.target.id not in inserted:
            if (a.source is None or a.source.type != "asset" or a.source.asset_id != clip.source_asset_id
                    or a.source.range != clip.source_range or abs(a.timing.timeline_start - clip.timeline_start) > 1e-6
                    or abs(end - clip_end) > 1e-6):
                raise PlanError("base INSERT_VIDEO must match sequence exactly")
            inserted.add(a.target.id)
        elif a.action == "INSERT_VIDEO":
            raise PlanError("duplicate base clip insertion")
        if a.action == "CROP" and (a.parameters.left + a.parameters.right >= 1
                                   or a.parameters.top + a.parameters.bottom >= 1):
            raise PlanError("crop removes entire frame")
    if inserted != set(clips):
        raise PlanError("every sequence clip must be inserted exactly once")
    return plan
