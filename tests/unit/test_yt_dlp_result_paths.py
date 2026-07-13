from pathlib import Path

import pytest

from creator_assistant.domain.job import CancellationToken
from creator_assistant.infrastructure.process_runner import ProcessResult
from creator_assistant.services.yt_dlp_service import YtDlpService


class ResultFileRunner:
    def __init__(self, result: Path, write_result: bool = True):
        self.result = result
        self.write_result = write_result

    def run(self, command, **kwargs):
        if self.write_result:
            index = command.index("--print-to-file")
            result_file = Path(command[index + 2])
            result_file.parent.mkdir(parents=True, exist_ok=True)
            result_file.write_text(str(self.result) + "\n", encoding="utf-8")
        return ProcessResult(list(command), 0, "")


@pytest.mark.parametrize(
    "relative",
    [
        Path("MylesMC/Делаю/Название/Материалы/Minecraft's Most Dead Server.mkv"),
        Path("Казак/Делаю/Тестовый ролик/Материалы/аудио.flac"),
        Path("Папка с пробелами/Видео автора/Материалы/файл.mkv"),
        Path("Автор 😀/Делаю/Кириллица's Unicode/Материалы/ролик 🎬.mkv"),
    ],
)
def test_utf8_result_file_preserves_real_unicode_path(tmp_path: Path, relative: Path):
    target = tmp_path / relative
    target.parent.mkdir(parents=True)
    target.write_bytes(b"media")
    template = target.with_name(target.stem + ".%(ext)s")
    service = YtDlpService(ResultFileRunner(target), "yt-dlp.exe", result_root=tmp_path / "results")
    found = service.download("https://youtu.be/abcdefghijk", "format", template, CancellationToken(), "Видео")
    assert found == target
    assert found.exists()


def test_damaged_console_marker_is_ignored_and_file_is_found_by_scan(tmp_path: Path):
    target = tmp_path / "Материалы" / "Название [MAX 1440p].mkv"
    target.parent.mkdir()
    target.write_bytes(b"media")
    template = target.with_name("Название [MAX 1440p].%(ext)s")
    runner = ResultFileRunner(target, write_result=False)
    service = YtDlpService(runner, "yt-dlp.exe", result_root=tmp_path / "results")
    assert service.download("https://youtu.be/abcdefghijk", "v+a", template, CancellationToken(), "Видео") == target


def test_partial_files_are_not_mistaken_for_completed_result(tmp_path: Path):
    template = tmp_path / "Видео.%(ext)s"
    (tmp_path / "Видео.mkv.part").write_bytes(b"partial")
    assert YtDlpService.find_created_file(template) is None
