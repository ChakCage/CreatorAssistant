from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
)


class SourcePanel(QGroupBox):
    choose_file_requested = Signal()
    choose_project_requested = Signal()
    choose_output_requested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__("A. Источник", parent)
        layout = QVBoxLayout(self)
        buttons = QHBoxLayout()
        self.file_button = QPushButton("Выбрать готовое видео…")
        self.project_button = QPushButton("Выбрать папку проекта…")
        self.output_button = QPushButton("Выбрать папку Shorts…")
        self.file_button.clicked.connect(self.choose_file_requested)
        self.project_button.clicked.connect(self.choose_project_requested)
        self.output_button.clicked.connect(self.choose_output_requested)
        buttons.addWidget(self.file_button)
        buttons.addWidget(self.project_button)
        buttons.addWidget(self.output_button)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        form = QFormLayout()
        self.path_edit = QLineEdit()
        self.path_edit.setReadOnly(True)
        self.path_edit.setPlaceholderText("Исходное видео не выбрано")
        self.output_edit = QLineEdit()
        self.output_edit.setReadOnly(True)
        self.output_button.setEnabled(False)
        form.addRow("Видео", self.path_edit)
        form.addRow("Проект Shorts", self.output_edit)
        layout.addLayout(form)
        self.info = QLabel("FFprobe ещё не запускался.")
        self.info.setWordWrap(True)
        layout.addWidget(self.info)
        self.analysis_info = QLabel("Проект ещё не анализировался локальной AI-моделью.")
        self.analysis_info.setWordWrap(True)
        self.analysis_info.setProperty("class", "muted")
        layout.addWidget(self.analysis_info)

    def set_busy(self, busy: bool) -> None:
        self.file_button.setEnabled(not busy)
        self.project_button.setEnabled(not busy)
        self.output_button.setEnabled(not busy and bool(self.path_edit.text()))
        if busy:
            self.info.setText("Проверяю видео через FFprobe…")

    def show_source(self, source, project_root: Path) -> None:
        self.path_edit.setText(source.path)
        self.output_edit.setText(str(project_root))
        self.output_button.setEnabled(True)
        self.info.setText(
            f"{source.width}×{source.height} · {source.fps:.3f} FPS · "
            f"{source.duration / 60:.1f} мин · {source.video_codec} / {source.audio_codec} · "
            f"{source.dynamic_range} · rotation {source.rotation}°\n"
            f"Fingerprint: {source.fingerprint[:16]}…"
        )

    def show_analysis_provenance(self, ai_analysis: dict) -> None:
        model = str(ai_analysis.get("model_id") or ai_analysis.get("model") or "")
        if not model:
            self.analysis_info.setText("Проект ещё не анализировался локальной AI-моделью.")
            return
        profile = str(ai_analysis.get("model_profile") or "—")
        digest = str(ai_analysis.get("model_digest") or "—")
        date = str(ai_analysis.get("analysis_date") or ai_analysis.get("timestamp") or "—")
        context = ai_analysis.get("context") if isinstance(ai_analysis.get("context"), dict) else {}
        execution = ai_analysis.get("execution") if isinstance(ai_analysis.get("execution"), dict) else {}
        details = execution.get("details") if isinstance(execution.get("details"), dict) else {}
        processor = execution.get("processor") or details.get("family") or ""
        self.analysis_info.setText(
            f"AI-анализ: {model} · профиль {profile} · context {context.get('length', '—')} · "
            f"CPU/GPU {processor or 'не сообщено'}\nDigest: {digest} · {date}"
        )
