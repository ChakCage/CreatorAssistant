from __future__ import annotations

from difflib import SequenceMatcher
from typing import Iterable

from creator_assistant.domain.author_presets import AuthorMatchKind, AuthorPreset, AuthorResolution
from creator_assistant.domain.models import VideoMetadata


def _norm(value: str) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def _handle(value: str) -> str:
    normalized = _norm(value)
    if not normalized:
        return ""
    tail = normalized.rstrip("/").rsplit("/", 1)[-1]
    return tail[1:] if tail.startswith("@") else tail


class AuthorPresetResolver:
    def resolve(self, metadata: VideoMetadata, presets: Iterable[AuthorPreset]) -> AuthorResolution:
        items = tuple(presets)
        channel_id = _norm(metadata.channel_id)
        if channel_id:
            matches = tuple(item for item in items if channel_id in {_norm(value) for value in item.youtube_channel_ids})
            result = self._exact_result(matches, "channel_id")
            if result:
                return result

        identifiers = {_handle(metadata.channel_handle), _handle(metadata.uploader_id)} - {""}
        if identifiers:
            matches = tuple(
                item for item in items
                if identifiers.intersection({
                    _handle(item.display_name),
                    *(_handle(value) for value in item.youtube_handles),
                } - {""})
            )
            result = self._exact_result(matches, "handle")
            if result:
                return result

        names = {_norm(metadata.channel), _norm(metadata.uploader)} - {""}
        if names:
            exact = tuple(item for item in items if _norm(item.display_name) in names)
            result = self._exact_result(exact, "display_name")
            if result:
                return result
            aliases = tuple(
                item for item in items
                if names.intersection({_norm(value) for value in item.aliases})
            )
            if len(aliases) == 1:
                return AuthorResolution(AuthorMatchKind.ALIAS_MATCH, aliases, "alias")
            if len(aliases) > 1:
                return AuthorResolution(AuthorMatchKind.MULTIPLE_MATCHES, aliases, "alias")

        suggestion = self._suggest(names, items)
        return AuthorResolution(AuthorMatchKind.NO_MATCH, suggestion=suggestion)

    @staticmethod
    def _exact_result(matches: tuple[AuthorPreset, ...], matched_by: str) -> AuthorResolution | None:
        if len(matches) == 1:
            return AuthorResolution(AuthorMatchKind.EXACT_MATCH, matches, matched_by)
        if len(matches) > 1:
            return AuthorResolution(AuthorMatchKind.MULTIPLE_MATCHES, matches, matched_by)
        return None

    @staticmethod
    def _suggest(names: set[str], presets: tuple[AuthorPreset, ...]) -> AuthorPreset | None:
        if not names:
            return None
        scored = []
        for preset in presets:
            candidates = {_norm(preset.display_name), *(_norm(value) for value in preset.aliases)} - {""}
            score = max((SequenceMatcher(None, left, right).ratio() for left in names for right in candidates), default=0.0)
            scored.append((score, preset))
        scored.sort(key=lambda item: item[0], reverse=True)
        return scored[0][1] if scored and scored[0][0] >= 0.72 else None
