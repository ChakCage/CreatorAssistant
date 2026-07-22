import json
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

from creator_assistant.domain.automation.models import AutomationJob, AutomationJobSource
from creator_assistant.domain.shorts.models import AudioFeatures, Candidate, Scene, SourceInfo, Transcript, TranscriptSegment
from creator_assistant.services.automation.shorts_pipeline import ExistingShortsAutomationPipeline
from creator_assistant.services.shorts.channel_assets import ChannelAssetStore
from creator_assistant.services.shorts.shorts_project_store import ShortsProjectStore
from creator_assistant.services.shorts.transcription_service import TranscriptionService


class _MustNotRun:
    def __getattr__(self, name):
        def fail(*_args, **_kwargs):
            raise AssertionError(f"cached stage unexpectedly executed: {name}")
        return fail


def test_new_source_analysis_reuses_heavy_local_caches(monkeypatch, tmp_path: Path):
    media = tmp_path / "video.mp4"
    media.write_bytes(b"source")
    source_info = SourceInfo(str(media), media.name, media.stat().st_size, media.stat().st_mtime, 120, 1920, 1080, 60, "h264", "aac", 2, 48000, fingerprint="same")
    paths = ShortsProjectStore().create(tmp_path / "Shorts", source_info)
    (paths.analysis / "analysis_proxy.mp4").write_bytes(b"proxy")
    (paths.analysis / "transcription_audio.flac").write_bytes(b"audio")
    transcript = Transcript("ru", 120, "Сильное событие завершилось.", [TranscriptSegment(1, 0, 60, "Сильное событие завершилось.")], "whisper", "large")
    TranscriptionService.write_files(transcript, paths.analysis)
    (paths.analysis / "scenes.json").write_text(json.dumps([asdict(Scene(0, 120))]), encoding="utf-8")
    (paths.analysis / "audio_features.json").write_text(json.dumps(asdict(AudioFeatures([[0, 120]], [], [], -12))), encoding="utf-8")

    monkeypatch.setattr(
        "creator_assistant.services.automation.shorts_pipeline.ShortsSourceService",
        lambda *_args, **_kwargs: SimpleNamespace(probe=lambda _path: source_info),
    )
    monkeypatch.setattr(
        "creator_assistant.services.automation.shorts_pipeline.ShortTitleAssetService",
        lambda: SimpleNamespace(prepare=lambda **_kwargs: None),
    )
    candidate = Candidate("short_001", 0, 60, 80, "Сильное событие завершилось.")
    container = SimpleNamespace(
        runner=object(), paths={"ffprobe": "ffprobe"},
        shorts_proxy=_MustNotRun(), shorts_audio=_MustNotRun(), shorts_transcription=_MustNotRun(),
        shorts_scenes=SimpleNamespace(load=lambda _path: [Scene(0, 120)]),
        shorts_audio_activity=SimpleNamespace(load=lambda _path: AudioFeatures([[0, 120]], [], [], -12)),
        shorts_candidate_generator=SimpleNamespace(generate=lambda *_args: [candidate]),
        shorts_candidate_scorer=SimpleNamespace(score=lambda item, *_args: item),
        shorts_hybrid_analyzer=SimpleNamespace(analyse=lambda *_args, **_kwargs: SimpleNamespace(candidates=[candidate])),
        shorts_semantic_backend=object(), settings={"shorts_ai": {"model": "qwen3.6:35b-a3b", "fallback": False}},
        channel_assets=ChannelAssetStore(tmp_path / "profiles"),
    )
    pipeline = ExistingShortsAutomationPipeline(container)
    source_job = AutomationJobSource(str(media), source_id="source-1", shorts_project_path=str(paths.root))
    job = AutomationJob("job", [source_job])

    pipeline._analyze_new_source(job, source_job)

    assert set(job.resume_data["cache_reused"]["source-1"]) == {
        "analysis_proxy", "transcription_audio", "transcript", "scenes", "audio_features",
    }
    assert (paths.analysis / "candidates.json").is_file()

