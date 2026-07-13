from __future__ import annotations

import uuid
from pathlib import Path


def _quote(value: str) -> str:
    return value.replace("\\", "/").replace('"', "''")


class ReaperService:
    def generate_project(
        self,
        output: Path,
        proxy_video: Path,
        instrumental: Path,
        duration: float,
        initial_audio: str = "original",
        proxy_height: int = 720,
    ) -> Path:
        duration = max(0.001, float(duration))
        video_muted = 1 if initial_audio == "instrumental" else 0
        instrumental_muted = 0 if initial_audio in ("instrumental", "both") else 1
        video_guid = "{" + str(uuid.uuid4()).upper() + "}"
        inst_guid = "{" + str(uuid.uuid4()).upper() + "}"
        project = f'''<REAPER_PROJECT 0.1 "7.0/win64" 0
  RIPPLE 0
  GROUPOVERRIDE 0 0 0
  AUTOXFADE 1
  TEMPO 120 4 4
  <TRACK {video_guid}
    NAME "VIDEO {proxy_height}P — ORIGINAL"
    PEAKCOL 16576
    MUTESOLO {video_muted} 0 0
    <ITEM
      POSITION 0
      LENGTH {duration:.9f}
      SOFFS 0
      PLAYRATE 1 1 0 -1 0 0.0025
      NAME "VIDEO {proxy_height}P — ORIGINAL"
      <SOURCE VIDEO
        FILE "{_quote(str(proxy_video))}"
      >
    >
  >
  <TRACK {inst_guid}
    NAME "INSTRUMENTAL"
    PEAKCOL 16761024
    MUTESOLO {instrumental_muted} 0 0
    <ITEM
      POSITION 0
      LENGTH {duration:.9f}
      SOFFS 0
      PLAYRATE 1 1 0 -1 0 0.0025
      NAME "INSTRUMENTAL"
      <SOURCE FLAC
        FILE "{_quote(str(instrumental))}"
      >
    >
  >
>
'''
        temporary = output.with_suffix(".rpp.tmp")
        temporary.write_text(project, encoding="utf-8")
        temporary.replace(output)
        return output

    @staticmethod
    def validate_project(path: Path, proxy_video: Path, instrumental: Path) -> bool:
        if not path.is_file() or path.stat().st_size == 0:
            return False
        content = path.read_text(encoding="utf-8", errors="replace")
        return (
            content.count("<TRACK ") == 2
            and _quote(str(proxy_video)) in content
            and _quote(str(instrumental)) in content
        )
