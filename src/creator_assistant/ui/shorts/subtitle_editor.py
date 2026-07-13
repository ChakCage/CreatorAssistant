from __future__ import annotations

from copy import deepcopy

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFormLayout, QHBoxLayout, QHeaderView, QLabel,
    QPushButton, QSpinBox, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from creator_assistant.domain.shorts.models import SubtitleCue
from creator_assistant.services.shorts.subtitle_service import SubtitleService
from creator_assistant.ui.shorts.vertical_layout_panel import VerticalLayoutPanel


class SubtitleEditor(QWidget):
    saved = Signal(object)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.service = SubtitleService()
        self.candidate = self.transcript = self.paths = None
        self.original: list[SubtitleCue] = []
        layout = QVBoxLayout(self)
        self.heading = QLabel("Выберите кандидата")
        layout.addWidget(self.heading)
        top = QHBoxLayout()
        self.vertical = VerticalLayoutPanel()
        style_widget = QWidget()
        style_form = QFormLayout(style_widget)
        self.style = QComboBox()
        for label, value in (("Чистый", "clean"), ("Крупный", "large"), ("Игровой", "gaming")):
            self.style.addItem(label, value)
        self.position = QComboBox()
        for label, value in (("Верхняя треть", "upper"), ("Центр", "center"), ("Нижняя треть", "lower")):
            self.position.addItem(label, value)
        self.size = QSpinBox(); self.size.setRange(24, 120); self.size.setValue(58)
        self.maximum = QSpinBox(); self.maximum.setRange(12, 80); self.maximum.setValue(36)
        self.lines = QSpinBox(); self.lines.setRange(1, 3); self.lines.setValue(2)
        self.outline = QSpinBox(); self.outline.setRange(0, 10); self.outline.setValue(3)
        self.shadow = QSpinBox(); self.shadow.setRange(0, 10); self.shadow.setValue(1)
        self.background = QCheckBox("Полупрозрачный фон")
        self.margin = QSpinBox(); self.margin.setRange(40, 500); self.margin.setValue(160)
        for label, control in (("Стиль", self.style), ("Положение", self.position), ("Размер", self.size), ("Максимум символов", self.maximum), ("Строк", self.lines), ("Обводка", self.outline), ("Тень", self.shadow), ("Безопасный отступ", self.margin)):
            style_form.addRow(label, control)
        style_form.addRow(self.background)
        top.addWidget(self.vertical)
        top.addWidget(style_widget)
        layout.addLayout(top)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(("Start", "End", "Text"))
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        layout.addWidget(self.table, 1)
        actions = QHBoxLayout()
        for text, handler in (("Объединить", self._merge), ("Разделить", self._split), ("Удалить", self._delete), ("Восстановить исходный вариант", self._restore), ("Сохранить SRT и ASS", self._save)):
            button = QPushButton(text)
            button.clicked.connect(handler)
            actions.addWidget(button)
        actions.addStretch(1)
        layout.addLayout(actions)

    def set_context(self, candidate, transcript, paths) -> None:
        self.candidate, self.transcript, self.paths = candidate, transcript, paths
        self.heading.setText(f"{candidate.id} · отдельная копия субтитров (глобальный transcript не изменяется)")
        srt = paths.subtitles / f"{candidate.id}.srt"
        cues = self.service.parse_srt(srt) if srt.is_file() else self.service.generate(transcript, candidate, self.maximum.value(), self.lines.value())
        self.original = deepcopy(self.service.generate(transcript, candidate, self.maximum.value(), self.lines.value()))
        settings = candidate.subtitle_settings or {}
        self.style.setCurrentIndex(max(0, self.style.findData(settings.get("style", "clean"))))
        self.position.setCurrentIndex(max(0, self.position.findData(settings.get("position", "lower"))))
        self.size.setValue(int(settings.get("size", 58)))
        self.maximum.setValue(int(settings.get("maximum", 36)))
        self.lines.setValue(int(settings.get("lines", 2)))
        self.outline.setValue(int(settings.get("outline", 3)))
        self.shadow.setValue(int(settings.get("shadow", 1)))
        self.background.setChecked(bool(settings.get("background", False)))
        self.margin.setValue(int(settings.get("safe_margin", 160)))
        self.vertical.set_value(candidate.layout_settings or {})
        self._show(cues)

    def _show(self, cues: list[SubtitleCue]) -> None:
        self.table.setRowCount(len(cues))
        for row, cue in enumerate(cues):
            self.table.setItem(row, 0, QTableWidgetItem(f"{cue.start:.3f}"))
            self.table.setItem(row, 1, QTableWidgetItem(f"{cue.end:.3f}"))
            self.table.setItem(row, 2, QTableWidgetItem(cue.text))
        self.table.resizeRowsToContents()

    def _cues(self) -> list[SubtitleCue]:
        result = []
        for row in range(self.table.rowCount()):
            try:
                start = float(self.table.item(row, 0).text().replace(",", "."))
                end = float(self.table.item(row, 1).text().replace(",", "."))
            except (AttributeError, ValueError):
                continue
            text = self.table.item(row, 2).text() if self.table.item(row, 2) else ""
            if text.strip() and end > start >= 0:
                result.append(SubtitleCue(start, end, text.strip()))
        return result

    def _merge(self) -> None:
        rows = sorted({index.row() for index in self.table.selectedIndexes()})
        if len(rows) < 2 or rows[-1] - rows[0] + 1 != len(rows):
            return
        cues = self._cues()
        merged = SubtitleCue(cues[rows[0]].start, cues[rows[-1]].end, " ".join(cues[row].text.replace("\n", " ") for row in rows))
        self._show(cues[:rows[0]] + [merged] + cues[rows[-1] + 1:])

    def _split(self) -> None:
        row = self.table.currentRow()
        cues = self._cues()
        if row < 0 or row >= len(cues):
            return
        cue = cues[row]
        words = cue.text.replace("\n", " ").split()
        if len(words) < 2:
            return
        cut = len(words) // 2
        middle = (cue.start + cue.end) / 2
        replacement = [SubtitleCue(cue.start, middle, " ".join(words[:cut])), SubtitleCue(middle, cue.end, " ".join(words[cut:]))]
        self._show(cues[:row] + replacement + cues[row + 1:])

    def _delete(self) -> None:
        rows = {index.row() for index in self.table.selectedIndexes()}
        self._show([cue for index, cue in enumerate(self._cues()) if index not in rows])

    def _restore(self) -> None:
        self._show(deepcopy(self.original))

    def _save(self) -> None:
        if not self.candidate or not self.paths:
            return
        settings = {"style": self.style.currentData(), "position": self.position.currentData(), "size": self.size.value(), "maximum": self.maximum.value(), "lines": self.lines.value(), "outline": self.outline.value(), "shadow": self.shadow.value(), "background": self.background.isChecked(), "safe_margin": self.margin.value()}
        self.candidate.subtitle_settings = settings
        self.candidate.layout_settings = self.vertical.value()
        self.service.write(self._cues(), self.paths.subtitles / f"{self.candidate.id}.srt", self.paths.subtitles / f"{self.candidate.id}.ass", settings)
        self.saved.emit(self.candidate)
