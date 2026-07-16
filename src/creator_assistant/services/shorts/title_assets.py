from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List

from creator_assistant.domain.job import CancellationToken
from creator_assistant.domain.shorts.models import Candidate, SourceInfo, Transcript
from creator_assistant.services.shorts.manifest import utc_now
from creator_assistant.services.shorts.semantic_backend import (
    HookSuggestionsResponse,
    OllamaSemanticScorer,
    SemanticBackendError,
)
from creator_assistant.services.shorts.semantic_cache import SemanticCache
from creator_assistant.services.shorts.title_service import ShortTitleService


TITLE_TRANSLATION_PROMPT_VERSION = "title_translation_v2"
SHORT_HOOK_PROMPT_VERSION = "short_hook_v3"


@dataclass
class TitleAssetResult:
    translated_ready: bool = False
    hook_ready: int = 0
    hook_unavailable: int = 0
    cache_hits: int = 0
    model: str = ""
    model_digest: str = ""
    elapsed_seconds: float = 0.0
    errors: List[str] = field(default_factory=list)


class ShortTitleAssetService:
    def __init__(self, title_service: ShortTitleService | None = None) -> None:
        self.title_service = title_service or ShortTitleService()

    def prepare(
        self,
        *,
        source: SourceInfo,
        paths,
        manifest,
        candidates: List[Candidate],
        transcript: Transcript,
        backend,
        ai_settings: Dict[str, Any],
        cache: SemanticCache,
        cancellation: CancellationToken,
        content_type: str,
        batch_size: int = 4,
        progress=None,
    ) -> TitleAssetResult:
        import time

        started = time.monotonic()
        result = TitleAssetResult()
        original = self.title_service.resolve_original_title(source, paths)
        existing_title_assets = dict(getattr(manifest, "title_assets", {}) or {})
        title_assets = dict(existing_title_assets)
        title_assets.setdefault("original_title", original.title)
        title_assets.setdefault("original_title_source", original.source)
        title_assets["original_title"] = original.title
        title_assets["original_title_source"] = original.source

        model, digest = self._model_identity(backend, ai_settings)
        result.model, result.model_digest = model, digest
        if not isinstance(backend, OllamaSemanticScorer) or not ai_settings.get("enabled", False):
            title_assets.update({
                "translated_title": str(title_assets.get("translated_title") or ""),
                "model": model,
                "model_digest": digest,
                "prompt_version": TITLE_TRANSLATION_PROMPT_VERSION,
                "timestamp": utc_now(),
                "status": "unavailable",
            })
            for candidate in candidates:
                self._apply_unavailable(candidate, original, model, digest, "Ollama disabled")
            manifest.title_assets = title_assets
            result.hook_unavailable = len(candidates)
            result.elapsed_seconds = time.monotonic() - started
            return result

        try:
            title_key_payload = {
                "kind": "title_translation",
                "original_video_title": original.title,
                "model": model,
                "digest": digest,
                "prompt_version": TITLE_TRANSLATION_PROMPT_VERSION,
                "target_language": "ru",
            }
            cached_title = cache.get(title_key_payload) if ai_settings.get("cache", True) else None
            if cached_title:
                translated = str(cached_title.get("translated_title") or "").strip()
                result.cache_hits += 1
            else:
                if progress:
                    progress("Перевод названия видео", f"{model}: перевод исходного названия.", 89)
                translated = backend.translate_video_title(original.title, cancellation)
                if ai_settings.get("cache", True):
                    cache.put(title_key_payload, {"translated_title": translated, "timestamp": utc_now()})
            title_assets.update({
                "translated_title": translated,
                "model": model,
                "model_digest": digest,
                "prompt_version": TITLE_TRANSLATION_PROMPT_VERSION,
                "timestamp": utc_now(),
                "status": "ready",
            })
            result.translated_ready = True
        except Exception as exc:
            if cancellation.is_cancelled:
                cancellation.raise_if_cancelled()
            message = str(exc).strip() or type(exc).__name__
            title_assets.update({
                "translated_title": str(title_assets.get("translated_title") or ""),
                "model": model,
                "model_digest": digest,
                "prompt_version": TITLE_TRANSLATION_PROMPT_VERSION,
                "timestamp": utc_now(),
                "status": "unavailable",
                "error": message,
            })
            result.errors.append(message)

        manifest.title_assets = title_assets
        translated_title = str(title_assets.get("translated_title") or "")
        for candidate in candidates:
            self._ensure_branding(candidate, original, translated_title)

        total = len(candidates)
        for offset in range(0, total, max(1, batch_size)):
            cancellation.raise_if_cancelled()
            batch = candidates[offset:offset + max(1, batch_size)]
            prepared = offset
            if progress:
                progress(
                    "Подготовка заголовков кандидатов",
                    f"{model}: обработано {prepared} из {total}; cache hits {result.cache_hits}.",
                    89 + min(3, int(3 * prepared / max(1, total))),
                )
            contexts, pending = [], []
            for candidate in batch:
                payload = self._hook_cache_payload(
                    candidate, transcript, model, digest, content_type, original.title
                )
                cached = cache.get(payload) if ai_settings.get("cache", True) else None
                if cached:
                    self._apply_hook_response(candidate, cached, model, digest, cached=True)
                    result.cache_hits += 1
                    result.hook_ready += 1
                else:
                    context = self.title_service.hook_context(candidate, transcript)
                    context.update({
                        "context_before": self._context_before(candidate, transcript),
                        "context_after": self._context_after(candidate, transcript),
                        "content_type": content_type,
                        "original_video_title": original.title,
                        "translated_video_title": translated_title,
                        "heuristic_score": candidate.heuristic_score,
                        "semantic_score": candidate.semantic_score,
                    })
                    contexts.append(context)
                    pending.append((candidate, payload))
            if contexts:
                try:
                    if hasattr(backend, "suggest_short_hooks_batch"):
                        responses = backend.suggest_short_hooks_batch(contexts, cancellation)
                    else:
                        responses = [backend.suggest_short_hooks(context, cancellation) for context in contexts]
                    by_id = {item.candidate_id: item for item in responses}
                    for candidate, payload in pending:
                        response = by_id.get(candidate.id)
                        if not response:
                            raise SemanticBackendError(f"No hook response for {candidate.id}")
                        value = self._response_payload(response, model, digest)
                        if ai_settings.get("cache", True):
                            cache.put(payload, value)
                        self._apply_hook_response(candidate, value, model, digest)
                        result.hook_ready += 1
                except Exception as exc:
                    if cancellation.is_cancelled:
                        cancellation.raise_if_cancelled()
                    message = str(exc).strip() or type(exc).__name__
                    result.errors.append(message)
                    # One retry using the safer single-candidate path.
                    for candidate, payload in pending:
                        try:
                            context = self.title_service.hook_context(candidate, transcript)
                            context.update({
                                "content_type": content_type,
                                "original_video_title": original.title,
                                "translated_video_title": translated_title,
                                "heuristic_score": candidate.heuristic_score,
                                "semantic_score": candidate.semantic_score,
                            })
                            response = backend.suggest_short_hooks(context, cancellation)
                            response.candidate_id = candidate.id
                            value = self._response_payload(response, model, digest)
                            if ai_settings.get("cache", True):
                                cache.put(payload, value)
                            self._apply_hook_response(candidate, value, model, digest)
                            result.hook_ready += 1
                        except Exception as item_exc:
                            if cancellation.is_cancelled:
                                cancellation.raise_if_cancelled()
                            self._mark_hooks_unavailable(candidate, model, digest, str(item_exc) or type(item_exc).__name__)
                            result.hook_unavailable += 1
            for candidate in batch:
                self._ensure_branding(candidate, original, translated_title)
        result.elapsed_seconds = time.monotonic() - started
        return result

    def mark_stale(self, candidate: Candidate, transcript: Transcript | None = None) -> None:
        branding = dict(candidate.branding_settings or {})
        suggestions = dict(branding.get("title_suggestions") or {})
        if suggestions:
            suggestions["status"] = "stale"
            suggestions["stale_reason"] = "Границы изменены — варианты заголовка могут не соответствовать новой версии."
            suggestions["boundaries_hash"] = self._boundaries_hash(candidate)
            if transcript:
                suggestions["transcript_hash"] = self._candidate_transcript_hash(candidate, transcript)
            branding["title_suggestions"] = suggestions
            candidate.branding_settings = branding

    def _ensure_branding(self, candidate: Candidate, original, translated_title: str) -> None:
        branding = dict(candidate.branding_settings or {})
        branding.setdefault("original_video_title", original.title)
        branding.setdefault("original_video_title_source", original.source)
        if translated_title:
            branding.setdefault("translated_video_title", translated_title)
        candidate.branding_settings = branding

    def _apply_unavailable(self, candidate: Candidate, original, model: str, digest: str, error: str) -> None:
        branding = dict(candidate.branding_settings or {})
        branding["original_video_title"] = original.title
        branding["original_video_title_source"] = original.source
        branding.setdefault("translated_video_title", "")
        branding["title_suggestions"] = {
            "suggestions": [],
            "recommended_id": "",
            "selected_id": None,
            "manual_title": branding.get("manual_title"),
            "model": model,
            "model_digest": digest,
            "prompt_version": SHORT_HOOK_PROMPT_VERSION,
            "status": "unavailable",
            "error": error,
        }
        candidate.branding_settings = branding

    def _mark_hooks_unavailable(self, candidate: Candidate, model: str, digest: str, error: str) -> None:
        branding = dict(candidate.branding_settings or {})
        branding["title_suggestions"] = {
            "suggestions": [],
            "recommended_id": "",
            "selected_id": None,
            "manual_title": branding.get("manual_title"),
            "model": model,
            "model_digest": digest,
            "prompt_version": SHORT_HOOK_PROMPT_VERSION,
            "transcript_hash": branding.get("title_suggestions", {}).get("transcript_hash", ""),
            "boundaries_hash": self._boundaries_hash(candidate),
            "status": "unavailable",
            "error": error,
        }
        candidate.branding_settings = branding

    def _apply_hook_response(self, candidate: Candidate, value: Dict[str, Any], model: str, digest: str, *, cached: bool = False) -> None:
        branding = dict(candidate.branding_settings or {})
        previous = dict(branding.get("title_suggestions") or {})
        manual_title = previous.get("manual_title") or branding.get("manual_title")
        selected_id = previous.get("selected_id")
        suggestions = list(value.get("suggestions") or [])
        recommended_id = str(value.get("recommended_id") or (suggestions[0].get("id") if suggestions else "hook_1"))
        title_suggestions = {
            "suggestions": suggestions,
            "recommended_id": recommended_id,
            "selected_id": selected_id,
            "manual_title": manual_title,
            "model": str(value.get("model") or model),
            "model_digest": str(value.get("model_digest") or digest),
            "prompt_version": SHORT_HOOK_PROMPT_VERSION,
            "transcript_hash": str(value.get("transcript_hash") or ""),
            "boundaries_hash": str(value.get("boundaries_hash") or self._boundaries_hash(candidate)),
            "status": "ready",
            "cache_hit": bool(cached),
            "timestamp": str(value.get("timestamp") or utc_now()),
        }
        branding["title_suggestions"] = title_suggestions
        branding["short_hook_suggestions"] = suggestions
        branding.setdefault("short_hook_title", self._text_for_id(suggestions, selected_id or recommended_id))
        candidate.branding_settings = branding

    @staticmethod
    def _response_payload(response: HookSuggestionsResponse, model: str, digest: str) -> Dict[str, Any]:
        suggestions = []
        for index, item in enumerate(response.suggestions, start=1):
            data = item.model_dump()
            data["id"] = data.get("id") or f"hook_{index}"
            suggestions.append(data)
        recommended_id = response.recommended_id or suggestions[0]["id"]
        if recommended_id not in {item["id"] for item in suggestions}:
            recommended_id = suggestions[0]["id"]
        return {
            "candidate_id": response.candidate_id,
            "suggestions": suggestions,
            "recommended_id": recommended_id,
            "model": model,
            "model_digest": digest,
            "prompt_version": SHORT_HOOK_PROMPT_VERSION,
            "timestamp": utc_now(),
        }

    def _hook_cache_payload(self, candidate: Candidate, transcript: Transcript, model: str, digest: str, content_type: str, original_title: str) -> Dict[str, Any]:
        return {
            "kind": "short_hook",
            "candidate_id": candidate.id,
            "transcript_hash": self._candidate_transcript_hash(candidate, transcript),
            "boundaries_hash": self._boundaries_hash(candidate),
            "content_type": content_type,
            "moment_type": candidate.ai_moment_type,
            "model": model,
            "digest": digest,
            "prompt_version": SHORT_HOOK_PROMPT_VERSION,
            "original_video_title": original_title,
        }

    def _candidate_transcript_hash(self, candidate: Candidate, transcript: Transcript) -> str:
        context = self.title_service.hook_context(candidate, transcript)
        return hashlib.sha256(str(context.get("transcript", "")).encode("utf-8")).hexdigest()

    @staticmethod
    def _boundaries_hash(candidate: Candidate) -> str:
        return hashlib.sha256(f"{candidate.start:.3f}:{candidate.end:.3f}".encode("utf-8")).hexdigest()

    @staticmethod
    def _model_identity(backend, settings: Dict[str, Any]) -> tuple[str, str]:
        model = str(settings.get("_resolved_model") or settings.get("model") or getattr(backend, "model", "") or "")
        digest = str(settings.get("model_digest") or "")
        if hasattr(backend, "model"):
            model = str(getattr(backend, "model") or model)
        try:
            if hasattr(backend, "model_info"):
                info = backend.model_info()
                digest = str(info.get("digest") or digest)
                model = str(info.get("name") or info.get("model") or model)
        except Exception:
            pass
        return model, digest

    @staticmethod
    def _text_for_id(suggestions: Iterable[Dict[str, Any]], suggestion_id: str | None) -> str:
        for item in suggestions:
            if str(item.get("id") or "") == str(suggestion_id or ""):
                return str(item.get("text") or "")
        return ""

    @staticmethod
    def _context_before(candidate: Candidate, transcript: Transcript) -> str:
        return " ".join(segment.text.strip() for segment in transcript.segments if candidate.start - 20 <= segment.end <= candidate.start and segment.text.strip())

    @staticmethod
    def _context_after(candidate: Candidate, transcript: Transcript) -> str:
        return " ".join(segment.text.strip() for segment in transcript.segments if candidate.end <= segment.start <= candidate.end + 20 and segment.text.strip())
