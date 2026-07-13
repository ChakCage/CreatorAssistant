import json

import pytest

from creator_assistant.domain.shorts.errors import InvalidClipError
from creator_assistant.domain.shorts.models import Candidate, SourceInfo
from creator_assistant.services.shorts.review_service import CandidateReviewService
from creator_assistant.services.shorts.shorts_project_store import ShortsProjectStore


def make_review(tmp_path):
    source = SourceInfo(str(tmp_path / "ролик.mp4"), "ролик.mp4", 100, 1.0, 120.0, 1920, 1080, 30.0, "h264", "aac", 2, 48000, fingerprint="fp")
    paths = ShortsProjectStore().create(tmp_path / "Shorts", source)
    return CandidateReviewService(paths, source.duration), paths


def test_approval_and_boundaries_are_persisted_atomically(tmp_path):
    review, paths = make_review(tmp_path)
    candidate = Candidate("short_001", 10, 55, 88.5, "Minecraft и редстоун", status="approved")
    review.update_boundaries(candidate, 9.9, 61.25)
    review.save([candidate])
    approved = json.loads((paths.approved / "short_001.json").read_text(encoding="utf-8"))
    manifest = json.loads(paths.manifest.read_text(encoding="utf-8"))
    assert approved["text"] == "Minecraft и редстоун"
    assert approved["start"] == 9.9 and approved["end"] == 61.25
    assert manifest["approved_clips"][0]["id"] == "short_001"
    assert not paths.manifest.with_suffix(".json.tmp").exists()


def test_rejection_removes_only_approved_metadata_not_candidate(tmp_path):
    review, paths = make_review(tmp_path)
    candidate = Candidate("short_001", 10, 55, 80, "Текст", status="approved")
    review.save([candidate])
    candidate.status = "rejected"
    review.save([candidate])
    assert not (paths.approved / "short_001.json").exists()
    saved = json.loads((paths.analysis / "candidates.json").read_text(encoding="utf-8"))
    assert saved[0]["status"] == "rejected"


@pytest.mark.parametrize("start,end", [(-0.1, 10), (20, 20), (30, 20), (0, 120.1)])
def test_invalid_boundaries_are_rejected(tmp_path, start, end):
    review, _paths = make_review(tmp_path)
    with pytest.raises(InvalidClipError):
        review.update_boundaries(Candidate("id", 0, 30, 1, ""), start, end)


def test_over_sixty_seconds_is_allowed_for_manual_choice(tmp_path):
    review, _paths = make_review(tmp_path)
    candidate = Candidate("id", 0, 30, 1, "")
    assert review.update_boundaries(candidate, 1, 70).duration == 69
