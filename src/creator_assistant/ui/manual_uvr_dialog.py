from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Callable

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QDialog, QGridLayout, QLabel, QMessageBox, QPushButton, QVBoxLayout


class ManualUvrDialog(QDialog):
    """Explicit manual hand-off. It never starts processing or waits in the background."""

    def __init__(
        self,
        source: Path,
        expected_output: Path,
        launcher: Path,
        validate_result: Callable[[Path], None],
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.source = source
        self.expected_output = expected_output
        self.launcher = launcher
        self.validate_result = validate_result
        self.started = time.time()
        self.setWindowTitle("Ручной режим Ultimate Vocal Remover")
        self.resize(820, 430)
        layout = QVBoxLayout(self)
        title = QLabel("Автоматическая интеграция UVR 5.6.1 недоступна")
        title.setObjectName("sectionTitle")
        layout.addWidget(title)
        explanation = QLabel(
            "Creator Assistant не запускал обработку и не использует сохранённые пути UVR. "
            "Укажите приведённые ниже Input и Output вручную, выберите MDX-Net / "
            "UVR-MDX-NET Inst HQ 3 / FLAC / Instrumental Only и нажмите Start Processing в UVR."
        )
        explanation.setWordWrap(True)
        layout.addWidget(explanation)
        grid = QGridLayout()
        values = (
            ("Входной файл", str(source)),
            ("Выходная папка", str(expected_output.parent)),
            ("Ожидаемый результат", str(expected_output)),
            ("Модель", "UVR-MDX-NET Inst HQ 3"),
            ("Формат", "FLAC"),
            ("Режим", "Instrumental Only"),
        )
        for row, (name, value) in enumerate(values):
            label = QLabel(name + ":")
            field = QLabel(value)
            field.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard)
            field.setWordWrap(True)
            field.setToolTip(value)
            grid.addWidget(label, row, 0)
            grid.addWidget(field, row, 1)
        layout.addLayout(grid)
        self.status = QLabel("Статус: MANUAL_ACTION_REQUIRED — обработка ещё не подтверждена.")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        buttons = QGridLayout()
        copy_input = QPushButton("Скопировать путь входного файла")
        copy_output = QPushButton("Скопировать выходную папку")
        open_uvr = QPushButton("Открыть UVR")
        check = QPushButton("Проверить результат")
        cancel = QPushButton("Отмена")
        copy_input.clicked.connect(lambda: QGuiApplication.clipboard().setText(str(source)))
        copy_output.clicked.connect(lambda: QGuiApplication.clipboard().setText(str(expected_output.parent)))
        open_uvr.clicked.connect(self._open_uvr)
        check.clicked.connect(self._check_result)
        cancel.clicked.connect(self.reject)
        for column, button in enumerate((copy_input, copy_output, open_uvr, check, cancel)):
            buttons.addWidget(button, 0, column)
        layout.addLayout(buttons)

    def _open_uvr(self) -> None:
        if not self.launcher.is_file():
            QMessageBox.warning(self, "UVR", "UVR Launcher не найден.")
            return
        os.startfile(str(self.launcher))
        self.status.setText("Статус: UVR открыт. Creator Assistant не считает это началом обработки.")

    def _check_result(self) -> None:
        candidate = self.expected_output if self.expected_output.is_file() else self._find_candidate()
        if not candidate:
            QMessageBox.warning(self, "UVR", "Instrumental FLAC в текущей папке проекта пока не найден.")
            return
        try:
            self.validate_result(candidate)
        except Exception as exc:
            QMessageBox.warning(self, "UVR", "Файл не прошёл FFprobe-проверку:\n" + str(exc))
            return
        if candidate != self.expected_output:
            if self.expected_output.exists():
                QMessageBox.warning(self, "UVR", "Ожидаемый файл уже существует; автоматическое переименование отменено.")
                return
            os.replace(str(candidate), str(self.expected_output))
        self.status.setText("Статус: COMPLETED — FLAC проверен.")
        QMessageBox.information(self, "UVR", "Instrumental FLAC найден и проверен. Проект можно продолжить.")
        self.accept()

    def _find_candidate(self):
        candidates = []
        for path in self.expected_output.parent.glob("*.flac"):
            try:
                if path.stat().st_size > 0 and path.stat().st_mtime >= self.started and "instrumental" in path.stem.casefold():
                    candidates.append(path)
            except OSError:
                continue
        return max(candidates, key=lambda path: path.stat().st_mtime) if candidates else None
