from __future__ import annotations

import mimetypes
import os
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from creator_assistant.domain.errors import JobCancelledError, ProcessExecutionError
from creator_assistant.domain.job import CancellationToken


CONTENT_EXTENSIONS = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}


class ThumbnailService:
    def download(self, url: str, destination_stem: Path, cancellation: CancellationToken) -> Path:
        cancellation.raise_if_cancelled()
        request = Request(url, headers={"User-Agent": "CreatorAssistant/0.1"})
        try:
            with urlopen(request, timeout=30) as response:
                content_type = response.headers.get_content_type()
                extension = CONTENT_EXTENSIONS.get(content_type)
                if not extension:
                    extension = Path(urlparse(url).path).suffix.lower()
                if extension not in (".jpg", ".jpeg", ".png", ".webp"):
                    extension = ".jpg"
                target = destination_stem.with_suffix(extension)
                temporary = target.with_name(target.name + ".tmp")
                with temporary.open("wb") as output:
                    while True:
                        cancellation.raise_if_cancelled()
                        chunk = response.read(1024 * 256)
                        if not chunk:
                            break
                        output.write(chunk)
                if temporary.stat().st_size == 0:
                    raise ProcessExecutionError("YouTube вернул пустое превью.")
                os.replace(str(temporary), str(target))
                return target
        except (JobCancelledError, ProcessExecutionError):
            raise
        except Exception as exc:
            raise ProcessExecutionError("Не удалось скачать превью.", str(exc)) from exc
