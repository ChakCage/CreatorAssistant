from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from creator_assistant.domain.models import ProjectPaths


INVALID_CHARS = re.compile(r'[<>:"/\\|?*]')
CONTROL_CHARS = re.compile(r"[\x00-\x1f]")
REPEATED_WHITESPACE = re.compile(r"\s+")
RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


@dataclass(frozen=True)
class NamingTemplates:
    maximum: str = "{title} [MAX {height}p]"
    proxy: str = "{title} [{proxy_height}p]"
    audio: str = "{title} [Audio]"
    instrumental: str = "{title} [Instrumental]"
    preview: str = "Preview"


def sanitize_windows_component(value: str, replacement: str = "-") -> str:
    value = unicodedata.normalize("NFC", value or "")
    value = CONTROL_CHARS.sub("", value)
    value = INVALID_CHARS.sub(replacement, value)
    value = REPEATED_WHITESPACE.sub(" ", value).strip().rstrip(". ")
    if not value:
        value = "Без названия"
    stem = value.split(".", 1)[0].upper()
    if stem in RESERVED_NAMES:
        value += "_"
    return value


def fit_component_to_path(parent: Path, component: str, reserve: int = 48, limit: int = 240) -> str:
    component = sanitize_windows_component(component)
    # Имя проекта повторяется в именах медиафайлов. Ограничиваем его так,
    # чтобы и вложенный путь «Материалы/<Название> [суффикс]» оставался безопасным.
    available = max(20, (limit - len(str(parent)) - reserve) // 2)
    if len(component) <= available:
        return component
    shortened = component[:available].rstrip(". ")
    return shortened or "Без названия"


def unique_directory_path(parent: Path, title: str) -> Path:
    safe = fit_component_to_path(parent, title)
    candidate = parent / safe
    index = 2
    while candidate.exists():
        suffix = f" ({index})"
        candidate = parent / f"{safe}{suffix}"
        index += 1
    return candidate


def exact_directory_path(parent: Path, title: str) -> Path:
    """Return a deterministic project path without probing or adding a suffix."""
    return parent / fit_component_to_path(parent, title)


def project_paths(parent: Path, title: str, create: bool = False) -> ProjectPaths:
    root = unique_directory_path(parent, title)
    paths = ProjectPaths(root=root, materials=root / "Материалы", base_name=root.name)
    if create:
        # mkdir без exist_ok намеренно гарантирует, что существующий проект не будет изменён.
        root.mkdir(parents=False, exist_ok=False)
        paths.materials.mkdir(exist_ok=False)
    return paths


def discover_author_folders(root: Path, custom_paths: Iterable[str] = ()) -> list[tuple[str, Path]]:
    found: dict[str, Path] = {}
    if root.is_dir():
        try:
            children = sorted(root.iterdir(), key=lambda path: path.name.casefold())
        except OSError:
            children = []
        for author in children:
            doing = author / "Делаю"
            if author.is_dir() and doing.is_dir():
                found[str(doing).casefold()] = doing
    for raw in custom_paths:
        path = Path(raw).expanduser()
        if path.is_dir():
            found[str(path).casefold()] = path
    return sorted(((path.parent.name, path) for path in found.values()), key=lambda item: item[0].casefold())


def safe_file_name(base: str, suffix_template: str, extension: str, **values: object) -> str:
    title = sanitize_windows_component(base)
    stem = suffix_template.format(title=title, **values)
    extension = extension.lstrip(".").lower()
    return f"{sanitize_windows_component(stem)}.{extension}"
