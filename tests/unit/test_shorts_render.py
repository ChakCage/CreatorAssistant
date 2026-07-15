import json
from pathlib import Path

import pytest

from creator_assistant.domain.errors import ProcessExecutionError
from creator_assistant.domain.job import CancellationToken
from creator_assistant.domain.shorts.errors import InvalidClipError
from creator_assistant.domain.shorts.models import Candidate, SourceInfo
from creator_assistant.infrastructure.process_runner import ProcessResult
from creator_assistant.services.shorts.filter_graph_builder import ShortsFilterGraphBuilder
from creator_assistant.services.shorts.render_service import ShortsRenderService, safe_filename, unique_output_path
from creator_assistant.services.shorts.channel_assets import ChannelAssetStore
from creator_assistant.services.shorts.subtitle_service import SubtitleService
from creator_assistant.domain.shorts.models import SubtitleCue


def source(path: Path, hdr=False):
    return SourceInfo(str(path), path.name, 100, 1, 120.0, 1920, 1080, 29.97, "h264", "aac", 2, 48000, "HDR" if hdr else "SDR", fingerprint="fp")


def test_filter_graph_contains_trim_reframe_subtitles_and_audio():
    candidate = Candidate("short_001", 10.125, 55.5, 90, "text", layout_settings={"mode": "center_crop", "crop_center": 70})
    graph = ShortsFilterGraphBuilder().build(candidate, source(Path("C:/видео автора's.mp4")), "short_001.ass")
    assert "trim=start=10.125:end=55.500" in graph
    assert "setpts=PTS-STARTPTS" in graph
    assert "crop=1080:1920" in graph
    assert "subtitles=filename='short_001.ass':charenc=UTF-8" in graph
    assert "atrim=start=10.125:end=55.500,asetpts=PTS-STARTPTS" in graph


def test_hdr_blur_graph_includes_tonemap():
    candidate = Candidate("short_001", 0, 30, 1, "", layout_settings={"mode": "blur_background", "foreground_scale": 95})
    graph = ShortsFilterGraphBuilder().build(candidate, source(Path("x.mp4"), hdr=True), "")
    assert "boxblur" in graph and "tonemap=hable" in graph and "p=bt709" in graph


def test_rotation_metadata_swaps_geometry_before_crop():
    rotated = source(Path("rotated.mp4"))
    rotated.width, rotated.height, rotated.rotation = 1920, 1080, 90
    graph = ShortsFilterGraphBuilder().build(Candidate("id", 0, 10, 1, ""), rotated)
    assert "scale=1080:1920,crop=1080:1920:0:0" in graph


def test_command_is_argument_list_with_unicode_apostrophe_paths(tmp_path):
    service = ShortsRenderService(None, "ffmpeg.exe", "ffprobe.exe", True, 0)
    video = tmp_path / "ролик автора's.mp4"
    subtitle = tmp_path / "short_001.ass"; subtitle.write_text("", encoding="utf-8")
    target = tmp_path / "готовый ролик.mp4"
    command = service.build_command(source(video), Candidate("short_001", 1, 31, 1, ""), subtitle, target, True)
    assert command[command.index("-i") + 1] == str(video)
    assert command[-1] == str(target)
    assert "h264_nvenc" in command
    assert command[command.index("-ss") + 1] == "1.000"
    assert command.index("-ss") < command.index("-i")
    assert command[command.index("-t") + 1] == "30.000"
    assert "trim=start=" not in command[command.index("-filter_complex") + 1]
    assert "-fps_mode" in command and "vfr" in command


def test_render_command_adds_title_and_channel_banner_overlay(tmp_path):
    service = ShortsRenderService(None, "ffmpeg.exe", "ffprobe.exe", True, 0)
    video = tmp_path / "input.mp4"
    subtitle = tmp_path / "short_001.ass"; subtitle.write_text("", encoding="utf-8")
    banner = tmp_path / "beppo_ru_subscribe.png"; banner.write_bytes(b"png")
    target = tmp_path / "out.mp4"
    candidate = Candidate(
        "short_001", 0, 10, 1, "",
        branding_settings={
            "show_title": True,
            "final_title_text": "Лучший момент: Beppo",
            "title_size": 84,
            "title_y": 160,
            "show_channel_card": True,
            "channel_banner_path": str(banner),
            "banner_scale": 80,
            "banner_x": 40,
            "banner_y": 1540,
            "banner_opacity": 90,
        },
    )
    command = service.build_command(source(video), candidate, subtitle, target, True)
    graph = command[command.index("-filter_complex") + 1]
    assert str(banner) in command
    assert graph.count("drawtext=") == 1
    assert "overlay=x=40:y=1540" in graph
    assert "colorchannelmixer=aa=0.900" in graph


def test_channel_assets_resolve_exact_profile_and_never_random(tmp_path):
    root = tmp_path / "channels"
    banner = root / "beppo_ru" / "beppo_ru_subscribe.png"
    banner.parent.mkdir(parents=True)
    banner.write_bytes(b"png")
    (banner.parent / "profile.json").write_text(
        json.dumps({
            "id": "beppo_ru",
            "display_name": "Beppo На Русском",
            "handle": "@BeppoJoeRussian",
            "source_author": "Beppo",
            "aliases": ["Beppo", "BeppoJoe", "Bep", "Беппо"],
            "subscribe_banner": "beppo_ru_subscribe.png",
            "enabled": True,
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    store = ChannelAssetStore(root)
    assert store.resolve(source_author="BeppoJoe").id == "beppo_ru"
    assert store.resolve(source_author="MylesMC") is None


def test_blur_background_is_processed_low_resolution_before_upscale():
    candidate = Candidate("short_001", 0, 30, 1, "", layout_settings={"mode": "blur_background"})
    graph = ShortsFilterGraphBuilder().build(candidate, source(Path("x.mp4")), "", input_clipped=True)
    assert "scale=270:480" in graph
    assert "boxblur=12:6" in graph
    assert "scale=1080:1920:flags=fast_bilinear" in graph
    assert graph.count("split=2") == 1


def test_solid_color_background_uses_black_canvas_and_preserves_single_decode():
    candidate = Candidate(
        "short_001", 0, 30, 1, "",
        layout_settings={"mode": "solid_color", "foreground_scale": 150},
    )
    graph = ShortsFilterGraphBuilder().build(candidate, source(Path("x.mp4")), "", input_clipped=True)
    assert "color=c=black:s=1080x1920:r=30000/1001" in graph
    assert "scale=1620:2880:force_original_aspect_ratio=decrease" in graph
    assert "overlay=(W-w)/2:(H-h)/2:shortest=1" in graph


def test_nvenc_failure_falls_back_and_ffprobe_validates(tmp_path):
    class Runner:
        commands = []

        def run(self, command, **kwargs):
            self.commands.append(list(command))
            if command[0] == "ffmpeg.exe":
                if "h264_nvenc" in command:
                    raise ProcessExecutionError("NVENC недоступен")
                Path(command[-1]).write_bytes(b"mp4")
                callback = kwargs.get("on_line")
                if callback:
                    callback("out_time_us=30000000")
                    callback("progress=end")
                return ProcessResult(list(command), 0, "")
            payload = {"format": {"duration": "30.0"}, "streams": [
                {"codec_type": "video", "codec_name": "h264", "width": 1080, "height": 1920},
                {"codec_type": "audio", "codec_name": "aac"},
            ]}
            text = json.dumps(payload)
            return ProcessResult(list(command), 0, text, stdout=text)

    runner = Runner()
    service = ShortsRenderService(runner, "ffmpeg.exe", "ffprobe.exe", True, 0)
    subtitle = tmp_path / "short_001.ass"; subtitle.write_text("ass", encoding="utf-8")
    target = tmp_path / "render.mp4"
    progress = []
    result = service.render(source(tmp_path / "input.mp4"), Candidate("short_001", 5, 35, 1, ""), subtitle, target, CancellationToken(), progress.append)
    assert result.is_file()
    assert service.last_encoder == "libx264"
    assert any("h264_nvenc" in command for command in runner.commands)
    assert any("libx264" in command for command in runner.commands)
    assert progress[-1] == 100


def test_filename_conflicts_and_windows_characters(tmp_path):
    assert safe_filename('Что: это? <Short>') == "Что_ это_ _Short_"
    first = unique_output_path(tmp_path, "Видео", 1)
    first.write_bytes(b"done")
    assert unique_output_path(tmp_path, "Видео", 1).name == "Видео [Short 01] (2).mp4"


def test_render_rejects_invalid_bounds_before_process(tmp_path):
    service = ShortsRenderService(None, "ffmpeg", "ffprobe", False, 0)
    with pytest.raises(InvalidClipError):
        service.render(source(tmp_path / "input.mp4"), Candidate("id", 30, 20, 1, ""), tmp_path / "x.ass", tmp_path / "x.mp4", CancellationToken())


def test_subtitle_vertical_offset_is_written_to_ass_and_clamped(tmp_path):
    service = SubtitleService()
    ass = tmp_path / "out.ass"
    service.write(
        [SubtitleCue(0, 3, "Очень длинная русская строка для проверки позиции")],
        tmp_path / "out.srt",
        ass,
        {"style": "clean", "position": "lower", "vertical_offset": 100, "safe_margin": 120, "lines": 2},
    )
    text = ass.read_text(encoding="utf-8-sig")
    assert r"\pos(540,1585)" in text
    service.write(
        [SubtitleCue(0, 3, "Очень длинная русская строка для проверки позиции")],
        tmp_path / "clamped.srt",
        ass,
        {"style": "large", "position": "lower", "vertical_offset": 300, "safe_margin": 220, "lines": 2},
    )
    assert r"\pos(540,1785)" not in ass.read_text(encoding="utf-8-sig")
