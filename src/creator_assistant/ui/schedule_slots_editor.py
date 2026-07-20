from __future__ import annotations

from PySide6.QtCore import QTime, Signal
from PySide6.QtWidgets import QHBoxLayout, QPushButton, QTimeEdit, QVBoxLayout, QWidget


class ScheduleSlotsEditor(QWidget):
    """Small ordered list of publication times; values are always HH:mm."""

    changed = Signal()

    def __init__(self, values: list[str] | None = None, parent=None) -> None:
        super().__init__(parent)
        self._rows = QVBoxLayout(self)
        self._rows.setContentsMargins(0, 0, 0, 0)
        self._edits: list[tuple[QWidget, QTimeEdit]] = []
        add = QPushButton("Добавить слот")
        add.clicked.connect(lambda: self.add_slot("12:00"))
        self._rows.addWidget(add)
        self._add_button = add
        for value in values or ["13:00", "19:00"]:
            self.add_slot(value, emit=False)

    def add_slot(self, value: str = "12:00", *, emit: bool = True) -> None:
        row = QWidget(self)
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        editor = QTimeEdit(QTime.fromString(value.zfill(5), "HH:mm"), row)
        editor.setDisplayFormat("HH:mm")
        remove = QPushButton("Удалить", row)
        layout.addWidget(editor)
        layout.addWidget(remove)
        self._rows.insertWidget(self._rows.count() - 1, row)
        self._edits.append((row, editor))
        editor.timeChanged.connect(self.changed)
        remove.clicked.connect(lambda: self._remove(row))
        if emit:
            self.changed.emit()

    def _remove(self, row: QWidget) -> None:
        self._edits = [item for item in self._edits if item[0] is not row]
        row.deleteLater()
        self.changed.emit()

    def values(self) -> list[str]:
        return sorted(editor.time().toString("HH:mm") for _row, editor in self._edits)

    def set_values(self, values: list[str]) -> None:
        for row, _editor in self._edits:
            row.deleteLater()
        self._edits.clear()
        for value in values:
            self.add_slot(value, emit=False)
        self.changed.emit()
