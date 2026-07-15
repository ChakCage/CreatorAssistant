from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QComboBox, QGridLayout, QHBoxLayout, QHeaderView, QLabel, QPushButton, QTableWidget,
    QTableWidgetItem, QTextEdit, QVBoxLayout, QWidget,
)

from creator_assistant.domain.shorts.models import Candidate


STATUS_LABELS = {"review": "Проверить", "approved": "Одобрен", "rejected": "Отклонён"}


class CandidateList(QWidget):
    selected = Signal(object)
    status_changed = Signal(object, str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.candidates: list[Candidate] = []
        layout = QVBoxLayout(self)
        controls = QHBoxLayout()
        self.filter = QComboBox()
        for label, value in (("Все", "all"), ("Лучшие", "best"), ("Одобренные", "approved"), ("Отклонённые", "rejected"), ("Требуют проверки", "review")):
            self.filter.addItem(label, value)
        self.sort = QComboBox()
        for label, value in (
            ("По итоговому score", "score"), ("По тайм-коду", "time"), ("По AI", "ai"),
            ("По эвристике", "heuristic"), ("По длительности", "duration"),
        ):
            self.sort.addItem(label, value)
        controls.addWidget(self.filter)
        controls.addWidget(self.sort)
        controls.addStretch(1)
        layout.addLayout(controls)
        self.table = QTableWidget(0, 12)
        self.table.setHorizontalHeaderLabels((
            "Место", "Start", "End", "Длина", "Эвристика", "AI", "Итог", "Источник",
            "Статус", "Кратко", "⚠", "Действия",
        ))
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setMouseTracking(True)
        self.table.setShowGrid(True)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(76)
        self.table.setStyleSheet(
            "QTableWidget { gridline-color: #334052; alternate-background-color: #121820; }"
            "QTableWidget::item { border-bottom: 1px solid #2e3846; padding: 4px; }"
            "QTableWidget::item:selected { background: #24513d; color: #ffffff; }"
            "QTableWidget::item:hover { background: #1c2631; }"
        )
        self.table.horizontalHeader().setSectionResizeMode(9, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(11, QHeaderView.ResizeToContents)
        self.table.cellDoubleClicked.connect(lambda row, _column: self._select_row(row))
        self.table.itemSelectionChanged.connect(self._selection_changed)
        self.filter.currentIndexChanged.connect(self.refresh)
        self.sort.currentIndexChanged.connect(self.refresh)
        layout.addWidget(self.table, 1)
        self.details_title = QLabel("Подробности кандидата")
        self.details_title.setObjectName("sectionTitle")
        self.details = QTextEdit()
        self.details.setReadOnly(True)
        self.details.setMinimumHeight(150)
        self.details.setPlaceholderText("Выберите кандидата, чтобы увидеть полную расшифровку, причины и оценки.")
        layout.addWidget(self.details_title)
        layout.addWidget(self.details)

    def set_candidates(self, candidates: list[Candidate]) -> None:
        self.candidates = candidates
        self.refresh()

    def refresh(self) -> None:
        mode = self.filter.currentData()
        values = list(self.candidates)
        if mode == "best":
            values = [item for item in values if item.score >= 70]
        elif mode != "all":
            values = [item for item in values if item.status == mode]
        sorting = self.sort.currentData()
        values.sort(key=self._sort_key(sorting))
        has_warnings = any(item.warnings for item in values)
        self.table.setRowCount(len(values))
        for row, candidate in enumerate(values):
            self.table.setRowHeight(row, 76)
            text = self._short_text(candidate.text, 105)
            reasons = self._short_text("; ".join(candidate.reasons[:2]), 120)
            ai_score = "—" if candidate.semantic_score is None else f"{candidate.semantic_score:.1f}"
            source = {
                "hybrid_ai": "Локальный AI",
                "heuristic_fallback": "Fallback",
                "heuristic": "Эвристика",
            }.get(candidate.selection_source, candidate.selection_source)
            details = text + (f"\n✓ {reasons}" if reasons else "")
            if candidate.ai_model or candidate.ai_mode:
                details += f"\nAI: {candidate.ai_model or '—'} · {candidate.ai_mode or '—'}"
            if candidate.ai_verdict:
                details += f"\n{self._short_text(candidate.ai_verdict, 90)}"
            cells = (
                str(row + 1), f"{candidate.start:.1f}", f"{candidate.end:.1f}",
                f"{candidate.duration:.1f} с", f"{candidate.heuristic_score or candidate.score:.1f}",
                ai_score, f"{candidate.final_score or candidate.score:.1f}", source,
                STATUS_LABELS.get(candidate.status, candidate.status), details,
                "⚠" if candidate.warnings else "",
            )
            for column, value in enumerate(cells):
                item = QTableWidgetItem(str(value))
                item.setData(256, candidate)
                item.setToolTip(self._tooltip_for(candidate, column))
                item.setTextAlignment(Qt.AlignCenter if column != 9 else (Qt.AlignLeft | Qt.AlignVCenter))
                if column == 0 and candidate.thumbnail and Path(candidate.thumbnail).is_file():
                    item.setIcon(QIcon(candidate.thumbnail))
                self.table.setItem(row, column, item)
            actions = QWidget()
            action_layout = QGridLayout(actions)
            action_layout.setContentsMargins(0, 0, 0, 0)
            action_layout.setSpacing(3)
            view = QPushButton("Просмотреть")
            alternatives = QPushButton("Альтернативы")
            alternatives.setEnabled(bool(candidate.alternatives))
            approve = QPushButton("✓")
            reject = QPushButton("✕")
            for button in (view, alternatives):
                button.setMaximumHeight(26)
            for button in (approve, reject):
                button.setMaximumWidth(38)
                button.setMaximumHeight(24)
            view.clicked.connect(lambda _checked=False, item=candidate: self.selected.emit(item))
            alternatives.clicked.connect(lambda _checked=False, item=candidate: self.selected.emit(item))
            approve.clicked.connect(lambda _checked=False, item=candidate: self.status_changed.emit(item, "approved"))
            reject.clicked.connect(lambda _checked=False, item=candidate: self.status_changed.emit(item, "rejected"))
            action_layout.addWidget(view, 0, 0, 1, 2)
            action_layout.addWidget(alternatives, 0, 2, 1, 2)
            action_layout.addWidget(approve, 1, 1)
            action_layout.addWidget(reject, 1, 2)
            self.table.setCellWidget(row, 11, actions)
        self.table.setColumnHidden(10, not has_warnings)
        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setSectionResizeMode(9, QHeaderView.Stretch)
        if values:
            self.table.selectRow(0)
        else:
            self.details.clear()

    def _sort_key(self, sorting: str):
        if sorting == "time":
            return lambda item: (item.start, -item.score)
        if sorting == "ai":
            return lambda item: (-(item.semantic_score if item.semantic_score is not None else -1), item.start)
        if sorting == "heuristic":
            return lambda item: (-(item.heuristic_score or item.score), item.start)
        if sorting == "duration":
            return lambda item: (-item.duration, item.start)
        return lambda item: (-(item.final_score or item.score), item.start)

    @staticmethod
    def _short_text(text: str, limit: int) -> str:
        value = " ".join(str(text or "").split())
        return value[:limit - 1] + "…" if len(value) > limit else value

    @staticmethod
    def _tooltip_for(candidate: Candidate, column: int) -> str:
        if column == 0:
            return candidate.id
        if column == 10:
            return "\n".join(candidate.warnings)
        return ""

    def _selection_changed(self) -> None:
        item = self.table.item(self.table.currentRow(), 0)
        if item:
            self._show_details(item.data(256))

    def _show_details(self, candidate: Candidate) -> None:
        warnings = "\n".join(f"• {item}" for item in candidate.warnings) or "—"
        reasons = "\n".join(f"• {item}" for item in candidate.reasons) or "—"
        weaknesses = "\n".join(f"• {item}" for item in candidate.ai_weaknesses) or "—"
        profile = {
            "gaming": "Игровой ролик",
            "education": "Обучающий",
            "talking": "Разговорный",
        }.get(candidate.ai_mode, candidate.ai_mode or "—")
        self.details.setPlainText(
            f"{candidate.id}\n"
            f"Начало: {candidate.start:.3f} · Конец: {candidate.end:.3f} · Длительность: {candidate.duration:.3f} с\n"
            f"Heuristic: {candidate.heuristic_score or candidate.score:.1f} · AI: {candidate.semantic_score if candidate.semantic_score is not None else '—'} · Итог: {candidate.final_score or candidate.score:.1f}\n"
            f"Тип момента: {candidate.ai_moment_type or '—'} · Модель: {candidate.ai_model or '—'} · Режим: {candidate.ai_mode or '—'}\n"
            f"Профиль анализа: {profile}\n\n"
            f"Transcript:\n{candidate.text}\n\n"
            f"Почему выбран:\n{reasons}\n\n"
            f"AI verdict:\n{candidate.ai_verdict or '—'}\n\n"
            f"Слабые стороны:\n{weaknesses}\n\n"
            f"Предупреждения:\n{warnings}"
        )

    def _select_row(self, row: int) -> None:
        item = self.table.item(row, 0)
        if item:
            self.selected.emit(item.data(256))
