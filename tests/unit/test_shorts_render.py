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
from creator_assistant.services.shorts.overlay_layout import OverlayLayoutCalculator, layout_title_text
from creator_assistant.services.shorts.subtitle_service import SubtitleService
from creator_assistant.services.shorts.subtitle_layout import SubtitleLayoutCalculator
from creator_assistant.services.shorts.text_alignment import HorizontalTextAlignment
from creator_assistant.services.shorts.font_resolver import resolve_font
from creator_assistant.domain.shorts.models import SubtitleCue


def source(path: Path, hdr=False):
    return SourceInfo(str(path), path.name, 100, 1, 120.0, 1920, 1080, 29.97, "h264", "aac", 2, 48000, "HDR" if hdr else "SDR", fingerprint="fp")


def test_filter_graph_contains_trim_reframe_subtitles_and_audio():
    candidate = Candidate("short_001", 10.125, 55.5, 90, "text", layout_settings={"mode": "center_crop", "crop_center": 70})
    graph = ShortsFilterGraphBuilder().build(candidate, source(Path("C:/видео автора's.mp4")), "short_001.ass")
    assert "trim=start=10.125:end=55.500" in graph
    assert "setpts=PTS-STARTPTS" in graph
    assert "crop=1080:1920" in graph
    assert "subtitles=filename='short_001.ass'" in graph
    assert "fontsdir=" in graph
    assert ":charenc=UTF-8" in graph
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
            "title_alignment": "right",
            "title_offset_x": -120,
            "show_channel_card": True,
            "channel_banner_path": str(banner),
            "banner_scale": 80,
            "banner_offset_x": 40,
            "banner_offset_y": -60,
            "banner_opacity": 90,
        },
    )
    command = service.build_command(source(video), candidate, subtitle, target, True)
    graph = command[command.index("-filter_complex") + 1]
    assert str(banner) in command
    assert graph.count("drawtext=") >= 1
    assert "fontfile=" in graph
    assert "w-text_w-90+-120" in graph
    assert "overlay=x='max(80,min(W-w-80,(W-w)/2+40))'" in graph
    assert "H-h-80+-60" in graph
    assert "colorchannelmixer=aa=0.900" in graph


def test_subtitles_are_composited_after_banner_and_title(tmp_path):
    banner = tmp_path / "banner.png"
    banner.write_bytes(b"png")
    candidate = Candidate(
        "short_001", 0, 10, 1, "",
        branding_settings={
            "show_channel_card": True, "channel_banner_path": str(banner),
            "show_title": True, "final_title_text": "Заголовок",
        },
    )
    graph = ShortsFilterGraphBuilder().build(
        candidate, source(Path("x.mp4")), "short.ass",
        input_clipped=True, has_channel_banner=True,
    )
    assert graph.index("overlay=x=") < graph.index("drawtext=") < graph.index("subtitles=filename=")
    assert "[subtitled]" in graph


@pytest.mark.parametrize("size", [(360, 640), (540, 960), (720, 1280), (1080, 1920)])
def test_exact_preview_quality_has_real_output_dimensions(size):
    graph = ShortsFilterGraphBuilder().build(
        Candidate("short_001", 0, 10, 1, ""), source(Path("x.mp4")),
        input_clipped=True, output_size=size,
    )
    if size == (1080, 1920):
        assert "scale=1080:1920:flags=lanczos" not in graph
    else:
        assert f"scale={size[0]}:{size[1]}:flags=lanczos" in graph


def test_banner_geometry_contains_whole_wide_card_inside_safe_bounds():
    rect = OverlayLayoutCalculator().banner_rect(2048, 682, {"banner_scale": 100, "safe_margin": 80})
    assert rect.width <= 1080 * 0.90
    assert rect.x >= 80
    assert rect.x + rect.width <= 1000
    assert rect.y + rect.height <= 1840


def test_two_line_subtitle_is_automatically_kept_above_banner():
    calculator = OverlayLayoutCalculator()
    branding = {"show_channel_card": True, "banner_scale": 100, "safe_margin": 80}
    banner = calculator.banner_rect(2048, 682, branding)
    layout = calculator.subtitle_layout(
        "Я поменял камень на\nнезерский",
        {"style": "clean", "position": "lower", "lines": 2, "auto_above_banner": True, "banner_gap": 32},
        branding,
        (2048, 682),
    )
    assert layout.y + layout.height // 2 + 32 <= banner.y


def test_manual_overlap_keeps_subtitles_no_higher_than_automatic_layout():
    calculator = OverlayLayoutCalculator()
    branding = {"show_channel_card": True, "banner_scale": 100, "safe_margin": 80}
    automatic = calculator.subtitle_layout(
        "Я сделал платформу из\nкамня, а",
        {"position": "lower", "auto_above_banner": True}, branding, (2048, 682),
    )
    manual = calculator.subtitle_layout(
        "Я сделал платформу из\nкамня, а",
        {"position": "lower", "auto_above_banner": False}, branding, (2048, 682),
    )
    assert automatic.y <= manual.y


@pytest.mark.parametrize("style", ["clean", "large"])
@pytest.mark.parametrize("size", [58, 65, 80, 100])
def test_ass_and_interactive_layout_keep_identical_explicit_wrapping(tmp_path, style, size):
    text = "Теперь я буду целый час\nубивать"
    settings = {
        "style": style,
        "font_family": "Segoe UI",
        "size": size,
        "lines": 2,
        "position": "lower",
        "line_anchor_mode": "first_line_fixed",
    }
    layout = SubtitleLayoutCalculator().calculate(text, settings)
    ass = tmp_path / f"{style}_{size}.ass"
    SubtitleService().write([SubtitleCue(0, 1, text)], tmp_path / "out.srt", ass, settings)
    payload = ass.read_text(encoding="utf-8-sig")
    assert len(layout.lines) == 2
    assert r"\N".join(layout.lines) in payload
    assert "PlayResX: 1080" in payload and "PlayResY: 1920" in payload
    assert f"Style: Shorts,Segoe UI,{size}," in payload


def test_segoe_ui_resolves_to_concrete_bold_file_without_fallback():
    font = resolve_font("Segoe UI")
    assert font.ass_font_name == "Segoe UI"
    assert not font.fallback
    assert font.file_for_weight(True) is not None
    assert font.file_for_weight(True).is_file()


def test_first_line_fixed_keeps_same_baseline_for_one_and_two_lines():
    calculator = SubtitleLayoutCalculator()
    settings = {"position": "lower", "lines": 2, "line_anchor_mode": "first_line_fixed"}
    one = calculator.calculate("Ааа, ну и где они?", settings)
    two = calculator.calculate("Окей, похожая проблема\nв том, что...", settings)
    assert len(one.lines) == 1
    assert len(two.lines) == 2
    assert one.first_line_baseline == two.first_line_baseline
    assert two.second_line_baseline > two.first_line_baseline


def test_banner_bottom_and_block_center_are_distinct_line_anchor_modes():
    calculator = OverlayLayoutCalculator()
    branding = {"show_channel_card": True, "banner_scale": 100, "safe_margin": 80}
    one = "Ааа, ну и где они?"
    two = "Окей, похожая проблема\nв том, что..."
    common = {"lines": 2, "auto_above_banner": True, "banner_gap": 15, "vertical_offset": 300}
    bottom_one = calculator.subtitle_layout(one, {**common, "line_anchor_mode": "banner_bottom"}, branding, (2048, 682))
    bottom_two = calculator.subtitle_layout(two, {**common, "line_anchor_mode": "banner_bottom"}, branding, (2048, 682))
    center_one = calculator.subtitle_layout(one, {**common, "line_anchor_mode": "block_center"}, branding, (2048, 682))
    center_two = calculator.subtitle_layout(two, {**common, "line_anchor_mode": "block_center"}, branding, (2048, 682))
    assert bottom_one.y + bottom_one.height // 2 == bottom_two.y + bottom_two.height // 2
    assert center_one.y == center_two.y
    assert bottom_one.first_line_baseline != bottom_two.first_line_baseline


@pytest.mark.parametrize("gap", [0, 15, 50])
def test_configurable_banner_gap_is_applied_exactly(gap):
    calculator = OverlayLayoutCalculator()
    branding = {"show_channel_card": True, "banner_scale": 100, "safe_margin": 80}
    banner = calculator.banner_rect(2048, 682, branding)
    layout = calculator.subtitle_layout(
        "Окей, похожая проблема\nв том, что...",
        {"position": "lower", "lines": 2, "auto_above_banner": True, "banner_gap": gap, "vertical_offset": 300},
        branding, (2048, 682),
    )
    assert banner.y - (layout.y + layout.height // 2) == gap


def test_legacy_absolute_banner_x_is_not_reinterpreted_as_offset():
    centered = OverlayLayoutCalculator().banner_rect(2048, 682, {"banner_scale": 100, "safe_margin": 80})
    migrated = OverlayLayoutCalculator().banner_rect(2048, 682, {"banner_scale": 100, "safe_margin": 80, "banner_x": 50})
    assert migrated.x == centered.x


def test_long_title_is_pixel_wrapped_to_two_safe_lines():
    wrapped, effective_size = layout_title_text("100 ЧЕРЕПОВ ЗА 30 МИНУТ!", 88, True)
    assert wrapped.count("\n") == 1
    assert wrapped.replace("\n", " ") == "100 ЧЕРЕПОВ ЗА 30 МИНУТ!"
    assert effective_size <= 88


def test_title_alignment_builds_three_distinct_multiline_geometries():
    graphs = {}
    for alignment in ("left", "center", "right"):
        candidate = Candidate(
            "short_001", 0, 10, 1, "",
            branding_settings={
                "show_title": True,
                "final_title_text": "Я добыл 48 235 обсидиана — Хардкор",
                "title_size": 88,
                "title_alignment": alignment,
                "title_offset_x": 40,
            },
        )
        graphs[alignment] = ShortsFilterGraphBuilder().build(candidate, source(Path("x.mp4")), "", input_clipped=True)
        assert graphs[alignment].count("drawtext=") == 2
    assert "90+40" in graphs["left"]
    assert "(w-text_w)/2+40" in graphs["center"]
    assert "w-text_w-90+40" in graphs["right"]
    assert len(set(graphs.values())) == 3


def test_subtitle_alignment_and_offset_are_safe_and_written_to_ass(tmp_path):
    service = SubtitleService()
    cue = SubtitleCue(0, 3, "Очень длинные русские субтитры для проверки безопасных границ")
    anchors = {"left": r"\an1", "center": r"\an2", "right": r"\an3"}
    positions = {}
    for name in anchors:
        settings = {
            "style": "clean", "position": "lower", "alignment": name,
            "horizontal_offset": 300 if name != "right" else -300,
            "safe_margin": 120, "lines": 2,
        }
        ass = tmp_path / f"{name}.ass"
        service.write([cue], tmp_path / f"{name}.srt", ass, settings)
        text = ass.read_text(encoding="utf-8-sig")
        assert anchors[name] in text
        layout = SubtitleLayoutCalculator().calculate(cue.text, settings)
        positions[name] = layout.x
        if name == "left":
            assert layout.x >= layout.safe_margin
        elif name == "right":
            assert layout.x <= 1080 - layout.safe_margin
        else:
            assert layout.safe_margin <= layout.x <= 1080 - layout.safe_margin
    assert len(set(positions.values())) == 3
    assert HorizontalTextAlignment.parse("CENTER") is HorizontalTextAlignment.CENTER


@pytest.mark.parametrize(
    ("position", "alignment", "anchor"),
    [
        ("lower", "left", 1), ("lower", "center", 2), ("lower", "right", 3),
        ("center", "left", 4), ("center", "center", 5), ("center", "right", 6),
        ("upper", "left", 7), ("upper", "center", 8), ("upper", "right", 9),
    ],
)
def test_ass_anchor_maps_vertical_zone_and_horizontal_alignment(tmp_path, position, alignment, anchor):
    ass = tmp_path / f"{position}_{alignment}.ass"
    SubtitleService().write(
        [SubtitleCue(0, 1, "Проверка")], tmp_path / "out.srt", ass,
        {"position": position, "alignment": alignment, "font_family": "Segoe UI"},
    )
    assert f"\\an{anchor}\\pos(" in ass.read_text(encoding="utf-8-sig")


def test_segoe_ui_resolves_to_concrete_windows_files_without_fallback():
    info = resolve_font("Segoe UI")
    assert info.ass_font_name == "Segoe UI"
    assert info.fallback is False
    assert info.regular_file and info.regular_file.name.casefold() == "segoeui.ttf"
    assert info.bold_file and info.bold_file.name.casefold() == "segoeuib.ttf"


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
    assert r"\an2\pos(540," in text
    service.write(
        [SubtitleCue(0, 3, "Очень длинная русская строка для проверки позиции")],
        tmp_path / "clamped.srt",
        ass,
        {"style": "large", "position": "lower", "vertical_offset": 300, "safe_margin": 220, "lines": 2},
    )
    assert r"\pos(540,1785)" not in ass.read_text(encoding="utf-8-sig")
