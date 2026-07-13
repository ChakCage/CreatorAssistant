from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, QThread, Signal, Slot
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from creator_assistant.app import ServiceContainer
from creator_assistant.domain.shorts.models import SourceInfo
from creator_assistant.services.shorts.shorts_project_store import ShortsProjectPaths, ShortsProjectStore
from creator_assistant.services.shorts.source_service import ShortsSourceService
from creator_assistant.ui.shorts.analysis_progress_panel import AnalysisProgressPanel
from creator_assistant.ui.shorts.source_panel import SourcePanel
from creator_assistant.ui.widgets.error_dialog import ErrorDialog


class _ProbeWorker(QObject):
    finished = Signal(object)
    failed = Signal(str, str)

    def __init__(self, service: ShortsSourceService, path: Path) -> None:
        super().__init__()
        self.service = service
        self.path = path

    @Slot()
    def run(self) -> None:
        try:
            self.finished.emit(self.service.probe(self.path))
        except Exception as exc:
            self.failed.emit(str(exc), repr(exc))


class ShortsTab(QWidget):
    """Independent Shorts workspace; later stages plug into its inner tabs."""

    def __init__(self, container: ServiceContainer, parent=None) -> None:
        super().__init__(parent)
        self.container = container
        self.source_service = ShortsSourceService(container.runner, container.paths.get("ffprobe", ""))
        self.project_store = ShortsProjectStore()
        self.source: Optional[SourceInfo] = None
        self.paths: Optional[ShortsProjectPaths] = None
        self._pending_root: Optional[Path] = None
        self._thread: Optional[QThread] = None
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        header = QLabel("Shorts · локальный анализ и вертикальный рендер")
        header.setStyleSheet("font-size: 18px; font-weight: 600;")
        layout.addWidget(header)
        splitter = QSplitter()
        left = QWidget()
        left_layout = QVBoxLayout(left)
        self.source_panel = SourcePanel()
        self.progress_panel = AnalysisProgressPanel()
        self.start_button = QPushButton("Запустить анализ")
        self.start_button.setEnabled(False)
        left_layout.addWidget(self.source_panel)
        left_layout.addWidget(self.progress_panel)
        left_layout.addWidget(self.start_button)
        left_layout.addStretch(1)
        self.workspace = QTabWidget()
        self.workspace.addTab(self._placeholder("После анализа здесь появятся реальные кандидаты."), "Кандидаты")
        self.workspace.addTab(self._placeholder("Выберите кандидата для просмотра и правки границ."), "Редактор")
        self.workspace.addTab(self._placeholder("Субтитры создаются отдельно для каждого Short."), "Субтитры")
        self.workspace.addTab(self._placeholder("Одобренные фрагменты попадут в последовательную очередь."), "Рендер")
        splitter.addWidget(left)
        splitter.addWidget(self.workspace)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 3)
        layout.addWidget(splitter, 1)
        self.source_panel.choose_file_requested.connect(self.choose_file)
        self.source_panel.choose_project_requested.connect(self.choose_project)

    @staticmethod
    def _placeholder(text: str) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        label = QLabel(text)
        label.setWordWrap(True)
        layout.addWidget(label)
        layout.addStretch(1)
        return widget

    @Slot()
    def choose_file(self) -> None:
        selected, _ = QFileDialog.getOpenFileName(
            self, "Выберите готовое видео", "", "Видео (*.mp4 *.mkv *.mov *.m4v *.webm *.avi);;Все файлы (*)"
        )
        if not selected:
            return
        output = QFileDialog.getExistingDirectory(self, "Выберите папку, в которой создать проект Shorts", str(Path(selected).parent))
        if output:
            self._begin_probe(Path(selected), Path(output) / f"{Path(selected).stem} Shorts")

    @Slot()
    def choose_project(self) -> None:
        selected = QFileDialog.getExistingDirectory(self, "Выберите папку проекта Creator Assistant")
        if not selected:
            return
        folder = Path(selected)
        candidates = self.source_service.project_video_candidates(folder)
        if not candidates:
            QMessageBox.warning(self, "Shorts", "В папке проекта не найден подходящий финальный видеофайл.")
            return
        if len(candidates) > 1:
            picker = QFileDialog(self, "Выберите финальный рендер", str(folder))
            picker.setNameFilter("Видео (*.mp4 *.mkv *.mov *.m4v *.webm *.avi)")
            picker.setFileMode(QFileDialog.ExistingFile)
            if not picker.exec() or not picker.selectedFiles():
                return
            source = Path(picker.selectedFiles()[0])
        else:
            source = candidates[0]
        self._begin_probe(source, folder / "Shorts")

    def _begin_probe(self, source: Path, root: Path) -> None:
        if self._thread and self._thread.isRunning():
            return
        self._pending_root = root
        self.source_panel.set_busy(True)
        self.progress_panel.update_state("1. Проверка исходника", "FFprobe читает только метаданные; исходник не изменяется.", 2)
        thread = QThread(self)
        worker = _ProbeWorker(self.source_service, source)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(self._probe_finished)
        worker.failed.connect(self._probe_failed)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        worker.failed.connect(worker.deleteLater)
        thread.finished.connect(self._probe_thread_finished)
        self._thread = thread
        self._thread.worker = worker
        thread.start()

    @Slot(object)
    def _probe_finished(self, source: SourceInfo) -> None:
        assert self._pending_root is not None
        self.source = source
        self.paths = self.project_store.open_or_create(self._pending_root, source)
        source_json = self.paths.analysis / "source_info.json"
        source_json.write_text(json.dumps(source.__dict__, ensure_ascii=False, indent=2), encoding="utf-8")
        self.source_panel.show_source(source, self.paths.root)
        self.progress_panel.update_state("Источник готов", "Manifest и структура проекта сохранены атомарно.", 8)
        self.start_button.setEnabled(True)

    @Slot(str, str)
    def _probe_failed(self, message: str, details: str) -> None:
        self.progress_panel.update_state("Ошибка проверки", message, 0)
        ErrorDialog(message, details, self).exec()

    @Slot()
    def _probe_thread_finished(self) -> None:
        self.source_panel.set_busy(False)
        thread = self._thread
        self._thread = None
        if thread:
            thread.deleteLater()

    def shutdown_workers(self) -> bool:
        if not self._thread or not self._thread.isRunning():
            return True
        self.container.runner.cancel_active()
        self._thread.quit()
        return self._thread.wait(5000)
