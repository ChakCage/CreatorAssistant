"""Timestamped JSON ingest; media/transcription are supplied by the caller."""
import json
from pathlib import Path
from typing import List, Optional
from pydantic import Field
from ai_editor_copilot.domain.models import Model, Identifier, Text, Positive, SemanticTimeline, SemanticSegment, SourceAsset


class TranscriptRow(Model):
    id: Identifier
    start: float
    end: float
    text: str
    speaker: Optional[str] = None


class TranscriptDocument(Model):
    source_id: Identifier
    source_uri: Text
    duration: Positive
    segments: List[TranscriptRow] = Field(min_length=1)


def load_transcript(path: Path):
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if "sources" in data:
        return SemanticTimeline.model_validate(data)
    return from_document(TranscriptDocument.model_validate(data))


def from_document(doc: TranscriptDocument):
    return SemanticTimeline(id=doc.source_id + "_timeline",
        sources=[SourceAsset(id=doc.source_id, kind="video", uri=doc.source_uri, duration=doc.duration)],
        segments=[SemanticSegment(id=s.id, source_asset_id=doc.source_id,
            range={"start": s.start, "end": s.end}, transcript=s.text, speaker=s.speaker)
            for s in doc.segments])


def from_creator_transcript(transcript, *, source_id, source_uri, duration):
    """Duck-typed adapter accepts existing CA Transcript without UI/import coupling."""
    return from_document(TranscriptDocument(source_id=source_id, source_uri=source_uri, duration=duration,
        segments=[TranscriptRow(id=f"seg_{i:05d}", start=s.start, end=s.end, text=s.text)
                  for i, s in enumerate(transcript.segments)]))
