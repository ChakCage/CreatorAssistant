import json

from creator_assistant.domain.shorts.models import Candidate, SourceInfo, Transcript, TranscriptSegment
from creator_assistant.services.shorts.shorts_project_store import ShortsProjectStore
from creator_assistant.services.shorts.title_service import ShortTitleService


def source(path):
    return SourceInfo(str(path), path.name, 10, 1, 60, 1920, 1080, 30, "h264", "aac", 2, 48000, fingerprint="fp")


def test_folder_title_wins_over_technical_video_filename(tmp_path):
    project = tmp_path / "I Mined 48,235 Obsidian - Hardcore"
    source_file = project / "Видос.mp4"
    source_file.parent.mkdir()
    source_file.write_bytes(b"v")
    paths = ShortsProjectStore().create(project / "Shorts", source(source_file))
    resolved = ShortTitleService().resolve_original_title(source(source_file), paths)
    assert resolved.title == "I Mined 48,235 Obsidian - Hardcore"
    assert resolved.source == "название папки проекта"


def test_metadata_title_has_priority_over_folder(tmp_path):
    project = tmp_path / "Folder Title"
    metadata_dir = project / ".creator-assistant"
    metadata_dir.mkdir(parents=True)
    (metadata_dir / "manifest.json").write_text(
        json.dumps({"schema_version": 2, "title": "YouTube Metadata Title"}, ensure_ascii=False),
        encoding="utf-8",
    )
    source_file = project / "Видос.mp4"
    source_file.write_bytes(b"v")
    paths = ShortsProjectStore().create(project / "Shorts", source(source_file))
    resolved = ShortTitleService().resolve_original_title(source(source_file), paths)
    assert resolved.title == "YouTube Metadata Title"
    assert resolved.source == "YouTube metadata"


def test_hook_rejects_technical_title_and_uses_candidate_transcript():
    service = ShortTitleService()
    assert not service.validate_hook("Видос")
    transcript = Transcript("ru", 20, "", [TranscriptSegment(0, 0, 10, "я добыл сорок восемь тысяч обсидиана")])
    hook = service.heuristic_hook(Candidate("short_001", 0, 10, 90, ""), transcript, "Видос")
    assert hook == "Я ДОБЫЛ СОРОК ВОСЕМЬ ТЫСЯЧ ОБСИДИАНА"
