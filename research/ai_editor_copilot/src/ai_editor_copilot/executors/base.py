from abc import ABC, abstractmethod
from ai_editor_copilot.planner.validation import validate_plan


class EditPlanExecutor(ABC):
    @abstractmethod
    def execute(self, plan, context):
        raise NotImplementedError


class DryRunExecutor(EditPlanExecutor):
    def execute(self, plan, context):
        plan = validate_plan(plan, context)
        return {"schema_version": "1.0", "plan_id": plan.plan_id, "mode": "dry_run",
            "media_modified": False, "operations": [
                {"id": a.id, "action": a.action, "target": a.target.model_dump(),
                 "source": a.source.model_dump() if a.source else None,
                 "timing": a.timing.model_dump(), "parameters": a.parameters.model_dump(),
                 "reason": a.reason, "status": "proposed_only"} for a in plan.actions]}


class DaVinciExecutor(EditPlanExecutor):
    def execute(self, plan, context):
        raise NotImplementedError("DaVinci execution is unverified and not implemented")


class FFmpegPrototypeExecutor(EditPlanExecutor):
    def execute(self, plan, context):
        raise NotImplementedError("FFmpeg media execution is a future milestone")
