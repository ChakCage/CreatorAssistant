"""Private, resumable research ingest via the existing CA TranscriptionBackend.

Never writes to source directories. Output must be inside the ignored runs/ tree.
"""
import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from creator_assistant.domain.job import CancellationToken
from creator_assistant.infrastructure.process_runner import ProcessRunner
from creator_assistant.services.shorts.transcription.existing_whisper import ExistingWhisperBackend
from creator_assistant.services.shorts.transcription_service import TranscriptionService
from ai_editor_copilot.feedback.events import save_json


def fingerprint(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    source = args.source.resolve(strict=True)
    output = args.output.resolve()
    private_root = Path(__file__).resolve().parents[1] / "runs"
    if private_root not in output.parents:
        raise ValueError("private evidence must be stored under research runs/")
    print("Hashing source (read-only)", flush=True)
    before = source.stat()
    digest = fingerprint(source)
    probe = json.loads(subprocess.check_output(["ffprobe", "-v", "error", "-show_entries",
        "format=duration,size", "-of", "json", str(source)], text=True))
    manifest_path = output / "input_manifest.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest["source_sha256"] != digest:
            raise ValueError("existing evidence belongs to another source")
        if fingerprint(output / "transcript.json") != manifest["transcript_sha256"]:
            raise ValueError("immutable raw transcript hash mismatch")
        print("Verified existing immutable evidence", flush=True)
        return
    if (output / "transcript.json").exists():
        raise ValueError("unsealed transcript exists: inspect manually; never overwrite evidence")
    backend = ExistingWhisperBackend(ProcessRunner(), {
        "whisper_model": "large-v3-turbo", "whisper_language": "en",
        "whisper_device": "cuda", "whisper_use_gpu": True,
        "whisper_word_timestamps": True, "whisper_use_dictionary": False,
    })
    capabilities = backend.capabilities()
    if "large-v3-turbo" not in capabilities.models:
        raise ValueError("required existing Whisper model missing; no implicit download")
    print("Transcribing with existing CA backend", capabilities.executable, flush=True)
    transcript = TranscriptionService(backend).transcribe(source, output, CancellationToken(),
        on_line=lambda line: print(line, flush=True))
    after = source.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError("source changed during ingest")
    save_json(manifest_path, {
        "source_sha256": digest, "source_path": str(source), "source_size": before.st_size,
        "source_mtime_ns": before.st_mtime_ns, "duration": float(probe["format"]["duration"]),
        "transcript_sha256": fingerprint(output / "transcript.json"),
        "raw_transcript": "transcript.json", "language": transcript.language,
        "backend": transcript.backend, "model": transcript.model,
        "backend_version": capabilities.version, "word_timestamps": True,
        "ingested_at": datetime.now(timezone.utc).isoformat(),
        "rights": "User selected these three project sources for local research on 2026-09-17; no redistribution.",
        "provenance": "ExistingWhisperBackend -> TranscriptionService; immutable evidence hash verified on reuse",
    })
    print("SEALED", len(transcript.segments), "segments", flush=True)


if __name__ == "__main__":
    main()
