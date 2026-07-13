from pathlib import Path

from creator_assistant.infrastructure.process_runner import ProcessRunner
from creator_assistant.services.ffmpeg_service import FfmpegService
from creator_assistant.services.reaper_service import ReaperService


def test_ffmpeg_nvenc_command_preserves_fps(tmp_path: Path):
    service = FfmpegService(ProcessRunner(), "ffmpeg.exe", nvenc_available=True)
    command = service.build_proxy_command(tmp_path / "вход.mkv", tmp_path / "выход.mp4")
    assert "h264_nvenc" in command
    assert command[command.index("-fps_mode") + 1] == "passthrough"
    assert "-r" not in command


def test_ffmpeg_cpu_fallback_command():
    service = FfmpegService(ProcessRunner(), "ffmpeg.exe", nvenc_available=False)
    command = service.build_proxy_command(Path("in.mkv"), Path("out.mp4"))
    assert "libx264" in command


def test_ffmpeg_proxy_caps_height_without_upscale():
    service = FfmpegService(ProcessRunner(), "ffmpeg.exe", nvenc_available=False)
    command = service.build_proxy_command(Path("in.mkv"), Path("out.mp4"), maximum_height=1080)
    assert command[command.index("-vf") + 1] == r"scale=-2:min(1080\,ih)"


def test_reaper_project_has_exactly_two_tracks_and_unicode_paths(tmp_path: Path):
    video = tmp_path / "Материалы" / "Ролик [720p].mp4"
    instrumental = tmp_path / "Материалы" / "Ролик [Instrumental].flac"
    video.parent.mkdir()
    video.write_bytes(b"video")
    instrumental.write_bytes(b"audio")
    output = tmp_path / "Ролик.rpp"
    service = ReaperService()
    service.generate_project(output, video, instrumental, 42.5)
    text = output.read_text(encoding="utf-8")
    assert text.count("<TRACK ") == 2
    assert "VIDEO 720P — ORIGINAL" in text
    assert "INSTRUMENTAL" in text
    assert service.validate_project(output, video, instrumental)


def test_reaper_initial_audio_modes(tmp_path: Path):
    video = tmp_path / "v.mp4"
    instrumental = tmp_path / "i.flac"
    output = tmp_path / "p.rpp"
    ReaperService().generate_project(output, video, instrumental, 1, initial_audio="both")
    assert output.read_text(encoding="utf-8").count("MUTESOLO 0 0 0") == 2


def test_reaper_track_uses_selected_proxy_height(tmp_path: Path):
    video = tmp_path / "v.mp4"
    instrumental = tmp_path / "i.flac"
    output = tmp_path / "p.rpp"
    ReaperService().generate_project(output, video, instrumental, 1, proxy_height=1080)
    assert "VIDEO 1080P" in output.read_text(encoding="utf-8")
