from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from threading import RLock
from typing import List, Optional

from creator_assistant.domain.automation.models import AutomationJob, AutomationStatus
from creator_assistant.infrastructure.settings_store import local_data_root


class AutomationJobStore:
    def __init__(self, root: Optional[Path] = None) -> None:
        self.root = root or (local_data_root() / "automation" / "jobs")
        self._lock = RLock()

    def path_for(self, job_id: str) -> Path:
        safe = "".join(char for char in job_id if char.isalnum() or char in "-_")
        if not safe:
            raise ValueError("Invalid automation job id")
        return self.root / f"{safe}.json"

    def save(self, job: AutomationJob) -> Path:
        with self._lock:
            path = self.path_for(job.job_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
            temporary.write_text(json.dumps(job.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
            for attempt in range(6):
                try:
                    os.replace(str(temporary), str(path))
                    break
                except PermissionError:
                    if attempt == 5:
                        raise
                    time.sleep(0.025 * (attempt + 1))
            return path

    def load(self, job_id: str) -> Optional[AutomationJob]:
        path = self.path_for(job_id)
        if not path.is_file():
            return None
        return AutomationJob.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def list(self) -> List[AutomationJob]:
        if not self.root.is_dir():
            return []
        jobs = []
        for path in self.root.glob("*.json"):
            try:
                jobs.append(AutomationJob.from_dict(json.loads(path.read_text(encoding="utf-8"))))
            except (OSError, ValueError, TypeError):
                continue
        return sorted(jobs, key=lambda item: item.updated_at or item.created_at, reverse=True)

    def unfinished(self) -> List[AutomationJob]:
        terminal = {
            AutomationStatus.COMPLETED.value, AutomationStatus.CANCELLED.value,
            AutomationStatus.FAILED.value, AutomationStatus.AI_FAILED.value,
        }
        return [job for job in self.list() if job.status not in terminal]

    def delete(self, job_id: str) -> bool:
        with self._lock:
            path = self.path_for(job_id)
            if not path.is_file():
                return False
            path.unlink()
            return True
