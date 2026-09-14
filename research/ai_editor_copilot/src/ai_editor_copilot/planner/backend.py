from abc import ABC, abstractmethod
from ai_editor_copilot.domain.models import PlannerInput
from ai_editor_copilot.tools.registry import Provenance


class LLMPlannerBackend(ABC):
    """Backend returns strict JSON text, never executable commands."""
    @property
    @abstractmethod
    def provenance(self) -> Provenance:
        raise NotImplementedError

    @abstractmethod
    def generate(self, context: PlannerInput, prompt: str, schema: dict) -> str:
        raise NotImplementedError
