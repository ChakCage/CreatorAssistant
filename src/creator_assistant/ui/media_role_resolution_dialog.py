from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt, QUrl
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSlider,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from creator_assistant.domain.progress import format_bytes, format_duration


class MediaRoleResolutionDialog(QDialog):
    """Normal preflight choice for ambiguous legacy audio; never mutates media files."""

    USE_EXISTING_FILE = "USE_EXISTING_FILE"
    DOWNLOAD_NEW = "DOWNLOAD_NEW"
    SKIP = "SKIP"
    CANCEL = "CANCEL"

    def __init__(
        self,
        *,
        title: str,
        duration: Optional[float],
        candidates: List[Dict[str, Any]],
        project_path: Path,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.candidates = list(candidates)
        self.project_path = project_path
        self.resolution_action = self.CANCEL
        self.selected_path: Optional[Path] = None
        self.setWindowTitle("Выберите оригинальную аудиодорожку")
        self.resize(1080, 560)

        layout = QVBoxLayout(self)
        if len(self.candidates) == 1:
            choice_text = (
                "Найден один возможный файл оригинальной аудиодорожки. "
                "Подтвердите его или выберите другое действие."
            )
        elif len(self.candidates) > 1:
            choice_text = (
                "Найдено несколько возможных файлов оригинальной аудиодорожки. "
                "Выберите нужный."
            )
        else:
            choice_text = "Оригинальная аудиодорожка не найдена. Выберите файл вручную или другое действие."
        self.intro_label = QLabel(
            f"Исходное видео: {title}\n"
            f"Длительность YouTube: {format_duration(duration or 0)}\n"
            f"Найдено кандидатов: {len(self.candidates)}\n\n"
            + choice_text
        )
        self.intro_label.setWordWrap(True)
        layout.addWidget(self.intro_label)

        self.table = QTableWidget(len(self.candidates), 8)
        self.table.setHorizontalHeaderLabels(
            ["Имя", "Папка", "Длительность", "Формат / codec", "Частота", "Каналы", "Размер", "Причина"]
        )
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        for row, candidate in enumerate(self.candidates):
            path = Path(str(candidate.get("path") or ""))
            values = (
                path.name,
                str(path.parent),
                format_duration(float(candidate.get("duration") or 0)),
                str(candidate.get("audio_codec") or candidate.get("container") or path.suffix.lstrip(".")),
                (str(candidate.get("sample_rate")) + " Hz") if candidate.get("sample_rate") else "—",
                str(candidate.get("channels") or "—"),
                format_bytes(int(candidate.get("size") or 0)),
                str(candidate.get("reason") or "Совпадает длительность"),
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(str(path) if column in {0, 1} else value)
                self.table.setItem(row, column, item)
        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setStretchLastSection(True)
        if self.candidates:
            self.table.selectRow(0)
        layout.addWidget(self.table, 1)

        self.audio_output = QAudioOutput(self)
        self.player = QMediaPlayer(self)
        self.player.setAudioOutput(self.audio_output)
        self.position = QSlider(Qt.Horizontal)
        self.position.setRange(0, 0)
        self.position.sliderMoved.connect(self.player.setPosition)
        self.player.positionChanged.connect(self.position.setValue)
        self.player.durationChanged.connect(lambda value: self.position.setRange(0, max(0, value)))

        preview = QHBoxLayout()
        play = QPushButton("Прослушать")
        stop = QPushButton("Остановить")
        location = QPushButton("Открыть расположение")
        play.clicked.connect(self._play_selected)
        stop.clicked.connect(self.player.stop)
        location.clicked.connect(self._open_selected_location)
        preview.addWidget(play)
        preview.addWidget(stop)
        preview.addWidget(location)
        preview.addWidget(self.position, 1)
        layout.addLayout(preview)

        actions = QHBoxLayout()
        self.use_button = QPushButton("Использовать выбранный файл")
        choose = QPushButton("Выбрать другой файл")
        skip = QPushButton("Оригинальное аудио мне не нужно")
        download = QPushButton("Скачать новое оригинальное аудио")
        open_project = QPushButton("Открыть папку проекта")
        cancel = QPushButton("Отмена")
        self.use_button.clicked.connect(self._use_selected)
        choose.clicked.connect(self._choose_file)
        skip.clicked.connect(lambda: self._finish(self.SKIP))
        download.clicked.connect(lambda: self._finish(self.DOWNLOAD_NEW))
        open_project.clicked.connect(lambda: os.startfile(str(self.project_path)))
        cancel.clicked.connect(self.reject)
        self.table.itemSelectionChanged.connect(self._sync_use_button)
        self._sync_use_button()
        for button in (self.use_button, choose, skip, download, open_project, cancel):
            actions.addWidget(button)
        layout.addLayout(actions)

    def _current_path(self) -> Optional[Path]:
        row = self.table.currentRow()
        if row < 0 or row >= len(self.candidates):
            return None
        path = Path(str(self.candidates[row].get("path") or ""))
        return path if path.is_file() else None

    def _play_selected(self) -> None:
        path = self._current_path()
        if not path:
            QMessageBox.warning(self, "Прослушивание", "Выберите существующий аудиофайл.")
            return
        self.player.stop()
        self.player.setSource(QUrl.fromLocalFile(str(path)))
        self.player.play()

    def _open_selected_location(self) -> None:
        path = self._current_path()
        if path:
            os.startfile(str(path.parent))

    def _use_selected(self) -> None:
        path = self._current_path()
        if not path:
            QMessageBox.warning(self, "Выбор аудио", "Выберите существующий файл.")
            return
        self.selected_path = path
        self._finish(self.USE_EXISTING_FILE)

    def _choose_file(self) -> None:
        selected, _filter = QFileDialog.getOpenFileName(
            self,
            "Выберите оригинальную аудиодорожку",
            str(self.project_path / "Материалы"),
            "Аудиофайлы (*.mp3 *.m4a *.flac *.wav *.ogg *.opus *.webm);;Все файлы (*)",
        )
        if not selected:
            return
        path = Path(selected)
        if not path.is_file():
            QMessageBox.warning(self, "Выбор аудио", "Выбранный файл не существует.")
            return
        self.selected_path = path
        self._finish(self.USE_EXISTING_FILE)

    def _sync_use_button(self) -> None:
        self.use_button.setEnabled(self._current_path() is not None)

    def _finish(self, action: str) -> None:
        self.resolution_action = action
        self.accept()

    def reject(self) -> None:
        self.player.stop()
        self.resolution_action = self.CANCEL
        super().reject()

    def closeEvent(self, event) -> None:
        self.player.stop()
        super().closeEvent(event)
