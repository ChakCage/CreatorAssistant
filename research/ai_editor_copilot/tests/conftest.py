from pathlib import Path
import pytest
from ai_editor_copilot.domain.models import PlannerInput, UserCommand
from ai_editor_copilot.ingest.transcript import load_transcript
from ai_editor_copilot.style.profiles import profile

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def context():
    return PlannerInput(timeline=load_transcript(ROOT / "examples/narrative_short/transcript.json"),
        style=profile(), command=UserCommand(text="Сделай 60-секундный Reel про необычный отель: hook, развитие и концовка."),
        target_duration=60, minimum_duration=50)
