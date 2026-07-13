from pathlib import Path

from creator_assistant.domain.models import ProjectOptions, ThumbnailInfo, VideoFormat, VideoMetadata
from creator_assistant.infrastructure.job_store import JobStore
from creator_assistant.infrastructure.windows_paths import NamingTemplates
from creator_assistant.services.project_service import ProjectService


class Unused:
    pass


def test_dry_run_does_not_create_project(tmp_path: Path):
    service = ProjectService(Unused(), Unused(), Unused(), Unused(), Unused(), Unused(), Unused(), JobStore(tmp_path / "state"), NamingTemplates())
    metadata = VideoMetadata(
        "id",
        "Новый проект",
        10,
        "https://youtu.be/abcdefghijk",
        formats=[
            VideoFormat("v4k", "webm", height=2160, fps=60, vcodec="vp9"),
            VideoFormat("v720", "mp4", height=720, fps=60, vcodec="avc1"),
            VideoFormat("a", "m4a", acodec="mp4a.40.2"),
        ],
        thumbnails=[ThumbnailInfo("https://example.com/a.jpg")],
    )
    result = service.dry_run(tmp_path, metadata, ProjectOptions(dry_run=True))
    assert result.project_path is None
    assert not (tmp_path / "Новый проект").exists()
    assert any("HDR" in line for line in result.plan_lines)
