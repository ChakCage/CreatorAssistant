import json

from creator_assistant.domain.automation.models import AutomationShort, RenderArtifact
from creator_assistant.infrastructure.process_runner import ProcessResult
from creator_assistant.services.automation.quality_control import AutomationQualityControl


def short(tmp_path):
    item = AutomationShort(
        "one", "source", "candidate", 1, 0, 40, 90,
        title="Заголовок", candidate_data={"text": "Полная фраза"},
        subtitle_settings={"cues": [{"start": 0, "end": 1, "text": "Текст"}]},
    )
    path = tmp_path / "render.mp4"
    path.write_bytes(b"0" * 150_000)
    item.artifact = RenderArtifact("candidate", 1, str(path))
    return item


def test_pre_render_critical_validation():
    item = AutomationShort("one", "source", "candidate", 1, 0, 80, 90, title="Видос")
    issues = AutomationQualityControl().pre_render(item, transcript_exists=False, profile_required=True)
    assert {issue.code for issue in issues} >= {"missing_transcript", "missing_subtitles", "invalid_title", "missing_profile", "invalid_duration"}
    assert all(issue.critical for issue in issues)


def test_ffprobe_validation_accepts_expected_mp4_and_three_frames(tmp_path):
    item = short(tmp_path)

    class Runner:
        def run(self, command):
            if command[0] == "ffprobe.exe":
                payload = {"format": {"duration": "40.0"}, "streams": [
                    {"codec_type": "video", "codec_name": "h264", "width": 1080, "height": 1920, "avg_frame_rate": "60000/1001"},
                    {"codec_type": "audio", "codec_name": "aac"},
                ]}
                return ProcessResult(command, 0, json.dumps(payload), stdout=json.dumps(payload))
            return ProcessResult(command, 0, "lavfi.signalstats.YAVG=42", stdout="lavfi.signalstats.YAVG=42")

    qc = AutomationQualityControl()
    artifact, issues = qc.probe_render(Runner(), "ffprobe.exe", item, 59.94)
    item.artifact = artifact
    assert issues == [] and artifact.validated
    assert qc.inspect_frames(Runner(), "ffmpeg.exe", item) == []


def test_three_black_frames_block_autopublish(tmp_path):
    item = short(tmp_path)

    class Runner:
        @staticmethod
        def run(command):
            return ProcessResult(command, 0, "lavfi.signalstats.YAVG=0", stdout="lavfi.signalstats.YAVG=0")

    issues = AutomationQualityControl().inspect_frames(Runner(), "ffmpeg.exe", item)
    assert issues[0].code == "black_render" and issues[0].critical
