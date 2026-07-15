from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from PySide6.QtCore import QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPixmap
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFormLayout, QHBoxLayout, QHeaderView, QLabel,
    QPushButton, QSlider, QSpinBox, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from creator_assistant.domain.shorts.models import SubtitleCue
from creator_assistant.services.shorts.subtitle_layout import SubtitleLayoutCalculator
from creator_assistant.services.shorts.subtitle_service import (
    STYLE_PRESETS, SubtitleService, resolved_style,
)
from creator_assistant.ui.shorts.vertical_layout_panel import VerticalLayoutPanel


class VerticalFramePreview(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setMinimumSize(216, 384)
        self.setMaximumWidth(320)
        self.candidate = None
        self.settings: dict = {}
        self.layout_settings: dict = {}
        self.sample = "Длинный пример субтитров безопасно помещается в кадре"

    def set_preview(self, candidate, settings: dict, layout_settings: dict, sample: str = "") -> None:
        self.candidate = candidate
        self.settings = dict(settings)
        self.layout_settings = dict(layout_settings)
        if sample.strip():
            self.sample = sample.replace("\n", " ")
        self._last_mode = self.layout_settings.get("mode", "center_crop")
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802 - Qt API
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.fillRect(self.rect(), QColor("#0b0f14"))
        scale = min(self.width() / 1080, self.height() / 1920)
        left = (self.width() - 1080 * scale) / 2
        top = (self.height() - 1920 * scale) / 2
        painter.save()
        painter.translate(left, top)
        painter.scale(scale, scale)
        frame = QRect(0, 0, 1080, 1920)
        pixmap = QPixmap()
        if self.candidate and getattr(self.candidate, "thumbnail", ""):
            pixmap = QPixmap(str(Path(self.candidate.thumbnail)))
        if pixmap.isNull():
            painter.fillRect(frame, QColor("#182330"))
            painter.setPen(QColor("#60758a"))
            painter.drawText(frame, Qt.AlignCenter, "PREVIEW 1080×1920")
        elif self.layout_settings.get("mode", "center_crop") in {"blur_background", "solid_color"}:
            mode = self.layout_settings.get("mode")
            if mode == "blur_background":
                expanded = pixmap.scaled(270, 480, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
                crop_x = round(max(0, expanded.width() - 270) / 2)
                crop_y = round(max(0, expanded.height() - 480) / 2)
                background = expanded.copy(crop_x, crop_y, 270, 480)
                painter.drawPixmap(frame, background, background.rect())
            else:
                painter.fillRect(frame, QColor(str(self.layout_settings.get("background_color", "black"))))
            factor = int(self.layout_settings.get("foreground_scale", 100)) / 100
            foreground = pixmap.scaled(
                round(1080 * factor), round(1920 * factor),
                Qt.KeepAspectRatio, Qt.SmoothTransformation,
            )
            painter.drawPixmap(
                round((1080 - foreground.width()) / 2),
                round((1920 - foreground.height()) / 2),
                foreground,
            )
        else:
            center = int(self.layout_settings.get("crop_center", 50)) / 100
            scaled = pixmap.scaled(1080, 1920, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
            x = round(max(0, scaled.width() - 1080) * center)
            y = round(max(0, scaled.height() - 1920) / 2)
            painter.drawPixmap(frame, scaled, QRect(x, y, 1080, 1920))
        painter.restore()

        layout = SubtitleLayoutCalculator().calculate(self.sample, self.settings)
        font = QFont(layout.font_name)
        font.setPixelSize(max(8, round(layout.font_size * scale)))
        font.setWeight(QFont.Weight.Bold)
        painter.setFont(font)
        box = QRectF(
            left + (layout.x - layout.width / 2) * scale,
            top + (layout.y - layout.height / 2) * scale,
            layout.width * scale,
            layout.height * scale,
        )
        flags = Qt.AlignCenter | Qt.TextWordWrap
        if layout.background:
            painter.fillRect(box, QColor(0, 0, 0, 130))
        if layout.shadow:
            offset = max(1, round(layout.shadow * 2 * scale))
            painter.setPen(QColor(0, 0, 0, 170))
            painter.drawText(box.translated(offset, offset), flags, layout.text)
        radius = max(1, round(layout.outline * scale))
        painter.setPen(QColor("#000000"))
        for dx, dy in ((-radius, 0), (radius, 0), (0, -radius), (0, radius), (-radius, -radius), (-radius, radius), (radius, -radius), (radius, radius)):
            painter.drawText(box.translated(dx, dy), flags, layout.text)
        color = QColor("#ffd700") if str(self.settings.get("style", "clean")) == "gaming" else QColor("#ffffff")
        painter.setPen(color)
        painter.drawText(box, flags, layout.text)
        painter.setPen(QColor(255, 255, 255, 70))
        painter.setBrush(Qt.NoBrush)
        painter.drawRect(QRectF(left + 90 * scale, top + 120 * scale, 900 * scale, 1560 * scale))
        painter.end()


class SubtitleEditor(QWidget):
    saved = Signal(object)
    configuration_changed = Signal(object)
    test_render_requested = Signal(object)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.service = SubtitleService()
        self.candidate = self.transcript = self.paths = None
        self.original: list[SubtitleCue] = []
        self._loading = False
        self._dirty = False
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(250)
        self._save_timer.timeout.connect(self._apply_configuration)
        layout = QVBoxLayout(self)
        heading_row = QHBoxLayout()
        self.heading = QLabel("Выберите кандидата")
        self.dirty_label = QLabel("Сохранено")
        self.dirty_label.setStyleSheet("color: #7fba7a;")
        heading_row.addWidget(self.heading, 1)
        heading_row.addWidget(self.dirty_label)
        layout.addLayout(heading_row)
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
        self.size = QSpinBox(); self.size.setRange(44, 120); self.size.setValue(58)
        self.maximum = QSpinBox(); self.maximum.setRange(12, 80); self.maximum.setValue(36)
        self.maximum.setToolTip("Предварительный ориентир переноса. Финальная ширина рассчитывается по фактическому размеру текста в пикселях")
        self.lines = QSpinBox(); self.lines.setRange(1, 2); self.lines.setValue(2)
        self.outline = QSpinBox(); self.outline.setRange(0, 10); self.outline.setValue(3)
        self.shadow = QSpinBox(); self.shadow.setRange(0, 10); self.shadow.setValue(1)
        self.background = QCheckBox("Полупрозрачный фон")
        self.margin = QSpinBox(); self.margin.setRange(80, 500); self.margin.setValue(120)
        self.offset = QSlider(Qt.Horizontal); self.offset.setRange(-300, 300); self.offset.setSingleStep(10); self.offset.setPageStep(10); self.offset.setValue(0)
        self.offset_value = QSpinBox(); self.offset_value.setRange(-300, 300); self.offset_value.setSingleStep(10); self.offset_value.setSuffix(" px")
        self.offset_reset = QPushButton("Сбросить")
        self.offset.valueChanged.connect(self.offset_value.setValue)
        self.offset_value.valueChanged.connect(self.offset.setValue)
        self.offset_reset.clicked.connect(lambda: self.offset.setValue(0))
        offset_row = QWidget()
        offset_layout = QHBoxLayout(offset_row)
        offset_layout.setContentsMargins(0, 0, 0, 0)
        offset_layout.addWidget(self.offset, 1)
        offset_layout.addWidget(self.offset_value)
        offset_layout.addWidget(self.offset_reset)
        for label, control in (("Стиль", self.style), ("Положение", self.position), ("Размер", self.size), ("Примерная длина строки", self.maximum), ("Строк", self.lines), ("Обводка", self.outline), ("Тень", self.shadow), ("Безопасный отступ", self.margin)):
            style_form.addRow(label, control)
        style_form.addRow("Смещение по вертикали", offset_row)
        style_form.addRow(self.background)
        self.preview = VerticalFramePreview()
        top.addWidget(self.vertical)
        top.addWidget(style_widget)
        top.addWidget(self.preview)
        layout.addLayout(top)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(("Start", "End", "Text"))
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        layout.addWidget(self.table, 1)
        actions = QHBoxLayout()
        for text, handler in (("Объединить", self._merge), ("Разделить", self._split), ("Удалить", self._delete), ("Восстановить исходный вариант", self._restore), ("Сохранить SRT и ASS", self._export), ("Рендер тестовых 5 секунд", self._test_render)):
            button = QPushButton(text)
            button.clicked.connect(handler)
            actions.addWidget(button)
        actions.addStretch(1)
        layout.addLayout(actions)

        self.style.currentIndexChanged.connect(self._style_selected)
        for control in (self.position, self.size, self.maximum, self.lines, self.outline, self.shadow, self.margin, self.offset):
            signal = control.currentIndexChanged if isinstance(control, QComboBox) else control.valueChanged
            signal.connect(self._mark_dirty)
        self.background.toggled.connect(self._mark_dirty)
        self.vertical.changed.connect(self._mark_dirty)
        self.table.itemChanged.connect(self._mark_dirty)

    def set_context(self, candidate, transcript, paths) -> None:
        if self._dirty:
            self._apply_configuration()
        self._loading = True
        self.candidate, self.transcript, self.paths = candidate, transcript, paths
        self.heading.setText(f"{candidate.id} · настройки сохраняются автоматически")
        settings = candidate.subtitle_settings or {}
        stored_cues = settings.get("cues")
        if stored_cues:
            cues = [SubtitleCue(float(item["start"]), float(item["end"]), str(item["text"])) for item in stored_cues]
        else:
            srt = paths.subtitles / f"{candidate.id}.srt"
            cues = self.service.parse_srt(srt) if srt.is_file() else self.service.generate(transcript, candidate, self.maximum.value(), self.lines.value())
        self.original = deepcopy(self.service.generate(transcript, candidate, self.maximum.value(), self.lines.value()))
        self.style.setCurrentIndex(max(0, self.style.findData(settings.get("style", "clean"))))
        self.position.setCurrentIndex(max(0, self.position.findData(settings.get("position", "lower"))))
        preset = STYLE_PRESETS.get(str(settings.get("style", "clean")), STYLE_PRESETS["clean"])
        self.size.setValue(int(settings.get("size", preset["size"])))
        self.maximum.setValue(int(settings.get("maximum", 36)))
        self.lines.setValue(min(2, int(settings.get("lines", 2))))
        self.outline.setValue(int(settings.get("outline", preset["outline"])))
        self.shadow.setValue(int(settings.get("shadow", preset["shadow"])))
        self.background.setChecked(bool(settings.get("background", False)))
        self.margin.setValue(int(settings.get("safe_margin", 120)))
        self.offset.setValue(int(settings.get("vertical_offset", 0)))
        self.vertical.set_value(candidate.layout_settings or {})
        self._show(cues)
        self._loading = False
        self._dirty = False
        self._update_preview()
        self._set_saved_state()

    def rebuild_for_boundaries(self, candidate, transcript) -> list[SubtitleCue]:
        """Rebuild only this candidate's local cues after its start/end changes."""
        if self.candidate is not candidate:
            return []
        self._save_timer.stop()
        settings = self.current_settings()
        cues = self.service.generate(
            transcript,
            candidate,
            int(settings.get("maximum", 36)),
            min(2, int(settings.get("lines", 2))),
        )
        self.transcript = transcript
        self.original = deepcopy(cues)
        self._show(cues)
        settings["cues"] = [
            {"start": cue.start, "end": cue.end, "text": cue.text}
            for cue in cues
        ]
        candidate.subtitle_settings = settings
        candidate.layout_settings = self.vertical.value()
        self._dirty = False
        self._update_preview()
        self._set_saved_state()
        return cues

    def _show(self, cues: list[SubtitleCue]) -> None:
        self.table.blockSignals(True)
        self.table.setRowCount(len(cues))
        for row, cue in enumerate(cues):
            self.table.setItem(row, 0, QTableWidgetItem(f"{cue.start:.3f}"))
            self.table.setItem(row, 1, QTableWidgetItem(f"{cue.end:.3f}"))
            self.table.setItem(row, 2, QTableWidgetItem(cue.text))
        self.table.blockSignals(False)
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
            clip_duration = self.candidate.duration if self.candidate else end
            end = min(end, clip_duration)
            if text.strip() and end > start >= 0 and start < clip_duration:
                result.append(SubtitleCue(start, end, text.strip()))
        return result

    def current_settings(self) -> dict:
        return {
            "style": self.style.currentData(), "position": self.position.currentData(),
            "size": self.size.value(), "maximum": self.maximum.value(), "lines": self.lines.value(),
            "outline": self.outline.value(), "shadow": self.shadow.value(),
            "background": self.background.isChecked(), "safe_margin": self.margin.value(),
            "vertical_offset": self.offset.value(),
            "minimum_size": 44,
            "cues": [{"start": cue.start, "end": cue.end, "text": cue.text} for cue in self._cues()],
        }

    def _style_selected(self) -> None:
        if self._loading:
            return
        preset = STYLE_PRESETS.get(str(self.style.currentData()), STYLE_PRESETS["clean"])
        self.size.setValue(preset["size"])
        self.outline.setValue(preset["outline"])
        self.shadow.setValue(preset["shadow"])
        self.maximum.setValue({"clean": 36, "large": 24, "gaming": 28}.get(str(self.style.currentData()), 36))
        self._mark_dirty()

    def _mark_dirty(self, *_args) -> None:
        if self._loading or not self.candidate:
            return
        self._clamp_offset_to_safe_area()
        self._dirty = True
        self.dirty_label.setText("Изменено · автосохранение…")
        self.dirty_label.setStyleSheet("color: #e6b450;")
        self._update_preview()
        self._save_timer.start()

    def _clamp_offset_to_safe_area(self) -> None:
        sample = self._cues()[0].text if self._cues() else "Пример безопасных субтитров"
        layout = SubtitleLayoutCalculator().calculate(sample, self.current_settings())
        if layout.clamped_vertical_offset != self.offset.value():
            self.offset.blockSignals(True)
            self.offset_value.blockSignals(True)
            self.offset.setValue(layout.clamped_vertical_offset)
            self.offset_value.setValue(layout.clamped_vertical_offset)
            self.offset.blockSignals(False)
            self.offset_value.blockSignals(False)
            self.dirty_label.setText("Достигнут безопасный предел позиции")

    def _apply_configuration(self) -> None:
        if not self.candidate:
            return
        self.candidate.subtitle_settings = self.current_settings()
        self.candidate.layout_settings = self.vertical.value()
        self._dirty = False
        self.configuration_changed.emit(self.candidate)

    def mark_saved(self) -> None:
        self._dirty = False
        self._set_saved_state()

    def _set_saved_state(self) -> None:
        self.dirty_label.setText("Сохранено автоматически")
        self.dirty_label.setStyleSheet("color: #7fba7a;")

    def _update_preview(self) -> None:
        sample = self._cues()[0].text if self._cues() else "Пример безопасных субтитров"
        self.preview.set_preview(self.candidate, self.current_settings(), self.vertical.value(), sample)

    def _merge(self) -> None:
        rows = sorted({index.row() for index in self.table.selectedIndexes()})
        cues = self._cues()
        if len(rows) < 2 or rows[-1] - rows[0] + 1 != len(rows) or rows[-1] >= len(cues):
            return
        merged = SubtitleCue(cues[rows[0]].start, cues[rows[-1]].end, " ".join(cues[row].text.replace("\n", " ") for row in rows))
        self._show(cues[:rows[0]] + [merged] + cues[rows[-1] + 1:])
        self._mark_dirty()

    def _split(self) -> None:
        row = self.table.currentRow()
        cues = self._cues()
        if row < 0 or row >= len(cues):
            return
        cue = cues[row]
        words = cue.text.replace("\n", " ").split()
        if len(words) < 2:
            return
        cut, middle = len(words) // 2, (cue.start + cue.end) / 2
        replacement = [SubtitleCue(cue.start, middle, " ".join(words[:cut])), SubtitleCue(middle, cue.end, " ".join(words[cut:]))]
        self._show(cues[:row] + replacement + cues[row + 1:])
        self._mark_dirty()

    def _delete(self) -> None:
        rows = {index.row() for index in self.table.selectedIndexes()}
        self._show([cue for index, cue in enumerate(self._cues()) if index not in rows])
        self._mark_dirty()

    def _restore(self) -> None:
        self._show(deepcopy(self.original))
        self._mark_dirty()

    def _export(self) -> None:
        if not self.candidate or not self.paths:
            return
        self._apply_configuration()
        self.service.write(self._cues(), self.paths.subtitles / f"{self.candidate.id}.srt", self.paths.subtitles / f"{self.candidate.id}.ass", self.current_settings())
        self.saved.emit(self.candidate)

    def _test_render(self) -> None:
        if self.candidate:
            self._apply_configuration()
            self.test_render_requested.emit(self.candidate)
