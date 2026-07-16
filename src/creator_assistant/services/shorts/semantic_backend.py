from __future__ import annotations

import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from pydantic import BaseModel, Field, ValidationError

from creator_assistant.domain.job import CancellationToken
from creator_assistant.services.shorts.semantic_models import (
    GlobalSelectionResponse,
    SemanticBatchResponse,
    SemanticCandidateInput,
    SemanticCandidateScore,
)


PROMPT_VERSION = "shorts-semantic-v1"
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


class TitleTranslationResponse(BaseModel):
    translation: str = Field(min_length=2, max_length=160)


class HookSuggestion(BaseModel):
    id: str = ""
    text: str = Field(min_length=2, max_length=80)
    score: int = Field(ge=1, le=100)
    reason: str = Field(min_length=2, max_length=240)


class HookSuggestionsResponse(BaseModel):
    candidate_id: str = ""
    suggestions: List[HookSuggestion] = Field(min_length=3, max_length=3)
    recommended_id: str = "hook_1"


class HookBatchResponse(BaseModel):
    results: List[HookSuggestionsResponse] = Field(min_length=1)


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


@dataclass(frozen=True)
class OllamaModelInfo:
    name: str
    size: int = 0
    parameter_size: str = ""
    quantization: str = ""
    digest: str = ""
    modified_at: str = ""
    capabilities: tuple[str, ...] = ()

    @classmethod
    def from_api(cls, value: Dict[str, Any]) -> "OllamaModelInfo":
        details = value.get("details") if isinstance(value.get("details"), dict) else {}
        return cls(
            name=str(value.get("name") or value.get("model") or ""),
            size=int(value.get("size", 0) or 0),
            parameter_size=str(details.get("parameter_size", "")),
            quantization=str(details.get("quantization_level", "")),
            digest=str(value.get("digest", "")),
            modified_at=str(value.get("modified_at", "")),
            capabilities=tuple(str(item) for item in value.get("capabilities", []) if item),
        )

    @property
    def recommendation(self) -> str:
        if self.name == "qwen3.6:35b-a3b":
            return "Глубокий анализ"
        if self.name == "qwen3:14b":
            return "Быстрее"
        return ""

    @property
    def display(self) -> str:
        size = f"{self.size / 1_000_000_000:.1f} ГБ" if self.size else "размер неизвестен"
        label = self.name
        if self.name == "qwen3.6:35b-a3b":
            label = "Qwen3.6 35B-A3B"
        elif self.name == "qwen3:14b":
            label = "Qwen3 14B"
        suffix = f" · {self.recommendation}" if self.recommendation else f" · {size}"
        return f"{label}{suffix}"

    @property
    def tooltip(self) -> str:
        size = f"{self.size / 1_000_000_000:.1f} ГБ" if self.size else "размер неизвестен"
        return (
            f"{self.name}\n"
            f"Размер: {size}\n"
            f"Параметры: {self.parameter_size or '—'}\n"
            f"Квант: {self.quantization or '—'}\n"
            f"Digest: {self.digest or '—'}\n"
            f"Изменена: {self.modified_at or '—'}\n"
            f"Capabilities: {', '.join(self.capabilities) or '—'}"
        )


def choose_installed_model(models: List[OllamaModelInfo], saved: str = "") -> Optional[OllamaModelInfo]:
    by_name = {item.name: item for item in models if item.name}
    if saved in by_name:
        return by_name[saved]
    for preferred in ("qwen3.6:35b-a3b", "qwen3:14b"):
        if preferred in by_name:
            return by_name[preferred]
    return models[0] if models else None


MODE_PROFILES = {
    "fast": {"context_length": 8192, "preliminary_count": 24, "batch_size": 10, "global_passes": 0, "think": False},
    "balanced": {"context_length": 16384, "preliminary_count": 40, "batch_size": 8, "global_passes": 1, "think": False},
    "deep": {"context_length": 32768, "preliminary_count": 56, "batch_size": 6, "global_passes": 2, "think": True},
    # Migration alias used by the first implementation.
    "quality": {"context_length": 32768, "preliminary_count": 56, "batch_size": 6, "global_passes": 2, "think": True},
}

CONTENT_TYPE_GUIDANCE = {
    "gaming": "Игровой ролик: реакция, конфликт, опасность, ошибка, победа/поражение, достижение, визуальное изменение, неожиданная развязка, динамика.",
    "education": "Обучающий ролик: конкретная проблема, ясное объяснение, решение, полезный результат, самостоятельность и понятность.",
    "talking": "Разговорный ролик: сильное мнение, история, эмоциональность, спорная мысль, шутка, неожиданный вывод, самостоятельная цитата.",
}


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
        context_length: int = 16384,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.model = model
        self.timeout = max(1.0, float(timeout))
        self.opener = opener or urllib.request.urlopen
        self.keep_alive = keep_alive
        self.context_length = max(2048, min(65536, int(context_length)))
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

    def installed_models(self) -> List[OllamaModelInfo]:
        return [item for item in (OllamaModelInfo.from_api(value) for value in self.list_models()) if item.name]

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

    def runtime_models(self) -> List[Dict[str, Any]]:
        values = self._request("GET", "/api/ps").get("models", [])
        return [item for item in values if isinstance(item, dict)] if isinstance(values, list) else []

    def runtime_info(self) -> Optional[Dict[str, Any]]:
        return next(
            (item for item in self.runtime_models() if item.get("name") == self.model or item.get("model") == self.model),
            None,
        )

    def unload(self) -> None:
        self._request("POST", "/api/generate", {"model": self.model, "keep_alive": 0})

    def compatibility_test(self, cancellation: CancellationToken) -> Dict[str, Any]:
        self.last_metrics = SemanticRunMetrics()
        sample = SemanticCandidateInput(
            candidate_id="compatibility_test", start=0, end=12, duration=12,
            transcript="Я нашёл редкий предмет и успешно завершил испытание.",
            heuristic_score=75, speech_density=1, scene_activity=.2,
            audio_activity=.2, pause_count=0,
        )
        result = self.evaluate([sample], cancellation)
        runtime = self.runtime_info() or {}
        size, size_vram = int(runtime.get("size", 0) or 0), int(runtime.get("size_vram", 0) or 0)
        return {
            "compatible": bool(result), "model": self.model,
            "load_seconds": self.last_metrics.load_duration_ns / 1e9,
            "total_seconds": self.last_metrics.total_duration_ns / 1e9,
            "tokens_per_second": (
                self.last_metrics.eval_count / (self.last_metrics.eval_duration_ns / 1e9)
                if self.last_metrics.eval_duration_ns else 0
            ),
            "size": size, "size_vram": size_vram,
            "gpu_percent": round(100 * size_vram / size, 1) if size else None,
            "context_length": int(runtime.get("context_length", 0) or 0),
        }

    def translate_video_title(self, original_title: str, cancellation: CancellationToken) -> str:
        """Translate a verified original title with the currently selected local model."""
        cancellation.raise_if_cancelled()
        prompt = (
            "Переведи исходное английское название YouTube-видео на естественный русский язык. "
            "Сохрани смысл, числа, имена и игровой контекст. Не добавляй пояснений, кавычек, хэштегов "
            "или новых фактов. Верни только JSON по схеме.\n"
            f"Исходное название: {original_title}"
        )
        response = self._structured_chat(prompt, TitleTranslationResponse, cancellation, think=False)
        return " ".join(response.translation.strip().strip('"«»').split())

    def suggest_short_hooks(self, context: Dict[str, Any], cancellation: CancellationToken) -> HookSuggestionsResponse:
        """Create three independent hooks from the complete selected-candidate story."""
        prompt = (
            "Создай три разных коротких русских hook-заголовка для выбранного YouTube Short. "
            "Проанализируй ВЕСЬ transcript: завязку, проблему, развитие и развязку. Заголовок должен "
            "отражать проблему, необычное событие, цель, ошибку, результат или интригу, а не копировать "
            "случайную первую реплику. Каждый вариант: 3–7 слов, желательно до 45 символов, без точки в "
            "конце; вопросительный или восклицательный знак допустим. Для каждого варианта поставь осмысленную "
            "оценку score от 1 до 100 и кратко объясни reason. Не используй технические названия "
            "вроде «Видос», «Видео», «Minecraft» или «Шортс про игру». Верни ровно три варианта JSON.\n"
            + json.dumps(context, ensure_ascii=False, separators=(",", ":"))
        )
        response = self._structured_chat(prompt, HookSuggestionsResponse, cancellation, think=False)
        if self._hooks_are_weak(response, str(context.get("transcript", ""))):
            repair = (
                prompt
                + "\nПредыдущий ответ был слабым или копировал первую реплику. Перескажи смысл ВСЕГО сюжета, "
                  "учти финал и верни три новых самостоятельных hook-варианта."
            )
            response = self._structured_chat(repair, HookSuggestionsResponse, cancellation, think=False)
        if self._hooks_are_weak(response, str(context.get("transcript", ""))):
            raise SemanticResponseError("Локальная модель не смогла создать три качественных hook-варианта.")
        return response

    def suggest_short_hooks_batch(self, contexts: List[Dict[str, Any]], cancellation: CancellationToken) -> List[HookSuggestionsResponse]:
        cancellation.raise_if_cancelled()
        if not contexts:
            return []
        prompt = (
            "Сгенерируй AI-заголовки для нескольких финальных YouTube Shorts. Для каждого candidate_id верни ровно три "
            "разных коротких русских hook-заголовка, score, reason и recommended_id. Анализируй полный transcript каждого "
            "кандидата, context_before/context_after, развитие и финал. Не копируй первую реплику и не используй технические "
            "слова вроде «Видео», «Видос», «Shorts» или одинокое «Minecraft». Верни только JSON по схеме.\n"
            + json.dumps(contexts, ensure_ascii=False, separators=(",", ":"))
        )
        response = self._structured_chat(prompt, HookBatchResponse, cancellation, think=False)
        requested = {str(item.get("candidate_id")) for item in contexts}
        results = response.results
        returned = [item.candidate_id for item in results]
        if set(returned) != requested or len(returned) != len(set(returned)):
            raise SemanticResponseError("Ollama вернула неизвестные, повторяющиеся или пропущенные candidate_id для hook-заголовков.")
        by_id = {str(item.get("candidate_id")): item for item in contexts}
        for item in results:
            if self._hooks_are_weak(item, str(by_id[item.candidate_id].get("transcript", ""))):
                raise SemanticResponseError("Ollama вернула слабые hook-заголовки в batch.")
        return results

    @staticmethod
    def _hooks_are_weak(response: HookSuggestionsResponse, transcript: str) -> bool:
        suggestions = response.suggestions
        normalized = [" ".join(item.text.strip().split()).casefold().rstrip(".") for item in suggestions]
        if len(set(normalized)) != 3:
            return True
        bad = {"видос", "видео", "minecraft", "шортс про игру", "shorts"}
        first_phrase = " ".join(transcript.strip().split()[:10]).casefold().rstrip(".!?")
        for text in normalized:
            words = text.split()
            if text in bad or not 3 <= len(words) <= 7 or len(text) > 60 or text.endswith("."):
                return True
            if first_phrase and (text == first_phrase or first_phrase.startswith(text)):
                return True
        return False

    def evaluate(self, candidates, cancellation, *, think=False):
        cancellation.raise_if_cancelled()
        content_type = candidates[0].content_type if candidates else "gaming"
        guidance = CONTENT_TYPE_GUIDANCE.get(content_type, CONTENT_TYPE_GUIDANCE["gaming"])
        prompt = (
            "Оцени кандидатов YouTube Shorts по смыслу. Не придумывай новые candidate_id. "
            "Учитывай самостоятельность, hook, конфликт, развитие, развязку, эмоцию, пользу и удержание. "
            f"Профиль анализа: {guidance} "
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
                "options": {"temperature": 0, "num_ctx": self.context_length},
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
