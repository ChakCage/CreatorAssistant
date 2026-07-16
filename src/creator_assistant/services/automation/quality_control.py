from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtGui import QImageReader

from creator_assistant.domain.automation.models import AutomationIssue, AutomationShort, RenderArtifact
from creator_assistant.services.shorts.overlay_layout import OverlayLayoutCalculator
from creator_assistant.services.shorts.text_alignment import HorizontalTextAlignment
from creator_assistant.services.shorts.subtitle_service import SubtitleService
from creator_assistant.domain.shorts.models import SubtitleCue


class AutomationQualityControl:
    INVALID_TITLES = {"", "видос", "video", "output", "untitled"}

    def pre_render(self, short: AutomationShort, *, transcript_exists: bool, profile_required: bool = False) -> list[AutomationIssue]:
        issues: list[AutomationIssue] = []
        if not transcript_exists:
            issues.append(AutomationIssue("missing_transcript", "Расшифровка отсутствует", True, "pre_render", short.short_id))
        cues = short.subtitle_settings.get("cues", [])
        if not cues:
            issues.append(AutomationIssue("missing_subtitles", "События субтитров отсутствуют", True, "pre_render", short.short_id))
        else:
            subtitle_problems = SubtitleService.validate(
                [SubtitleCue(float(item["start"]), float(item["end"]), str(item["text"])) for item in cues],
                short.duration,
                short.subtitle_settings,
            )
            issues.extend(
                AutomationIssue("invalid_subtitles", problem, True, "pre_render", short.short_id)
                for problem in subtitle_problems
            )
        if not str(short.candidate_data.get("text", "")).strip():
            issues.append(AutomationIssue("empty_candidate_text", "У кандидата отсутствует связный transcript", True, "pre_render", short.short_id))
        candidate_text = str(short.candidate_data.get("text", "")).strip()
        if candidate_text and candidate_text[-1] not in ".!?…)]}\"'»":
            issues.append(AutomationIssue("cut_phrase", "Конец кандидата может обрывать фразу", True, "pre_render", short.short_id))
        if any("обрыв" in str(value).casefold() for value in short.candidate_data.get("warnings", [])):
            issues.append(AutomationIssue("boundary_warning", "Анализ отметил возможный обрыв фразы", True, "pre_render", short.short_id))
        if short.title.strip().casefold() in self.INVALID_TITLES:
            issues.append(AutomationIssue("invalid_title", "Заголовок отсутствует или является техническим", True, "pre_render", short.short_id))
        if profile_required and not short.profile_id:
            issues.append(AutomationIssue("missing_profile", "Обязательный профиль канала не определён", True, "pre_render", short.short_id))
        banner = str(short.branding_settings.get("channel_banner_path", ""))
        if short.branding_settings.get("show_channel_card") and not Path(banner).is_file():
            issues.append(AutomationIssue("missing_banner", "Баннер профиля не найден", profile_required, "pre_render", short.short_id))
        banner_size = None
        if Path(banner).is_file():
            size = QImageReader(banner).size()
            if size.isValid():
                banner_size = (size.width(), size.height())
        calculator = OverlayLayoutCalculator()
        for cue in cues:
            layout = calculator.subtitle_layout(
                str(cue.get("text", "")), short.subtitle_settings,
                short.branding_settings, banner_size,
            )
            if layout.alignment is HorizontalTextAlignment.LEFT:
                left = layout.x
            elif layout.alignment is HorizontalTextAlignment.RIGHT:
                left = layout.x - layout.width
            else:
                left = layout.x - layout.width / 2
            if left < layout.safe_margin or left + layout.width > 1080 - layout.safe_margin:
                issues.append(AutomationIssue("subtitle_outside_safe_area", "Субтитры выходят за безопасную область", True, "pre_render", short.short_id))
                break
            if banner_size and short.branding_settings.get("show_channel_card"):
                banner_rect = calculator.banner_rect(*banner_size, short.branding_settings)
                if layout.y + layout.height / 2 > banner_rect.y:
                    issues.append(AutomationIssue("subtitle_banner_overlap", "Субтитры перекрывают баннер", True, "pre_render", short.short_id))
                    break
        if not 0 < short.duration <= 75.5:
            issues.append(AutomationIssue("invalid_duration", "Длительность Short вне допустимого диапазона", True, "pre_render", short.short_id))
        return issues

    def inspect_frames(self, runner, ffmpeg: str, short: AutomationShort) -> list[AutomationIssue]:
        if not short.artifact:
            return [AutomationIssue("missing_artifact", "Невозможно проверить кадры без артефакта", True, "post_render", short.short_id)]
        positions = (0.1, max(0.1, short.duration / 2), max(0.1, short.duration - 0.2))
        averages = []
        try:
            for position in positions:
                result = runner.run([
                    ffmpeg, "-hide_banner", "-v", "error", "-ss", f"{position:.3f}",
                    "-i", short.artifact.output_path, "-frames:v", "1",
                    "-vf", "signalstats,metadata=print:file=-", "-f", "null", "NUL",
                ])
                for line in result.stdout.splitlines():
                    if "lavfi.signalstats.YAVG=" in line:
                        averages.append(float(line.rsplit("=", 1)[-1]))
                        break
        except Exception as exc:
            return [AutomationIssue("frame_decode_failed", f"Не удалось декодировать контрольные кадры: {exc}", True, "post_render", short.short_id)]
        if len(averages) == 3 and all(value < 1.0 for value in averages):
            return [AutomationIssue("black_render", "Первый, средний и последний кадры полностью чёрные", True, "post_render", short.short_id)]
        return []

    def probe_render(self, runner, ffprobe: str, short: AutomationShort, expected_fps: float) -> tuple[RenderArtifact, list[AutomationIssue]]:
        path = Path(short.artifact.output_path if short.artifact else "")
        issues: list[AutomationIssue] = []
        if not path.is_file() or path.stat().st_size < 100_000:
            issues.append(AutomationIssue("missing_or_small_render", "Файл рендера отсутствует или подозрительно мал", True, "post_render", short.short_id))
            return RenderArtifact(short.candidate_id, short.candidate_rank, str(path)), issues
        result = runner.run([ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)])
        raw = json.loads(result.stdout)
        streams = raw.get("streams", [])
        video = next((item for item in streams if item.get("codec_type") == "video"), {})
        audio = next((item for item in streams if item.get("codec_type") == "audio"), {})
        rate = str(video.get("avg_frame_rate", "0/1"))
        numerator, denominator = (rate.split("/", 1) + ["1"])[:2]
        fps = float(numerator) / max(float(denominator), 1.0)
        duration = float(raw.get("format", {}).get("duration", 0) or 0)
        artifact = RenderArtifact(
            short.candidate_id, short.candidate_rank, str(path), duration,
            int(video.get("width", 0) or 0), int(video.get("height", 0) or 0), fps,
            str(video.get("codec_name", "")), str(audio.get("codec_name", "")), path.stat().st_size,
        )
        if (artifact.width, artifact.height) != (1080, 1920):
            issues.append(AutomationIssue("invalid_resolution", "Ожидалось разрешение 1080×1920", True, "post_render", short.short_id))
        if not video or not audio:
            issues.append(AutomationIssue("missing_stream", "Отсутствует видео- или аудиопоток", True, "post_render", short.short_id))
        if abs(duration - short.duration) > max(1.0, short.duration * 0.04):
            issues.append(AutomationIssue("duration_mismatch", "Длительность рендера не совпадает с кандидатом", True, "post_render", short.short_id))
        if expected_fps and abs(fps - expected_fps) > 0.2:
            issues.append(AutomationIssue("fps_mismatch", "FPS рендера не совпадает с источником", True, "post_render", short.short_id))
        if artifact.video_codec != "h264" or artifact.audio_codec != "aac":
            issues.append(AutomationIssue("codec_mismatch", "Ожидались H.264 и AAC", True, "post_render", short.short_id))
        artifact.validated = not any(item.critical for item in issues)
        return artifact, issues
