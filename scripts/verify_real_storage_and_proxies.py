"""Short, non-destructive verification of real FFmpeg and per-job temp routing."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from creator_assistant.domain.job import CancellationToken
from creator_assistant.infrastructure.process_runner import ProcessRunner
from creator_assistant.services.ffmpeg_service import FfmpegService
from creator_assistant.services.storage_service import JobTempManager
from creator_assistant.services.reaper_service import ReaperService


def main() -> int:
    if len(sys.argv) != 4:
        raise SystemExit("usage: verify_real_storage_and_proxies.py TEMP_ROOT FFMPEG FFPROBE")
    temp_root, ffmpeg, ffprobe = map(Path, sys.argv[1:])
    job = JobTempManager(temp_root, "verification-job")
    work = job.create()
    env = job.environment()
    source = work / "source-1080p.mp4"
    subprocess.run(
        [
            str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=30:duration=1",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
            "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(source),
        ],
        cwd=work,
        env={**__import__("os").environ, **env},
        check=True,
    )
    runner = ProcessRunner()
    service = FfmpegService(runner, str(ffmpeg), nvenc_available=False, ffprobe_path=str(ffprobe))
    dimensions = {}
    for height in (480, 720, 1080):
        target = work / f"Verification [{height}p].mp4"
        service.create_proxy(
            source, target, CancellationToken(), maximum_height=height,
            environment=env, cwd=work,
        )
        probe = subprocess.run(
            [str(ffprobe), "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height", "-of", "json", str(target)],
            capture_output=True, text=True, check=True,
        )
        stream = json.loads(probe.stdout)["streams"][0]
        dimensions[str(height)] = [stream["width"], stream["height"]]
    marker = work / "child-environment.json"
    child = (
        "import json,os,pathlib; "
        f"pathlib.Path({str(marker)!r}).write_text(json.dumps({{k:os.environ.get(k) for k in ('TEMP','TMP','TMPDIR')}}))"
    )
    runner.run([sys.executable, "-c", child], environment=env, cwd=work)
    child_environment = json.loads(marker.read_text(encoding="utf-8"))
    instrumental = work / "Verification [Instrumental].flac"
    subprocess.run(
        [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y", "-i", str(source), "-vn", "-c:a", "flac", str(instrumental)],
        cwd=work, env={**__import__("os").environ, **env}, check=True,
    )
    selected_proxy = work / "Verification [1080p].mp4"
    rpp = work / "Verification.rpp"
    reaper = ReaperService()
    reaper.generate_project(rpp, selected_proxy, instrumental, 1.0, proxy_height=1080)
    rpp_text = rpp.read_text(encoding="utf-8")
    rpp_valid = reaper.validate_project(rpp, selected_proxy, instrumental)
    result = {
        "job_temp": str(work),
        "proxy_dimensions": dimensions,
        "child_environment": child_environment,
        "all_outputs_valid": dimensions == {"480": [854, 480], "720": [1280, 720], "1080": [1920, 1080]},
        "selected_output": selected_proxy.name,
        "rpp_references_selected_output": rpp_valid and selected_proxy.name in rpp_text and "VIDEO 1080P" in rpp_text,
    }
    job.mark_completed()
    job.cleanup_current()
    result["current_job_cleaned"] = not work.exists()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["all_outputs_valid"] and result["rpp_references_selected_output"] and result["current_job_cleaned"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
