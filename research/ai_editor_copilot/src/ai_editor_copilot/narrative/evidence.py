"""Evidence contracts. Model summaries never replace immutable transcript text."""
import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import List, Literal, Optional, Union

from pydantic import Field, model_validator
from ai_editor_copilot.domain.models import Model, Identifier, Text, Probability, Positive, TimeRange
from ai_editor_copilot.planner.service import stable_id


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class Word(Model):
    start: float = Field(ge=0)
    end: float = Field(ge=0)
    word: str
    probability: Optional[Probability] = None

    @model_validator(mode="after")
    def ordered(self):
        if self.end < self.start:
            raise ValueError("word end before start")
        return self


class RawPoint(Model):
    """ASR zero-duration evidence is preserved, but cannot be an executable clip alone."""
    start: float = Field(ge=0)
    end: float = Field(ge=0)

    @model_validator(mode="after")
    def point(self):
        if self.end != self.start:
            raise ValueError("raw point requires equal timestamps")
        return self


class RawSegment(Model):
    id: Identifier
    range: Union[TimeRange, RawPoint]
    text: str
    words: List[Word] = Field(default_factory=list)


class EvidenceSource(Model):
    id: Identifier
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    transcript_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    duration: Positive
    language: Text
    backend: Text
    model: Text
    ingested_at: datetime
    segments: List[RawSegment] = Field(min_length=1)

    @model_validator(mode="after")
    def valid(self):
        if self.ingested_at.tzinfo is None:
            raise ValueError("ingest timestamp must have timezone")
        if len({s.id for s in self.segments}) != len(self.segments):
            raise ValueError("duplicate raw segment IDs")
        last_start = -1
        for s in self.segments:
            if s.range.end > self.duration or s.range.start < last_start:
                raise ValueError("raw timestamp outside source or out of order")
            last_start = s.range.start
            if any(w.end > self.duration for w in s.words):
                raise ValueError("word timestamp outside source")
        return self


class EvidenceRef(Model):
    source_segment_ids: List[Identifier] = Field(min_length=1)
    evidence_start: float = Field(ge=0)
    evidence_end: Positive


class Annotation(Model):
    type: Text
    value: Optional[str]
    status: Literal["OBSERVED", "INFERRED", "UNKNOWN"]
    confidence: Probability
    evidence: EvidenceRef
    generator: Text
    model: Text
    created_at: datetime

    @model_validator(mode="after")
    def consistent(self):
        if self.created_at.tzinfo is None:
            raise ValueError("annotation timestamp must have timezone")
        if self.status == "UNKNOWN" and (self.value is not None or self.confidence != 0):
            raise ValueError("UNKNOWN is not a factual value")
        if self.status != "UNKNOWN" and not self.value:
            raise ValueError("known annotation needs value")
        return self


def resolve(source: EvidenceSource, ref: EvidenceRef):
    """Only complete contiguous raw segments; no fabricated word-level precision."""
    ids = [s.id for s in source.segments]
    if len(set(ref.source_segment_ids)) != len(ref.source_segment_ids):
        raise ValueError("duplicate evidence references")
    try:
        indexes = [ids.index(i) for i in ref.source_segment_ids]
    except ValueError:
        raise ValueError("unknown evidence segment")
    if indexes != list(range(indexes[0], indexes[-1] + 1)):
        raise ValueError("evidence must be ordered contiguous raw segments")
    selected = source.segments[indexes[0]:indexes[-1] + 1]
    if ref.evidence_start != selected[0].range.start or ref.evidence_end != selected[-1].range.end:
        raise ValueError("evidence timestamps must match exact raw boundaries")
    return selected


def reference(segments):
    return EvidenceRef(source_segment_ids=[s.id for s in segments],
        evidence_start=segments[0].range.start, evidence_end=segments[-1].range.end)


def validate_annotation(source, annotation):
    selected = resolve(source, annotation.evidence)
    if annotation.status == "OBSERVED":
        # This contract supports only literal observed transcript quotes, not inferred facts.
        if annotation.type != "transcript_quote" or annotation.value not in " ".join(s.text for s in selected):
            raise ValueError("OBSERVED requires verbatim transcript evidence")
    return annotation


def load_evidence(directory):
    directory = Path(directory)
    manifest = json.loads((directory / "input_manifest.json").read_text(encoding="utf-8"))
    path = directory / manifest["raw_transcript"]
    if path.resolve().parent != directory.resolve():
        raise ValueError("raw evidence path escapes dataset directory")
    if file_sha256(path) != manifest["transcript_sha256"]:
        raise ValueError("immutable transcript hash mismatch")
    raw = json.loads(path.read_text(encoding="utf-8"))
    segments = []
    for s in raw["segments"]:
        # Missing or malformed timestamps are errors, never default to zero.
        range_type = RawPoint if s["start"] == s["end"] else TimeRange
        segments.append(RawSegment(id="s_" + str(s["id"]),
            range=range_type(start=s["start"], end=s["end"]), text=s["text"],
            words=[Word.model_validate(w) for w in s.get("words", [])]))
    return EvidenceSource(id=stable_id("source_", manifest["source_sha256"]),
        sha256=manifest["source_sha256"], transcript_sha256=manifest["transcript_sha256"],
        duration=manifest["duration"], language=manifest["language"], backend=manifest["backend"],
        model=manifest["model"], ingested_at=datetime.fromisoformat(manifest["ingested_at"]), segments=segments)
