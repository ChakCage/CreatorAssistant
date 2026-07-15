from __future__ import annotations

import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from pydantic import ValidationError

from creator_assistant.domain.job import CancellationToken
from creator_assistant.services.shorts.semantic_models import (
    GlobalSelectionResponse,
    SemanticBatchResponse,
    SemanticCandidateInput,
    SemanticCandidateScore,
)


PROMPT_VERSION = "shorts-semantic-v1"
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


class SemanticBackendError(RuntimeError):
    pass


class SemanticResponseError(SemanticBackendError):
    pass


@dataclass
class SemanticRunMetrics:
    load_duration_ns: int = 0
    prompt_eval_duration_ns: int = 0
    eval_duration_ns: int = 0
    prompt_eval_count: int = 0
    eval_count: int = 0
    total_duration_ns: int = 0
    cache_hits: int = 0
    fallback: bool = False
    warnings: List[str] = field(default_factory=list)


class SemanticScorerBackend(ABC):
    name = "disabled"

    @abstractmethod
    def list_models(self) -> List[Dict[str, Any]]:
        raise NotImplementedError

    @abstractmethod
    def evaluate(
        self,
        candidates: List[SemanticCandidateInput],
        cancellation: CancellationToken,
        *,
        think: bool = False,
    ) -> List[SemanticCandidateScore]:
        raise NotImplementedError

    @abstractmethod
    def global_select(
        self,
        candidates: List[Dict[str, Any]],
        count: int,
        cancellation: CancellationToken,
        *,
        think: bool = False,
    ) -> GlobalSelectionResponse:
        raise NotImplementedError


class DisabledSemanticScorer(SemanticScorerBackend):
    name = "disabled"

    def list_models(self) -> List[Dict[str, Any]]:
        return []

    def evaluate(self, candidates, cancellation, *, think=False):
        return []

    def global_select(self, candidates, count, cancellation, *, think=False):
        return GlobalSelectionResponse(candidate_ids=[item["candidate_id"] for item in candidates[:count]])


class HeuristicFallbackScorer(SemanticScorerBackend):
    name = "heuristic-fallback"

    def list_models(self) -> List[Dict[str, Any]]:
        return []

    def evaluate(self, candidates, cancellation, *, think=False):
        results = []
        for item in candidates:
            cancellation.raise_if_cancelled()
            score = float(item.heuristic_score)
            results.append(SemanticCandidateScore(
                candidate_id=item.candidate_id,
                semantic_score=score, hook_score=score, context_independence=score,
                conflict_score=score, development_score=score, payoff_score=score,
                emotion_score=score, entertainment_score=score, usefulness_score=score,
                retention_score=score, completeness_score=score, moment_type="other",
                verdict="Эвристическая оценка", reason="Локальная AI-оценка недоступна.",
                weaknesses=[], suggested_start=item.start, suggested_end=item.end,
            ))
        return results

    def global_select(self, candidates, count, cancellation, *, think=False):
        cancellation.raise_if_cancelled()
        ranked = sorted(candidates, key=lambda item: -float(item.get("final_score", 0)))
        return GlobalSelectionResponse(candidate_ids=[item["candidate_id"] for item in ranked[:count]])


class OllamaSemanticScorer(SemanticScorerBackend):
    name = "ollama"

    def __init__(
        self,
        endpoint: str = "http://127.0.0.1:11434",
        model: str = "qwen3:14b",
        timeout: float = 180.0,
        opener: Optional[Callable[..., Any]] = None,
        keep_alive: str = "10m",
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.model = model
        self.timeout = max(1.0, float(timeout))
        self.opener = opener or urllib.request.urlopen
        self.keep_alive = keep_alive
        self.last_metrics = SemanticRunMetrics()

    @property
    def is_local(self) -> bool:
        host = (urllib.parse.urlparse(self.endpoint).hostname or "").casefold()
        return host in LOCAL_HOSTS

    def require_local(self) -> None:
        if not self.is_local:
            raise SemanticBackendError("Для Ollama разрешён только локальный адрес 127.0.0.1, localhost или ::1.")

    def list_models(self) -> List[Dict[str, Any]]:
        self.require_local()
        payload = self._request("GET", "/api/tags")
        models = payload.get("models", [])
        if not isinstance(models, list):
            raise SemanticResponseError("Ollama вернула некорректный список моделей.")
        return [item for item in models if isinstance(item, dict)]

    def model_info(self) -> Dict[str, Any]:
        for item in self.list_models():
            if item.get("name") == self.model or item.get("model") == self.model:
                return item
        raise SemanticBackendError(
            f"Модель {self.model} не установлена. Выполните: ollama pull {self.model}"
        )

    def check(self, cancellation: Optional[CancellationToken] = None) -> Dict[str, Any]:
        if cancellation:
            cancellation.raise_if_cancelled()
        info = self.model_info()
        if cancellation:
            cancellation.raise_if_cancelled()
        return info

    def evaluate(self, candidates, cancellation, *, think=False):
        cancellation.raise_if_cancelled()
        prompt = (
            "Оцени кандидатов YouTube Shorts по смыслу. Не придумывай новые candidate_id. "
            "Учитывай самостоятельность, hook, конфликт, развитие, развязку, эмоцию, пользу и удержание. "
            "Верни только JSON по переданной схеме. Данные:\n" +
            json.dumps([item.model_dump() for item in candidates], ensure_ascii=False, separators=(",", ":"))
        )
        response = self._structured_chat(prompt, SemanticBatchResponse, cancellation, think=think)
        requested = {item.candidate_id for item in candidates}
        returned = [item.candidate_id for item in response.results]
        if len(returned) != len(set(returned)) or set(returned) != requested:
            raise SemanticResponseError("AI вернула неизвестные, повторяющиеся или пропущенные candidate_id.")
        return response.results

    def global_select(self, candidates, count, cancellation, *, think=False):
        cancellation.raise_if_cancelled()
        prompt = (
            f"Выбери Top-{count} разных самостоятельных сюжетных моментов. Не выбирай дубли, "
            "обеспечь разнообразие и используй только существующие candidate_id. Верни только JSON.\n" +
            json.dumps(candidates, ensure_ascii=False, separators=(",", ":"))
        )
        response = self._structured_chat(prompt, GlobalSelectionResponse, cancellation, think=think)
        allowed = {str(item.get("candidate_id")) for item in candidates}
        if len(response.candidate_ids) != len(set(response.candidate_ids)):
            raise SemanticResponseError("Глобальный выбор содержит повторяющиеся candidate_id.")
        if any(item not in allowed for item in response.candidate_ids):
            raise SemanticResponseError("Глобальный выбор содержит несуществующий candidate_id.")
        response.candidate_ids = response.candidate_ids[:count]
        return response

    def _structured_chat(self, prompt, model_class, cancellation, *, think):
        error = ""
        for attempt in range(2):
            cancellation.raise_if_cancelled()
            messages = [{"role": "system", "content": "Ты строгий JSON-анализатор видеосюжетов."},
                        {"role": "user", "content": prompt}]
            if error:
                messages.append({"role": "user", "content": f"Исправь ответ. Ошибка схемы: {error}"})
            payload = {
                "model": self.model,
                "messages": messages,
                "stream": False,
                "format": model_class.model_json_schema(),
                "think": bool(think),
                "keep_alive": self.keep_alive,
                "options": {"temperature": 0},
            }
            raw = self._request("POST", "/api/chat", payload)
            self._record_metrics(raw)
            cancellation.raise_if_cancelled()
            try:
                content = raw["message"]["content"]
                return model_class.model_validate_json(content)
            except (KeyError, TypeError, ValueError, ValidationError) as exc:
                error = str(exc)[:800]
        raise SemanticResponseError(f"Ollama дважды вернула невалидный structured response: {error}")

    def _record_metrics(self, raw: Dict[str, Any]) -> None:
        for source, target in (
            ("load_duration", "load_duration_ns"),
            ("prompt_eval_duration", "prompt_eval_duration_ns"),
            ("eval_duration", "eval_duration_ns"),
            ("prompt_eval_count", "prompt_eval_count"),
            ("eval_count", "eval_count"),
            ("total_duration", "total_duration_ns"),
        ):
            setattr(self.last_metrics, target, getattr(self.last_metrics, target) + int(raw.get(source, 0) or 0))

    def _request(self, method: str, path: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        self.require_local()
        data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.endpoint + path, data=data, method=method,
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        try:
            with self.opener(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, socket.timeout, OSError) as exc:
            raise SemanticBackendError(f"Ollama API недоступен: {exc}") from exc
        try:
            value = json.loads(raw)
        except ValueError as exc:
            raise SemanticResponseError("Ollama API вернула невалидный JSON.") from exc
        if not isinstance(value, dict):
            raise SemanticResponseError("Ollama API вернула неожиданный тип ответа.")
        if value.get("error"):
            raise SemanticBackendError(str(value["error"]))
        return value

