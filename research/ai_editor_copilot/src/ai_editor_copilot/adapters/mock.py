"""Deterministic annotated-fixture baseline, NOT a general language understanding model."""
import re
from collections import defaultdict
from ai_editor_copilot.planner.backend import LLMPlannerBackend
from ai_editor_copilot.tools.registry import Provenance
from ai_editor_copilot.domain.plan import PlannerProposal, Selection


class MockPlannerBackend(LLMPlannerBackend):
    def __init__(self, responses=None):
        self.responses = list(responses) if responses is not None else None
        self.calls = 0
        self.prompts = []

    @property
    def provenance(self):
        return Provenance(backend="mock", model="annotated-fixture-baseline", prompt_version="narrative-v1")

    def generate(self, context, prompt, schema):
        self.prompts.append(prompt)
        self.calls += 1
        if self.responses is not None:
            return self.responses[min(self.calls - 1, len(self.responses) - 1)]
        words = set(re.findall(r"\w{3,}", context.command.text.casefold()))
        stories = defaultdict(list)
        for s in context.timeline.segments:
            if s.story_id and s.narrative_role:
                stories[s.story_id].append(s)
        if not stories:
            raise ValueError("mock requires annotated story/role fixtures; use local backend for raw transcript")
        def relevance(group):
            content = " ".join(s.transcript + " " + (s.topic or "") for s in group).casefold()
            return sum(word in content for word in words), sum(s.importance or 0 for s in group)
        story, group = sorted(stories.items(), key=lambda p: (relevance(p[1]), p[0]), reverse=True)[0]
        selected = []
        for role in ("hook", "setup", "development", "climax", "payoff"):
            choices = [s for s in group if s.narrative_role == role]
            if choices:
                s = sorted(choices, key=lambda s: (-(s.importance or 0), s.id))[0]
                selected.append(Selection(segment_id=s.id, source_range=s.range, narrative_role=role,
                    reason=f"Annotated {role} in story {story}; deterministic fixture baseline.", confidence=0.8))
        return PlannerProposal(story=story, clips=selected,
            notes="Mock uses supplied role/story annotations; natural-language reasoning not demonstrated.").model_dump_json()
