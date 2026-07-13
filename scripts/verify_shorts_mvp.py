"""Run an opt-in, real local end-to-end Shorts validation (no mocks)."""
from __future__ import annotations

import json
import logging
import re
import shutil
import time
from dataclasses import asdict
from pathlib import Path

from creator_assistant.domain.job import CancellationToken
from creator_assistant.infrastructure.process_runner import ProcessRunner
from creator_assistant.services.shorts.audio_activity_service import AudioActivityService
from creator_assistant.services.shorts.audio_extract_service import TranscriptionAudioService
from creator_assistant.services.shorts.candidate_generator import CandidateGenerator, CandidateSettings
from creator_assistant.services.shorts.candidate_scorer import HeuristicCandidateScorer
from creator_assistant.services.shorts.duplicate_filter import DuplicateFilter
from creator_assistant.services.shorts.proxy_service import AnalysisProxyService
from creator_assistant.services.shorts.render_service import ShortsRenderService
from creator_assistant.services.shorts.scene_detection_service import SceneDetectionService
from creator_assistant.services.shorts.shorts_project_store import ShortsProjectStore
from creator_assistant.services.shorts.source_service import ShortsSourceService
from creator_assistant.services.shorts.subtitle_service import SubtitleService
from creator_assistant.services.shorts.transcription.existing_whisper import ExistingWhisperBackend, find_existing_python
from creator_assistant.services.shorts.transcription_service import TranscriptionService


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    target = root / "build" / "shorts-mvp-validation"
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    runner = ProcessRunner(logging.getLogger("shorts-validation"))
    ffmpeg = shutil.which("ffmpeg") or shutil.which("ffmpeg.exe")
    ffprobe = shutil.which("ffprobe") or shutil.which("ffprobe.exe")
    if not ffmpeg or not ffprobe:
        raise RuntimeError("FFmpeg/FFprobe not found in PATH")
    token = CancellationToken()
    speech = target / "speech_ru.wav"
    source_file = target / "validation_source_ru.mp4"
    escaped = str(speech).replace("'", "''")
    sapi = (
        "Add-Type -AssemblyName System.Speech; $s=New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        "$s.SelectVoice('Microsoft Irina Desktop'); "
        f"$s.SetOutputToWaveFile('{escaped}'); "
        "$s.Speak('Как победить в Майнкрафт? Сегодня мы проверяем настоящий вертикальный ролик. Редстоун и хардкор работают. В результате всё получилось!'); $s.Dispose()"
    )
    runner.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", sapi], cancellation=token, timeout=60)
    runner.run([
        ffmpeg, "-hide_banner", "-y", "-f", "lavfi", "-i", "color=c=blue:s=1280x720:r=30:d=15",
        "-i", str(speech), "-filter_complex", "[1:a]apad[a]", "-map", "0:v:0", "-map", "[a]",
        "-t", "15", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18", "-c:a", "aac", "-b:a", "192k", str(source_file),
    ], cancellation=token)
    source = ShortsSourceService(runner, ffprobe).probe(source_file)
    paths = ShortsProjectStore().create(target / "Shorts", source)
    proxy = AnalysisProxyService(runner, ffmpeg, False).create(source_file, paths.cache / "analysis_proxy.mp4", token)
    audio = TranscriptionAudioService(runner, ffmpeg).extract(source_file, paths.cache / "transcription_audio.wav", token)
    backend = ExistingWhisperBackend(runner, {
        "whisper_python": find_existing_python(), "whisper_model_dir": str(Path.home() / ".cache" / "whisper"),
        "whisper_model": "large-v3-turbo", "whisper_language": "ru", "whisper_device": "gpu",
        "whisper_use_gpu": True, "whisper_fp16": True, "whisper_word_timestamps": True,
        "whisper_use_dictionary": True, "whisper_dictionary": ["Minecraft", "редстоун", "хардкор", "Shorts"],
    })
    started = time.monotonic()
    transcript = TranscriptionService(backend).transcribe(audio, paths.analysis, token)
    whisper_seconds = time.monotonic() - started
    scenes = SceneDetectionService(runner, ffmpeg).detect(proxy, source.duration, paths.analysis / "scenes.json", token)
    features = AudioActivityService(runner, ffmpeg).analyse(audio, source.duration, paths.analysis / "audio_features.json", token)
    settings = CandidateSettings(minimum=4, maximum=14, desired=9, count=3)
    raw = CandidateGenerator().generate(transcript, scenes, features, settings)
    scored = [HeuristicCandidateScorer().score(item, scenes, features) for item in raw]
    candidates = DuplicateFilter().filter(scored, 3)
    if not candidates:
        raise RuntimeError("Real transcript produced no candidates")
    candidate = candidates[0]
    candidate.status = "approved"
    candidate.layout_settings = {"mode": "center_crop", "crop_center": 50, "foreground_scale": 100}
    candidate.subtitle_settings = {"style": "gaming", "position": "lower", "size": 68, "outline": 4, "shadow": 2, "safe_margin": 180}
    (paths.analysis / "candidates.json").write_text(json.dumps([asdict(item) for item in candidates], ensure_ascii=False, indent=2), encoding="utf-8")
    subtitle_service = SubtitleService()
    cues = subtitle_service.generate(transcript, candidate, 30, 2)
    if not cues:
        raise RuntimeError("No real subtitle cues were generated")
    srt, ass = paths.subtitles / f"{candidate.id}.srt", paths.subtitles / f"{candidate.id}.ass"
    subtitle_service.write(cues, srt, ass, candidate.subtitle_settings)
    output = paths.renders / "Creator Assistant Shorts MVP Validation.mp4"
    renderer = ShortsRenderService(runner, ffmpeg, ffprobe, True, 0)
    renderer.render(source, candidate, ass, output, token)
    validation = renderer.validate(output, candidate.duration, token)
    sample_at = max(0.1, min(candidate.duration - 0.1, (cues[0].start + cues[0].end) / 2))
    pixels = runner.run([
        ffmpeg, "-hide_banner", "-ss", f"{sample_at:.3f}", "-i", str(output), "-frames:v", "1",
        "-vf", "signalstats,metadata=print", "-f", "null", "-",
    ], cancellation=token)
    ymin = re.search(r"lavfi.signalstats.YMIN=([0-9]+)", pixels.output)
    ymax = re.search(r"lavfi.signalstats.YMAX=([0-9]+)", pixels.output)
    if not ymin or not ymax or int(ymax.group(1)) - int(ymin.group(1)) < 20:
        raise RuntimeError("Burned subtitle pixels were not detected in the solid-colour validation frame")
    report = {
        "source": asdict(source), "whisper_backend": asdict(backend.capabilities()),
        "whisper_seconds": round(whisper_seconds, 3), "transcript": transcript.text,
        "segments": len(transcript.segments), "words": sum(len(item.words) for item in transcript.segments),
        "scenes": len(scenes), "candidates": [asdict(item) for item in candidates],
        "render": str(output), "encoder": renderer.last_encoder,
        "video": next(item for item in validation["streams"] if item.get("codec_type") == "video"),
        "audio": next(item for item in validation["streams"] if item.get("codec_type") == "audio"),
        "subtitle_pixel_range": {"ymin": int(ymin.group(1)), "ymax": int(ymax.group(1))},
    }
    (paths.reports / "validation.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (paths.reports / "analysis_summary.md").write_text(
        "# Shorts MVP real validation\n\n"
        f"- Whisper: `{report['whisper_backend']['version']}` / `large-v3-turbo` / CUDA\n"
        f"- Transcription: {report['whisper_seconds']} s, {report['segments']} segments, {report['words']} words\n"
        f"- Candidates: {len(candidates)}\n- Render encoder: `{renderer.last_encoder}`\n"
        f"- Output: `{output}`\n- Burned subtitle Y range: {ymin.group(1)}–{ymax.group(1)}\n",
        encoding="utf-8",
    )
    speech.unlink(missing_ok=True)
    print(json.dumps({"ok": True, "render": str(output), "report": str(paths.reports / "validation.json"), "whisper_seconds": report["whisper_seconds"], "encoder": renderer.last_encoder}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
