from __future__ import annotations

from typing import Callable

from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QInputDialog,
    QLabel, QMessageBox, QPushButton, QVBoxLayout,
)

from creator_assistant.services.shorts.global_template_library import GlobalShortsTemplateLibrary
from creator_assistant.services.shorts.project_template import ProjectShortsTemplate
from creator_assistant.ui.shorts.project_template_dialog import ProjectTemplateDialog


class GlobalTemplateLibraryDialog(QDialog):
    def __init__(
        self,
        library: GlobalShortsTemplateLibrary,
        current_template: Callable[[], ProjectShortsTemplate],
        apply_to_project: Callable[[ProjectShortsTemplate, str], None],
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.library = library
        self.current_template = current_template
        self.apply_to_project = apply_to_project
        self.setWindowTitle("Глобальная библиотека шаблонов Shorts")
        self.setMinimumWidth(760)
        root = QVBoxLayout(self)
        help_label = QLabel(
            "Глобальный шаблон хранит композицию и геометрию баннера, но не путь к картинке. "
            "Изображение всегда выбирается через профиль фактического автора."
        )
        help_label.setWordWrap(True)
        root.addWidget(help_label)
        self.templates = QComboBox()
        root.addWidget(self.templates)
        actions = QHBoxLayout()
        for label, handler in (
            ("Создать из текущего", self._create), ("Обновить текущими", self._update),
            ("Дублировать", self._duplicate), ("Переименовать", self._rename),
            ("Удалить", self._delete), ("Просмотреть", self._view),
        ):
            button = QPushButton(label)
            button.clicked.connect(handler)
            actions.addWidget(button)
        root.addLayout(actions)
        self.use_for_project = QPushButton("Выбрать этот шаблон для проекта")
        self.use_for_project.clicked.connect(self._apply_project)
        root.addWidget(self.use_for_project)
        form = QFormLayout()
        self.assignments = {}
        for label, scope in (
            ("По умолчанию для новых проектов", "default"),
            ("YouTube template", "youtube"),
            ("TikTok template", "tiktok"),
        ):
            combo = QComboBox()
            self.assignments[scope] = combo
            form.addRow(label, combo)
        root.addLayout(form)
        save_assignments = QPushButton("Сохранить назначения")
        save_assignments.clicked.connect(self._save_assignments)
        root.addWidget(save_assignments)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.close)
        root.addWidget(buttons)
        self.refresh()

    def refresh(self, selected_id: str = "") -> None:
        current_id = selected_id or str(self.templates.currentData() or "")
        values = self.library.templates()
        self.templates.clear()
        for item in values:
            self.templates.addItem(item.name, item.template_id)
        index = self.templates.findData(current_id)
        self.templates.setCurrentIndex(max(0, index))
        for scope, combo in self.assignments.items():
            combo.clear()
            combo.addItem("Не назначен", "")
            for item in values:
                combo.addItem(item.name, item.template_id)
            combo.setCurrentIndex(max(0, combo.findData(self.library.assignment(scope))))

    def _selected(self):
        return self.library.get(str(self.templates.currentData() or ""))

    def _create(self) -> None:
        name, ok = QInputDialog.getText(self, "Новый глобальный шаблон", "Название")
        if ok:
            value = self.library.create(name, self.current_template())
            self.refresh(value.template_id)

    def _update(self) -> None:
        selected = self._selected()
        if selected:
            self.library.update(selected.template_id, self.current_template())
            self.refresh(selected.template_id)

    def _duplicate(self) -> None:
        selected = self._selected()
        if selected:
            value = self.library.duplicate(selected.template_id)
            self.refresh(value.template_id)

    def _rename(self) -> None:
        selected = self._selected()
        if not selected:
            return
        name, ok = QInputDialog.getText(self, "Переименовать шаблон", "Название", text=selected.name)
        if ok:
            value = self.library.rename(selected.template_id, name)
            self.refresh(value.template_id)

    def _delete(self) -> None:
        selected = self._selected()
        if selected and QMessageBox.question(self, "Удалить шаблон", f"Удалить «{selected.name}»?") == QMessageBox.Yes:
            self.library.delete(selected.template_id)
            self.refresh()

    def _view(self) -> None:
        selected = self._selected()
        if selected:
            ProjectTemplateDialog(selected.project_template(), f"Глобальный шаблон: {selected.name}", self).exec()

    def _apply_project(self) -> None:
        selected = self._selected()
        if selected:
            self.apply_to_project(selected.project_template(), selected.name)

    def _save_assignments(self) -> None:
        for scope, combo in self.assignments.items():
            self.library.assign(scope, str(combo.currentData() or ""))
        QMessageBox.information(self, "Шаблоны Shorts", "Назначения глобальных шаблонов сохранены.")
