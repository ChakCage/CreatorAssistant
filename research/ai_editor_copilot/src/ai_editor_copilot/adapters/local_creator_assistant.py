"""The only dependency on Creator Assistant's backend; imported only when selected."""
from ai_editor_copilot.planner.backend import LLMPlannerBackend
from ai_editor_copilot.tools.registry import Provenance


class LocalCreatorAssistantLLMBackend(LLMPlannerBackend):
    def __init__(self, scorer=None):
        if scorer is None:
            from creator_assistant.services.shorts.semantic_backend import OllamaSemanticScorer
            scorer = OllamaSemanticScorer(model="qwen3.6:35b-a3b", timeout=900,
                warmup_timeout=900, context_length=32768, keep_alive="10m")
        self.scorer = scorer
        if scorer.model != "qwen3.6:35b-a3b":
            raise ValueError("this adapter's current verified model is qwen3.6:35b-a3b")
        scorer.require_local()

    @property
    def provenance(self):
        return Provenance(backend="creator-assistant-local", model=self.scorer.model, prompt_version="narrative-v1")

    def prepare(self):
        from creator_assistant.domain.job import CancellationToken
        info = self.scorer.preflight()
        self.scorer.warm_up(CancellationToken())
        return info

    def generate(self, context, prompt, schema):
        # Existing transport is private: this dependency is pinned by an adapter contract test.
        # Keep schema/semantic repair in EditPlanner, not in the older scorer's retry loop.
        raw = self.scorer._stream_chat({
            "model": self.scorer.model, "stream": True, "think": False, "format": schema,
            "keep_alive": self.scorer.keep_alive,
            "messages": [{"role": "system", "content": "Return only valid edit-planning JSON."},
                         {"role": "user", "content": prompt}],
            "options": {"temperature": 0, "num_ctx": self.scorer.context_length, "num_predict": 6000},
        })
        self.scorer._record_metrics(raw)
        content = raw.get("message", {}).get("content")
        if not isinstance(content, str):
            raise ValueError("local backend returned no JSON content")
        return content
