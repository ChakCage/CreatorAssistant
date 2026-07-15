from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

from creator_assistant.services.shorts.semantic_backend import PROMPT_VERSION
from creator_assistant.services.shorts.semantic_models import SemanticCandidateInput


def semantic_cache_payload(
    candidates: list[SemanticCandidateInput],
    *,
    model: str,
    content_type: str,
    digest: str = "",
    quantization: str = "",
    analysis_mode: str = "balanced",
    transcript_hash: str = "",
    prompt_version: str = PROMPT_VERSION,
) -> Dict[str, Any]:
    """Return the stable semantic input identity, deliberately excluding render UI state."""
    return {
        "prompt_version": prompt_version,
        "model": model,
        "digest": digest,
        "quantization": quantization,
        "analysis_mode": analysis_mode,
        "transcript_hash": transcript_hash,
        "content_type": content_type,
        "candidates": [item.model_dump(mode="json") for item in candidates],
    }


class SemanticCache:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.data = self._load()

    @staticmethod
    def key(payload: Dict[str, Any]) -> str:
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def get(self, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        value = self.data.get(self.key(payload))
        return dict(value) if isinstance(value, dict) else None

    def put(self, payload: Dict[str, Any], value: Dict[str, Any]) -> None:
        self.data[self.key(payload)] = value
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(str(temporary), str(self.path))

    def clear(self) -> None:
        self.data = {}
        if self.path.exists():
            self.path.unlink()

    def _load(self) -> Dict[str, Any]:
        if not self.path.is_file():
            return {}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}
