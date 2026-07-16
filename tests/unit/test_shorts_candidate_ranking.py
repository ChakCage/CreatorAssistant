from dataclasses import asdict

from creator_assistant.domain.shorts.models import Candidate, RenderArtifact, RenderJob
from creator_assistant.services.shorts.candidate_ranking import assign_candidate_ranks


def test_assign_candidate_ranks_uses_final_score_and_is_deterministic():
    candidates = [
        Candidate("later", 20, 30, 80, "", final_score=90),
        Candidate("best", 10, 20, 90, "", final_score=95),
        Candidate("earlier", 5, 15, 80, "", final_score=90),
    ]
    assert assign_candidate_ranks(candidates) is True
    assert {item.id: item.candidate_rank for item in candidates} == {
        "best": 1, "earlier": 2, "later": 3,
    }
    assert assign_candidate_ranks(candidates) is False


def test_render_artifact_and_job_preserve_rank_at_render_time():
    artifact = RenderArtifact("short_007", "render.mp4", candidate_rank=3)
    job = RenderJob("render_short_007", "short_007", "render.mp4", candidate_rank=3, artifact=asdict(artifact))
    restored = RenderJob(**asdict(job))
    assert restored.candidate_rank == 3
    assert restored.artifact == {
        "candidate_id": "short_007", "output_path": "render.mp4", "candidate_rank": 3,
    }


def test_legacy_candidate_and_render_job_load_without_rank():
    candidate = Candidate(**{"id": "legacy", "start": 0, "end": 10, "score": 50, "text": ""})
    job = RenderJob(**{"id": "render_legacy", "candidate_id": "legacy", "output_path": "legacy.mp4"})
    assert candidate.candidate_rank is None
    assert job.candidate_rank is None
    assert job.artifact is None
