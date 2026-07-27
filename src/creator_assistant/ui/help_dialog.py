from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLineEdit, QListWidget, QTextBrowser, QVBoxLayout


HELP_PAGES = (
    ("Быстрый старт", "beta-quick-start-ru.md"),
    ("Системные требования и Ollama", "beta-user-guide-ru.md"),
    ("Создание проекта и Shorts", "README_RU.md"),
    ("Обновление программы", "updating-ru.md"),
    ("Лицензия и устройства", "commercial-license-e2e-ru.md"),
    ("Как отправить отчёт поддержке", "support-and-privacy-ru.md"),
    ("Частые ошибки", "beta-known-issues-ru.md"),
    ("Политика конфиденциальности", "privacy-policy-ru.md"),
    ("Пользовательское соглашение", "eula-ru.md"),
)


class HelpDialog(QDialog):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Справка Creator Assistant")
        self.resize(980, 680)
        layout = QVBoxLayout(self)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Поиск по встроенной справке…")
        layout.addWidget(self.search)
        row = QHBoxLayout()
        self.pages = QListWidget()
        self.browser = QTextBrowser()
        self.browser.setOpenExternalLinks(True)
        row.addWidget(self.pages, 0)
        row.addWidget(self.browser, 1)
        layout.addLayout(row, 1)
        self._documents: list[tuple[str, str]] = []
        for title, filename in HELP_PAGES:
            self._documents.append((title, _read_doc(filename)))
            self.pages.addItem(title)
        self.pages.currentRowChanged.connect(self._show)
        self.search.textChanged.connect(self._filter)
        self.pages.setCurrentRow(0)

    def _show(self, row: int) -> None:
        if 0 <= row < len(self._documents):
            title, text = self._documents[row]
            self.browser.setMarkdown(f"# {title}\n\n{text}")

    def _filter(self, query: str) -> None:
        needle = query.strip().casefold()
        for index, (title, text) in enumerate(self._documents):
            self.pages.item(index).setHidden(bool(needle and needle not in (title + "\n" + text).casefold()))


def _read_doc(filename: str) -> str:
    bundle = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[3]))
    candidates = [
        bundle / "docs" / filename,
        Path(__file__).resolve().parents[3] / "docs" / filename,
        Path(__file__).resolve().parents[3] / filename,
    ]
    for path in candidates:
        if path.is_file():
            return path.read_text(encoding="utf-8-sig", errors="replace")
    return "Страница справки отсутствует в этой сборке."
