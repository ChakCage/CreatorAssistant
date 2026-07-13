from pathlib import Path

from creator_assistant.infrastructure.windows_paths import (
    discover_author_folders,
    safe_file_name,
    sanitize_windows_component,
    unique_directory_path,
)


def test_sanitize_invalid_windows_characters():
    assert sanitize_windows_component('A<B>:"C/\\D|?*') == "A-B---C--D---"


def test_sanitize_unicode_and_cyrillic_are_preserved():
    assert sanitize_windows_component("  Привет  мир 🎬  ") == "Привет мир 🎬"


def test_reserved_names_and_trailing_characters():
    assert sanitize_windows_component("CON. ") == "CON_"
    assert sanitize_windows_component("video...   ") == "video"


def test_unique_path_uses_2_and_3(tmp_path: Path):
    (tmp_path / "Видео").mkdir()
    assert unique_directory_path(tmp_path, "Видео").name == "Видео (2)"
    (tmp_path / "Видео (2)").mkdir()
    assert unique_directory_path(tmp_path, "Видео").name == "Видео (3)"


def test_discover_all_author_folders(tmp_path: Path):
    (tmp_path / "Beppo" / "Делаю").mkdir(parents=True)
    (tmp_path / "Чак" / "Делаю").mkdir(parents=True)
    (tmp_path / "Не автор").mkdir()
    found = discover_author_folders(tmp_path)
    assert [(author, path.name) for author, path in found] == [("Beppo", "Делаю"), ("Чак", "Делаю")]


def test_configurable_file_name():
    assert safe_file_name("Заголовок", "{title} [MAX {height}p]", "mkv", height=2160) == "Заголовок [MAX 2160p].mkv"
