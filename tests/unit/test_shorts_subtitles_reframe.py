from creator_assistant.domain.shorts.models import Candidate, SubtitleCue, Transcript, TranscriptSegment
from creator_assistant.services.shorts.reframe.blur_background import BlurBackgroundReframe
from creator_assistant.services.shorts.reframe.center_crop import CenterCropReframe, crop_geometry
from creator_assistant.services.shorts.subtitle_service import (
    MAX_TEXT_WIDTH, SubtitleService, _metrics, ass_timestamp, fit_cues, wrap_subtitle,
)


def test_subtitles_are_clip_relative_and_do_not_mutate_transcript():
    segment = TranscriptSegment(0, 8, 18, "Очень длинная строка субтитров для проверки переноса на две строки")
    transcript = Transcript("ru", 30, segment.text, [segment])
    original = segment.text
    candidate = Candidate("short_001", 10, 20, 90, segment.text)
    cues = SubtitleService().generate(transcript, candidate, maximum=28, lines=2)
    assert (cues[0].start, cues[0].end) == (0.0, 8.0)
    assert "\n" in cues[0].text
    assert segment.text == original


def test_srt_ass_utf8_cyrillic_and_styles(tmp_path):
    transcript = Transcript("ru", 20, "Привет, Minecraft!", [TranscriptSegment(0, 0, 5, "Привет, Minecraft!")])
    candidate = Candidate("short_001", 0, 10, 80, transcript.text)
    service = SubtitleService()
    cues = service.generate(transcript, candidate)
    srt, ass = tmp_path / "short_001.srt", tmp_path / "short_001.ass"
    service.write(cues, srt, ass, {"style": "gaming", "position": "upper", "size": 60, "outline": 4, "shadow": 2, "background": True, "safe_margin": 200})
    assert "Привет" in srt.read_text(encoding="utf-8")
    ass_text = ass.read_text(encoding="utf-8-sig")
    assert "PlayResX: 1080" in ass_text and "PlayResY: 1920" in ass_text
    assert "Arial Black" in ass_text and "Привет" in ass_text
    parsed = service.parse_srt(srt)
    assert parsed[0].text.replace("\n", " ") == "Привет, Minecraft!"
    assert ass_timestamp(65.126) == "0:01:05.13"


def test_wrap_keeps_words_and_requested_line_count():
    text = "Minecraft редстоун хардкор YouTube Shorts"
    wrapped = wrap_subtitle(text, maximum=18, lines=2)
    assert wrapped.replace("\n", " ") == text
    assert len(wrapped.splitlines()) == 2


def test_center_crop_geometry_uses_horizontal_slider():
    left = crop_geometry(1920, 1080, 0)
    center = crop_geometry(1920, 1080, 50)
    right = crop_geometry(1920, 1080, 100)
    assert left[:2] == center[:2] == right[:2] == (3413, 1920)
    assert left[2] == 0 < center[2] < right[2] == 2333
    graph = CenterCropReframe(50).video_filter(1920, 1080)
    assert "crop=1080:1920" in graph and "setsar=1" in graph


def test_portrait_center_crop_and_blur_graph_are_valid():
    assert crop_geometry(720, 1280, 50) == (1080, 1920, 0, 0)
    graph = BlurBackgroundReframe(90).video_filter(1920, 1080)
    assert "split=2" in graph
    assert "boxblur=12:6" in graph
    assert "overlay=(W-w)/2:(H-h)/2" in graph
    assert "scale=972:1728" in graph


def test_ass_presets_are_visibly_different_and_lower_third_is_safe(tmp_path):
    service = SubtitleService()
    cue = SubtitleCue(0, 2, "Проверка разных стилей")
    outputs = {}
    for style in ("clean", "large", "gaming"):
        ass = tmp_path / f"{style}.ass"
        service.write([cue], tmp_path / f"{style}.srt", ass, {"style": style, "position": "lower"})
        outputs[style] = ass.read_text(encoding="utf-8-sig")
    assert len(set(outputs.values())) == 3
    assert "Shorts,Segoe UI,58" in outputs["clean"]
    assert "Shorts,Segoe UI,72" in outputs["large"]
    assert "Shorts,Arial Black,68" in outputs["gaming"]
    assert r"\pos(540,1260)" in outputs["clean"]


def test_long_russian_text_is_pixel_wrapped_to_two_lines_and_split_into_events():
    text = (
        "Сверхдлинноерусскоесловобезединогопробелакотороенельзяобрезать "
        "и ещё одна очень длинная русская строка для настоящей проверки безопасной ширины субтитров"
    )
    fitted, style = fit_cues([SubtitleCue(0, 6, text)], {"style": "large", "lines": 2})
    assert len(fitted) >= 2
    metrics = _metrics(style["font"], style["size"])
    available = MAX_TEXT_WIDTH - 2 * (style["outline"] + 4)
    assert all(len(cue.text.splitlines()) <= 2 for cue in fitted)
    assert all(metrics.horizontalAdvance(line) <= available + 0.5 for cue in fitted for line in cue.text.splitlines())
    assert fitted[0].start == 0 and fitted[-1].end == 6
