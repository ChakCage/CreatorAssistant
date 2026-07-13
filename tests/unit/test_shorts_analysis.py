from creator_assistant.domain.job import CancellationToken
from creator_assistant.domain.shorts.models import AudioFeatures, Candidate, Scene, Transcript, TranscriptSegment
from creator_assistant.infrastructure.process_runner import ProcessResult
from creator_assistant.services.shorts.audio_activity_service import AudioActivityService
from creator_assistant.services.shorts.candidate_generator import CandidateGenerator, CandidateSettings
from creator_assistant.services.shorts.candidate_scorer import HeuristicCandidateScorer
from creator_assistant.services.shorts.duplicate_filter import DuplicateFilter, overlap_ratio
from creator_assistant.services.shorts.scene_detection_service import SceneDetectionService


class OutputRunner:
    def __init__(self, lines):
        self.lines, self.command = lines, None

    def run(self, command, on_line=None, **kwargs):
        self.command = list(command)
        if on_line:
            for line in self.lines:
                on_line(line)
        return ProcessResult(self.command, 0, "\n".join(self.lines))


def test_scene_detection_parses_pts_and_preserves_full_duration(tmp_path):
    runner = OutputRunner(["[Parsed_showinfo] n:1 pts_time:12.5", "[Parsed_showinfo] pts_time:40.25"])
    target = tmp_path / "scenes.json"
    scenes = SceneDetectionService(runner, "ffmpeg.exe").detect(tmp_path / "прокси.mp4", 60.0, target, CancellationToken())
    assert [(item.start, item.end) for item in scenes] == [(0.0, 12.5), (12.5, 40.25), (40.25, 60.0)]
    assert "showinfo" in runner.command[runner.command.index("-vf") + 1]
    assert SceneDetectionService.load(target) == scenes


def test_audio_activity_builds_speech_as_pause_complement(tmp_path):
    runner = OutputRunner([
        "[silencedetect] silence_start: 3.0", "[silencedetect] silence_end: 5.5 | silence_duration: 2.5",
        "[silencedetect] silence_start: 10.0", "[silencedetect] silence_end: 12.0", "mean_volume: -18.4 dB",
    ])
    target = tmp_path / "audio_features.json"
    result = AudioActivityService(runner, "ffmpeg.exe").analyse(tmp_path / "audio.wav", 15.0, target, CancellationToken())
    assert result.pauses == [[3.0, 5.5], [10.0, 12.0]]
    assert result.speech_intervals == [[0.0, 3.0], [5.5, 10.0], [12.0, 15.0]]
    assert result.mean_loudness == -18.4


def transcript_100_seconds():
    segments = [TranscriptSegment(index, index * 10.0, (index + 1) * 10.0, f"Фраза номер {index}. Это законченная мысль!") for index in range(10)]
    return Transcript("ru", 100.0, " ".join(item.text for item in segments), segments)


def test_candidate_windows_respect_defaults_and_align_boundaries():
    scenes = [Scene(index * 10.0, (index + 1) * 10.0) for index in range(10)]
    audio = AudioFeatures(pauses=[[39.8, 40.2]])
    result = CandidateGenerator().generate(transcript_100_seconds(), scenes, audio, CandidateSettings())
    assert result
    assert all(25 <= item.duration <= 60 for item in result)
    assert result[0].duration in {40.0, 40.2, 50.0}


def test_weighted_scoring_explains_hook_payoff_and_penalizes_pause():
    candidate = Candidate("one", 0, 45, 0, "Как победить? Смотрите, это невероятно! В результате всё получилось!")
    scenes = [Scene(0, 8), Scene(8, 16), Scene(16, 25), Scene(25, 35), Scene(35, 45)]
    result = HeuristicCandidateScorer().score(candidate, scenes, AudioFeatures(pauses=[[20, 23]]))
    assert result.score >= 70
    assert "Сильное начало" in result.reasons
    assert "Есть развязка" in result.reasons
    assert any("пауз" in warning for warning in result.warnings)


def test_duplicate_filter_keeps_best_and_records_alternative_boundaries():
    best = Candidate("a", 10, 55, 90, "Одинаковая история про Minecraft и редстоун")
    duplicate = Candidate("b", 12, 57, 80, "Одинаковая история про Minecraft и редстоун")
    distinct = Candidate("c", 70, 110, 70, "Совсем другая законченная история")
    result = DuplicateFilter().filter([duplicate, distinct, best], limit=15)
    assert result == [best, distinct]
    assert best.alternatives == [[12, 57]]
    assert overlap_ratio(best, duplicate) > 0.9
