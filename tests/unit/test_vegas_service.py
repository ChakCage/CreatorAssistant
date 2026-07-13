from pathlib import Path

import pytest

from creator_assistant.domain.errors import VegasProjectError, VegasProjectValidationError
from creator_assistant.services.vegas_service import VegasService
from creator_assistant.domain.job import CancellationToken


def test_existing_nonempty_veg_is_not_overwritten(tmp_path: Path):
    output = tmp_path / "project.veg"
    output.write_bytes(b"manual veg")
    service = VegasService()

    assert service.validate_project(output)
    assert service.project_path(tmp_path, "A:B?C").name == "A-B-C.veg"


def test_result_validation_rejects_wrong_timeline_counts(tmp_path: Path):
    output = tmp_path / "project.veg"
    output.write_bytes(b"veg")
    result = tmp_path / "result.json"
    result.write_text(
        '{"success":true,"output":"' + str(output).replace("\\", "\\\\") + '","tracks":2,"video_events":1,"audio_events":2,"max_audio_events":1}',
        encoding="utf-8",
    )

    with pytest.raises(VegasProjectValidationError):
        VegasService()._read_result(result, output)


def test_unicode_job_uses_exact_max_and_instrumental_paths(monkeypatch, tmp_path: Path):
    vegas = tmp_path / "VEGAS Pro" / "vegas220.exe"
    vegas.parent.mkdir()
    vegas.write_bytes(b"exe")
    vegas.with_name("ScriptPortal.Vegas.dll").write_bytes(b"api")
    video = tmp_path / "Материалы" / "MAX's видео.mkv"
    instrumental = tmp_path / "Материалы" / "Инструментал.flac"
    video.parent.mkdir()
    video.write_bytes(b"video")
    instrumental.write_bytes(b"audio")
    output = tmp_path / "Проект's монтаж.veg"
    captured = {}
    commands = []

    class Process:
        pid = 999

        def poll(self):
            return 0

        def terminate(self):
            pass

    def launch(_command, **kwargs):
        commands.append(_command)
        job_path = Path(kwargs["env"]["CREATOR_ASSISTANT_VEGAS_JOB"])
        job = __import__("json").loads(job_path.read_text(encoding="utf-8"))
        captured.update(job)
        Path(job["output_project_path"]).write_bytes(b"real veg")
        Path(job["result_path"]).write_text(__import__("json").dumps({
            "success": True,
            "project_path": job["output_project_path"],
            "video_track_count": 1,
            "audio_track_count": 1,
            "video_event_count": 1,
            "audio_event_count": 1,
            "video_media_path": job["video_path"],
            "audio_media_path": job["instrumental_path"],
            "original_video_audio_added": False,
            "max_audio_events": 0,
            "video_start_nanos": 0,
            "audio_start_nanos": 0,
            "width": 2560,
            "height": 1440,
            "fps": 60.0,
        }, ensure_ascii=False), encoding="utf-8")
        return Process()

    monkeypatch.setattr("creator_assistant.services.vegas_service.subprocess.Popen", launch)
    service = VegasService(str(vegas), script_dir=tmp_path / "managed script")
    monkeypatch.setattr(service, "_running_vegas_processes", lambda: set())
    result = service.create_project(
        output=output,
        max_video=video,
        instrumental=instrumental,
        duration=12.0,
        temp_dir=tmp_path / "job",
        cancellation=CancellationToken(),
        job_id="unicode-job",
    )

    assert captured["schema_version"] == 1
    assert Path(captured["video_path"]) == video.resolve()
    assert Path(captured["instrumental_path"]) == instrumental.resolve()
    assert result.video_media_path == str(video.resolve())
    assert result.audio_media_path == str(instrumental.resolve())
    assert result.tracks == 2
    saved_job = __import__("json").loads((tmp_path / "job" / "vegas_project_job.json").read_text(encoding="utf-8"))
    assert saved_job["dedicated_pid"] == 999
    assert saved_job["use_active_dedicated_project"] is True
    assert commands[0][-1].startswith("-SCRIPT:")


def test_running_user_vegas_is_never_reused_or_stopped(monkeypatch, tmp_path: Path):
    vegas = tmp_path / "vegas220.exe"
    vegas.write_bytes(b"exe")
    vegas.with_name("ScriptPortal.Vegas.dll").write_bytes(b"api")
    video = tmp_path / "max.mp4"
    audio = tmp_path / "instrumental.flac"
    video.write_bytes(b"video")
    audio.write_bytes(b"audio")
    launched = []
    service = VegasService(str(vegas), script_dir=tmp_path / "script")
    monkeypatch.setattr(service, "_running_vegas_processes", lambda: {42})
    monkeypatch.setattr("creator_assistant.services.vegas_service.subprocess.Popen", lambda *args, **kwargs: launched.append(args))

    with pytest.raises(VegasProjectError, match="уже запущен"):
        service.create_project(
            output=tmp_path / "project.veg",
            max_video=video,
            instrumental=audio,
            duration=1.0,
            temp_dir=tmp_path / "job",
            cancellation=CancellationToken(),
        )

    assert launched == []
