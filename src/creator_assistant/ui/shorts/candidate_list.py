from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QComboBox, QHBoxLayout, QHeaderView, QPushButton, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
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
        for label, value in (("По score", "score"), ("По тайм-коду", "time"), ("По длительности", "duration")):
            self.sort.addItem(label, value)
        controls.addWidget(self.filter)
        controls.addWidget(self.sort)
        controls.addStretch(1)
        layout.addLayout(controls)
        self.table = QTableWidget(0, 9)
        self.table.setHorizontalHeaderLabels(("№", "Start", "End", "Длина", "Score", "Статус", "Расшифровка / причины", "Предупреждения", "Действия"))
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(6, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(8, QHeaderView.ResizeToContents)
        self.table.cellDoubleClicked.connect(lambda row, _column: self._select_row(row))
        self.filter.currentIndexChanged.connect(self.refresh)
        self.sort.currentIndexChanged.connect(self.refresh)
        layout.addWidget(self.table, 1)

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
        values.sort(key=(lambda item: -item.score) if sorting == "score" else ((lambda item: item.start) if sorting == "time" else (lambda item: -item.duration)))
        self.table.setRowCount(len(values))
        for row, candidate in enumerate(values):
            self.table.setVerticalHeaderItem(row, QTableWidgetItem(candidate.id))
            text = candidate.text[:180] + ("…" if len(candidate.text) > 180 else "")
            reasons = "; ".join(candidate.reasons)
            cells = (candidate.id.replace("short_", ""), f"{candidate.start:.1f}", f"{candidate.end:.1f}", f"{candidate.duration:.1f} с", f"{candidate.score:.1f}", STATUS_LABELS.get(candidate.status, candidate.status), text + (f"\n✓ {reasons}" if reasons else ""), "; ".join(candidate.warnings))
            for column, value in enumerate(cells):
                item = QTableWidgetItem(str(value))
                item.setData(256, candidate)
                self.table.setItem(row, column, item)
            actions = QWidget()
            action_layout = QHBoxLayout(actions)
            action_layout.setContentsMargins(0, 0, 0, 0)
            view = QPushButton("Просмотреть")
            approve = QPushButton("✓")
            reject = QPushButton("✕")
            view.clicked.connect(lambda _checked=False, item=candidate: self.selected.emit(item))
            approve.clicked.connect(lambda _checked=False, item=candidate: self.status_changed.emit(item, "approved"))
            reject.clicked.connect(lambda _checked=False, item=candidate: self.status_changed.emit(item, "rejected"))
            action_layout.addWidget(view)
            action_layout.addWidget(approve)
            action_layout.addWidget(reject)
            self.table.setCellWidget(row, 8, actions)
        self.table.resizeRowsToContents()

    def _select_row(self, row: int) -> None:
        item = self.table.item(row, 0)
        if item:
            self.selected.emit(item.data(256))
