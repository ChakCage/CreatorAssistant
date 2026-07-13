from creator_assistant.domain.progress import WeightedProgressTracker
from creator_assistant.domain.stages import JobStage
from creator_assistant.services.ffmpeg_service import FfmpegService
from creator_assistant.services.yt_dlp_service import YtDlpService


def test_yt_dlp_progress_uses_total_bytes_and_numeric_metrics():
    info = YtDlpService.parse_progress_line(
        "CREATOR_PROGRESS|68.4%|2100000000|3100000000|NA|18400000|55|NA|NA|av01|none",
        JobStage.DOWNLOAD_MAXIMUM.value,
        multi_stream=True,
    )
    assert info is not None
    assert round(info.percent, 2) == 47.88
    assert info.total_bytes == 3_100_000_000
    assert info.speed_bytes_per_second == 18_400_000
    assert info.eta_seconds == 55
    assert "видеопотока" in info.substage_name


def test_yt_dlp_progress_falls_back_to_estimated_size():
    info = YtDlpService.parse_progress_line(
        "CREATOR_PROGRESS|10%|100|NA|1000|NA|NA|1|10|none|opus",
        "Аудио",
    )
    assert info is not None
    assert info.total_bytes == 1000


def test_yt_dlp_progress_keeps_unknown_size_honest():
    info = YtDlpService.parse_progress_line(
        "CREATOR_PROGRESS|NA|100|NA|NA|NA|NA|NA|NA|none|opus",
        "Аудио",
    )
    assert info is not None
    assert info.percent is None
    assert info.total_bytes is None
    assert info.total == "Размер пока неизвестен"


def test_ffmpeg_progress_is_calculated_from_out_time():
    assert FfmpegService.progress_percent(30_000_000, 120.0) == 25.0
    assert FfmpegService.progress_percent(150_000_000, 120.0) == 100.0


def test_weighted_overall_progress_is_monotonic_and_finishes_at_100():
    tracker = WeightedProgressTracker([JobStage.CHECK_DEPENDENCIES, JobStage.DOWNLOAD_MAXIMUM, JobStage.FINAL_VALIDATION])
    first = tracker.update(JobStage.DOWNLOAD_MAXIMUM, 70)
    second = tracker.update(JobStage.DOWNLOAD_MAXIMUM, 20)
    assert second >= first
    tracker.mark_complete(JobStage.CHECK_DEPENDENCIES)
    tracker.mark_complete(JobStage.DOWNLOAD_MAXIMUM)
    tracker.mark_complete(JobStage.FINAL_VALIDATION)
    assert tracker.last_overall == 100.0


def test_disabled_stage_is_excluded_from_weight_normalization():
    tracker = WeightedProgressTracker([JobStage.CHECK_DEPENDENCIES, JobStage.FINAL_VALIDATION])
    assert JobStage.SEPARATE_STEMS not in tracker.active
    tracker.mark_complete(JobStage.CHECK_DEPENDENCIES)
    assert 0 < tracker.last_overall < 100
