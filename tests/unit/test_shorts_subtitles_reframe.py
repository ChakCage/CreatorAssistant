from creator_assistant.domain.shorts.models import Candidate, Transcript, TranscriptSegment
from creator_assistant.services.shorts.reframe.blur_background import BlurBackgroundReframe
from creator_assistant.services.shorts.reframe.center_crop import CenterCropReframe, crop_geometry
from creator_assistant.services.shorts.subtitle_service import SubtitleService, ass_timestamp, wrap_subtitle


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
    assert "Segoe UI" in ass_text and "Привет" in ass_text
    parsed = service.parse_srt(srt)
    assert parsed[0].text == "Привет, Minecraft!"
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
    assert "boxblur=30:15" in graph
    assert "overlay=(W-w)/2:(H-h)/2" in graph
    assert "scale=972:1728" in graph
