import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Literal, Optional
from pydantic import model_validator
from ai_editor_copilot.domain.models import Model, Identifier, Text
from ai_editor_copilot.tools.registry import EditAction


class FeedbackEvent(Model):
    schema_version: Literal["1.0"] = "1.0"
    id: Identifier
    plan_id: Identifier
    action_id: Identifier
    participant_id: Identifier  # pseudonymous, no contact/identity data
    occurred_at: datetime
    decision: Literal["accepted", "modified", "rejected", "undone"]
    ai_proposal: EditAction
    user_modification: Optional[EditAction] = None
    final_edit_decision: Optional[EditAction] = None
    context_digest: Text
    timeline_revision_before: Identifier
    timeline_revision_after: Identifier
    reason: str = ""

    @model_validator(mode="after")
    def consistent(self):
        if self.occurred_at.tzinfo is None:
            raise ValueError("timezone-aware feedback timestamp required")
        if self.ai_proposal.id != self.action_id:
            raise ValueError("feedback action ID mismatch")
        if self.decision == "accepted" and self.final_edit_decision != self.ai_proposal:
            raise ValueError("accepted action must preserve proposal")
        if self.decision == "modified" and (self.user_modification is None or self.final_edit_decision != self.user_modification):
            raise ValueError("modified decision requires final correction")
        if self.decision in {"rejected", "undone"} and self.final_edit_decision is not None:
            raise ValueError("rejected/undone action must have no final decision")
        return self


def save_json(path: Path, value):
    """Atomic single-file replace; original media is never written by this module."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def save_feedback(directory: Path, event: FeedbackEvent):
    event = FeedbackEvent.model_validate_json(event.model_dump_json())
    path = directory / (event.id + ".json")
    if path.exists():
        raise FileExistsError("feedback is append-only by event ID")
    # Exclusive creation prevents two writers replacing the same event.
    directory.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        stream.write(event.model_dump_json(indent=2) + "\n")
    return path
