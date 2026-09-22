"""Regenerate checked-in JSON Schema from canonical Pydantic models."""
import json
from pathlib import Path
from ai_editor_copilot.domain.models import SemanticTimeline, StyleProfile, AssetIndex, PlannerInput
from ai_editor_copilot.domain.plan import EditPlan, PlannerProposal
from ai_editor_copilot.tools.registry import ACTION_ADAPTER
from ai_editor_copilot.feedback.events import FeedbackEvent
from ai_editor_copilot.narrative.evidence import EvidenceSource, Annotation
from ai_editor_copilot.narrative.contracts import Candidate, Decision, Refusal
from ai_editor_copilot.narrative.evaluation import Task, HumanRating

SCHEMAS = {"semantic_timeline": SemanticTimeline, "style_profile": StyleProfile,
           "edit_plan": EditPlan, "feedback_event": FeedbackEvent, "asset_index": AssetIndex,
           "planner_input": PlannerInput, "planner_proposal": PlannerProposal,
           "evidence_source": EvidenceSource, "evidence_annotation": Annotation,
           "story_candidate": Candidate, "story_decision": Decision, "narrative_refusal": Refusal,
           "evaluation_task": Task, "human_rating": HumanRating}


def generated():
    values = {name: cls.model_json_schema() for name, cls in SCHEMAS.items()}
    values["edit_action"] = ACTION_ADAPTER.json_schema()
    for name, value in values.items():
        value["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        value["$id"] = f"urn:ai-editor-copilot:{name}:1.0"
    return values


if __name__ == "__main__":
    folder = Path(__file__).resolve().parents[1] / "schemas"
    folder.mkdir(exist_ok=True)
    for name, value in generated().items():
        (folder / (name + ".schema.json")).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
