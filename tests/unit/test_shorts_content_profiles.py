from creator_assistant.domain.shorts.models import AudioFeatures, Candidate
from creator_assistant.services.shorts.candidate_scorer import HeuristicCandidateScorer


def test_content_type_changes_heuristic_weights_and_reasons():
    text = "Как решить проблему: объясняю способ и показываю полезный результат."
    scorer = HeuristicCandidateScorer()
    gaming = scorer.score(Candidate("g", 0, 45, 0, text), [], AudioFeatures(), "gaming")
    education = scorer.score(Candidate("e", 0, 45, 0, text), [], AudioFeatures(), "education")
    assert gaming.score != education.score
    assert any("обучающего" in reason for reason in education.reasons)
