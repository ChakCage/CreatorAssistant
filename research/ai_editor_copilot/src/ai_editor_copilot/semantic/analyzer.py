from typing import Protocol
from ai_editor_copilot.domain.models import SemanticTimeline


class SemanticAnalyzer(Protocol):
    def analyze(self, timeline: SemanticTimeline) -> SemanticTimeline:
        """Enrich transcript with evidence-backed annotations in future phases."""
        ...


class TranscriptOnlyAnalyzer:
    def analyze(self, timeline):
        # Missing visual evidence stays null, not a hallucinated negative result.
        return SemanticTimeline.model_validate_json(timeline.model_dump_json())
