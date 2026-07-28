from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from PySide6.QtCore import QThread, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QCheckBox, QComboBox, QDialog, QFileDialog,
    QFormLayout, QGroupBox, QHBoxLayout, QLabel, QMessageBox, QPlainTextEdit,
    QProgressBar, QPushButton, QRadioButton, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWizard, QWizardPage,
)

from creator_assistant.infrastructure.build_info import current_build_info
from creator_assistant.infrastructure.settings_store import local_data_root
from creator_assistant.product import AppEdition
from creator_assistant.services.commercial_setup import AI_PROFILES, CommercialSetupService
from creator_assistant.ui.workers import FunctionWorker, UiWorkerBridge


def _gb(value: int) -> str:
    return f"{value / 1024**3:.1f} ГБ" if value else "не определено"


class CommercialSetupWizard(QWizard):
    """First-run wizard. It is instantiated only by Commercial Edition."""

    setup_saved = Signal()

    def __init__(self, container, parent=None) -> None:
        if container.edition is not AppEdition.COMMERCIAL:
            raise RuntimeError("Мастер первоначальной настройки доступен только в Commercial Edition")
        super().__init__(parent)
        self.container = container
        self.service: CommercialSetupService = container.commercial_setup
        self.report = None
        self._threads: list[QThread] = []
        self._pull_cancelled = False
        self.setWindowTitle("Первоначальная настройка Creator Assistant")
        self.setMinimumSize(850, 620)
        self.setWizardStyle(QWizard.ModernStyle)
        self._create_pages()
        self.currentIdChanged.connect(self._page_changed)
        self.setOption(QWizard.HaveCustomButton1, True)
        self.setButtonText(QWizard.CustomButton1, "Продолжить позже")
        self.customButtonClicked.connect(lambda which: self.reject() if which == QWizard.CustomButton1 else None)

    def _page(self, title: str, text: str = "") -> tuple[QWizardPage, QVBoxLayout]:
        page = QWizardPage(); page.setTitle(title); page.setSubTitle(text)
        layout = QVBoxLayout(page); layout.setSpacing(12)
        self.addPage(page)
        return page, layout

    def _create_pages(self) -> None:
        page, layout = self._page("Добро пожаловать", "Локальная подготовка Creator Assistant Commercial")
        label = QLabel("Исходные видео обрабатываются на этом компьютере и не отправляются на сервер приложения. "
                       "Ollama и выбранная AI-модель устанавливаются отдельно; профиль можно сменить позже.")
        label.setWordWrap(True); layout.addWidget(label)
        requirements = QPushButton("Открыть системные требования")
        requirements.clicked.connect(lambda: QMessageBox.information(self, "Рекомендации",
            "Компактная: желательно 16 ГБ RAM.\nМаксимальное качество: рекомендуется 32 ГБ RAM, NVIDIA GPU и около 16 ГБ VRAM.\n"
            "Это рекомендации, а не жёсткие требования."))
        layout.addWidget(requirements); layout.addStretch(1)

        page, layout = self._page(
            "Активация лицензии",
            "AI можно настроить до активации, но платные операции доступны только после успешной активации.",
        )
        self.activation_status = QLabel("Лицензия ещё не проверена.")
        self.activation_status.setWordWrap(True)
        activate = QPushButton("Открыть активацию лицензии")
        activate.clicked.connect(self._open_license)
        layout.addWidget(self.activation_status)
        layout.addWidget(activate)
        layout.addWidget(QLabel("Можно продолжить настройку и вернуться к активации позже."))
        layout.addStretch(1)

        page, layout = self._page("Проверка компьютера", "Проверка выполняется локально и не собирает токены или содержимое проектов.")
        self.computer_summary = QLabel("Проверка ещё не выполнена"); self.computer_summary.setWordWrap(True)
        self.computer_table = QTableWidget(0, 3); self.computer_table.setHorizontalHeaderLabels(("Компонент", "Статус", "Значение"))
        self.computer_table.horizontalHeader().setStretchLastSection(True)
        row = QHBoxLayout(); check = QPushButton("Проверить ещё раз"); copy = QPushButton("Скопировать отчёт о компьютере")
        check.clicked.connect(self.refresh_report); copy.clicked.connect(self.copy_report)
        row.addWidget(check); row.addWidget(copy); row.addStretch(1)
        layout.addWidget(self.computer_summary); layout.addWidget(self.computer_table, 1); layout.addLayout(row)

        page, layout = self._page("Выбор AI-профиля", "Автоматическая рекомендация учитывает RAM, VRAM и место, но выбор остаётся за вами.")
        self.profile_buttons = QButtonGroup(self); self.profile_radios: dict[str, QRadioButton] = {}
        saved = self.service.selected_profile().id
        for profile in AI_PROFILES.values():
            group = QGroupBox(profile.display_name); box = QVBoxLayout(group)
            radio = QRadioButton(f"{profile.model_id} · около {profile.estimated_bytes / 1024**3:.0f} ГБ")
            radio.setChecked(profile.id == saved); self.profile_buttons.addButton(radio); self.profile_radios[profile.id] = radio
            details = QLabel(profile.quality + "\n" + profile.requirements); details.setWordWrap(True)
            box.addWidget(radio); box.addWidget(details); layout.addWidget(group)
        self.recommendation_label = QLabel(); self.recommendation_label.setWordWrap(True); layout.addWidget(self.recommendation_label); layout.addStretch(1)

        page, layout = self._page("Ollama", "Наличие файла недостаточно: мастер проверяет локальный API и версию.")
        self.ollama_status = QLabel("Не проверено"); self.ollama_status.setWordWrap(True)
        row = QHBoxLayout(); install = QPushButton("Скачать и установить Ollama"); retry = QPushButton("Проверить ещё раз")
        install.clicked.connect(self.open_official_ollama); retry.clicked.connect(self.refresh_report)
        row.addWidget(install); row.addWidget(retry); row.addStretch(1)
        layout.addWidget(self.ollama_status); layout.addLayout(row); layout.addWidget(QLabel("Также можно выбрать «Я установлю Ollama самостоятельно» и вернуться к проверке позже.")); layout.addStretch(1)

        page, layout = self._page("Загрузка модели", "Скачивается только точная модель выбранного профиля; скрытого fallback нет.")
        self.model_status = QLabel("Не проверено"); self.model_status.setWordWrap(True)
        self.pull_progress = QProgressBar(); self.pull_progress.setRange(0, 0); self.pull_progress.hide()
        self.pull_log = QPlainTextEdit(); self.pull_log.setReadOnly(True); self.pull_log.setMaximumBlockCount(200)
        row = QHBoxLayout(); pull = QPushButton("Скачать выбранную модель"); cancel = QPushButton("Отмена")
        pull.clicked.connect(self.pull_selected_model); cancel.clicked.connect(lambda: setattr(self, "_pull_cancelled", True))
        row.addWidget(pull); row.addWidget(cancel); row.addStretch(1)
        layout.addWidget(self.model_status); layout.addWidget(self.pull_progress); layout.addWidget(self.pull_log, 1); layout.addLayout(row)

        page, layout = self._page("Проверка AI", "Короткий structured-output запрос проверит запуск модели и валидность JSON.")
        self.ai_test_status = QLabel("Проверка ещё не выполнена"); self.ai_test_status.setWordWrap(True)
        test = QPushButton("Проверить выбранную модель"); test.clicked.connect(self.test_selected_model)
        layout.addWidget(self.ai_test_status); layout.addWidget(test); layout.addStretch(1)

        page, layout = self._page("Whisper, FFmpeg и рендер", "Отсутствующий компонент блокирует только связанную операцию.")
        self.runtime_status = QLabel("Результаты появятся после проверки компьютера"); self.runtime_status.setWordWrap(True)
        layout.addWidget(self.runtime_status); layout.addStretch(1)

        page, layout = self._page("Папки", "Можно выбрать несистемный диск. Перед сохранением проверяется возможность записи.")
        self.folder_edits: dict[str, QComboBox] = {}
        form = QFormLayout()
        setup = self.container.settings.get("commercial_setup", {})
        defaults = {
            "projects_folder": setup.get("projects_folder") or self.container.settings.get("youtube_root", ""),
            "renders_folder": setup.get("renders_folder") or "",
            "temp_folder": setup.get("temp_folder") or self.container.settings.get("temp_root", ""),
        }
        for key, label in (("projects_folder", "Проекты"), ("renders_folder", "Готовые Shorts"), ("temp_folder", "Временные файлы")):
            combo = QComboBox(); combo.setEditable(True); combo.addItem(str(defaults[key] or ""))
            browse = QPushButton("Обзор…"); browse.clicked.connect(lambda _=False, c=combo: self._browse(c))
            line = QHBoxLayout(); line.addWidget(combo, 1); line.addWidget(browse); form.addRow(label, line); self.folder_edits[key] = combo
        layout.addLayout(form); layout.addStretch(1)

        page, layout = self._page(
            "Короткое обучение",
            "Основной рабочий путь от исходника до готового вертикального MP4.",
        )
        training = QLabel(
            "1. «Подготовка проекта»: выберите автора и исходное видео.\n"
            "2. Дождитесь MAX/proxy/audio и откройте вкладку Shorts.\n"
            "3. Выполните локальную расшифровку и AI-анализ.\n"
            "4. Проверьте кандидаты, субтитры, заголовок и баннер.\n"
            "5. Сделайте тестовый 5-секундный рендер, затем «Рендер всех Shorts».\n"
            "6. Готовые MP4 находятся в Shorts\\Renders."
        )
        training.setWordWrap(True)
        layout.addWidget(training)
        layout.addStretch(1)

        page, layout = self._page("Готово", "Настройки можно изменить позже через «Настройки → Локальный AI».")
        self.final_summary = QLabel(); self.final_summary.setWordWrap(True)
        layout.addWidget(self.final_summary); layout.addStretch(1)

    def _selected_profile_id(self) -> str:
        return next((key for key, radio in self.profile_radios.items() if radio.isChecked()), "maximum_quality")

    def _open_license(self) -> None:
        from creator_assistant.ui.license_dialog import LicenseDialog
        LicenseDialog(self.container, self).exec()
        status = self.container.feature_gate.status()
        state = status.state.value if hasattr(status.state, "value") else str(status.state)
        self.activation_status.setText(f"Текущее состояние лицензии: {state}")

    def _start(self, function, finished, failed=None, progress=None) -> None:
        thread = QThread(self); worker = FunctionWorker(lambda emit: function(emit)); worker.moveToThread(thread)
        bridge = UiWorkerBridge({"finished": finished, "failed": failed or self._failed,
                                 "progress": progress or (lambda _: None),
                                 "thread_finished": lambda: self._threads.remove(thread) if thread in self._threads else None}, parent=self)
        worker.finished.connect(bridge.finished); worker.failed.connect(bridge.failed); worker.progress.connect(bridge.progress)
        worker.finished.connect(thread.quit); worker.failed.connect(thread.quit); thread.finished.connect(bridge.thread_finished)
        thread.started.connect(worker.run); thread.finished.connect(worker.deleteLater); thread.finished.connect(thread.deleteLater)
        thread._worker = worker; thread._bridge = bridge; self._threads.append(thread); thread.start()

    def refresh_report(self) -> None:
        self.computer_summary.setText("Проверяю компьютер…")
        self._start(lambda _: self.service.inspect(), self._show_report)

    def _show_report(self, report) -> None:
        self.report = report
        recommended = AI_PROFILES[report.recommendation]
        self.recommendation_label.setText(f"Для этого компьютера рекомендуется: {recommended.display_name} ({recommended.model_id})")
        rows = [
            ("Windows", "Готово", f"{report.windows} · {report.architecture}"), ("CPU", "Готово", report.cpu),
            ("RAM", "Готово" if report.ram_total >= 16 * 1024**3 else "Предупреждение", f"{_gb(report.ram_total)}, доступно {_gb(report.ram_available)}"),
            ("GPU / VRAM", "Готово" if report.gpu_vendor else "Предупреждение", f"{report.gpu} · {_gb(report.vram_total)}"),
            ("NVIDIA / CUDA", "Готово" if report.cuda else "Предупреждение", f"драйвер {report.nvidia_driver or '—'} · CUDA {report.cuda or '—'}"),
            ("Ollama API", "Готово" if report.ollama_api else "Требуется действие", report.ollama_version or report.ollama_path or "не найден"),
            ("Системный диск", "Готово" if report.system_free >= 10 * 1024**3 else "Предупреждение", f"свободно {_gb(report.system_free)}"),
            ("Место для Ollama", "Готово" if report.ollama_free >= 12 * 1024**3 else "Требуется действие", f"{_gb(report.ollama_free)} · {report.ollama_models_path}"),
            ("Папка проектов", "Готово" if report.projects_free >= 10 * 1024**3 else "Предупреждение", f"свободно {_gb(report.projects_free)}"),
        ] + [(item.label, {"ready": "Готово", "warning": "Предупреждение", "action": "Требуется действие"}.get(item.status, item.status), item.value or item.details) for item in report.components]
        self.computer_table.setRowCount(len(rows))
        for row, values in enumerate(rows):
            for column, value in enumerate(values): self.computer_table.setItem(row, column, QTableWidgetItem(value))
        self.computer_summary.setText(f"Проверено. Рекомендация: {recommended.display_name}. Слабое железо не блокирует CPU-режим.")
        self.ollama_status.setText(f"Ollama {report.ollama_version}: API localhost доступен" if report.ollama_api else "Ollama API localhost:11434 недоступен. Установите или запустите Ollama.")
        selected = self.service.profile(self._selected_profile_id())
        installed = any(str(item.get("name") or item.get("model")) == selected.model_id for item in report.installed_models)
        self.model_status.setText(f"{selected.model_id}: {'установлена' if installed else 'не установлена'}")
        self.runtime_status.setText("\n".join(f"{item.label}: {item.status} · {item.value or item.details}" for item in report.components))

    def copy_report(self) -> None:
        if not self.report: self.refresh_report(); return
        QApplication.clipboard().setText(json.dumps(self.report.safe_dict(), ensure_ascii=False, indent=2))

    def open_official_ollama(self) -> None:
        if QMessageBox.question(self, "Официальная установка Ollama", "Открыть официальный установщик Ollama? Установка начнётся только после вашего подтверждения в установщике.") == QMessageBox.Yes:
            QDesktopServices.openUrl(QUrl("https://ollama.com/download/OllamaSetup.exe"))

    def pull_selected_model(self) -> None:
        model = self.service.profile(self._selected_profile_id()).model_id
        if not self.report or not self.report.ollama_api:
            QMessageBox.warning(self, "Ollama", "Сначала запустите Ollama и повторите проверку API."); return
        self._pull_cancelled = False; self.pull_progress.show(); self.pull_log.clear()
        self._start(lambda emit: self.service.pull_model(model, emit, lambda: self._pull_cancelled) or model,
                    lambda _: (self.pull_progress.hide(), self.refresh_report()), progress=self._pull_message)

    def _pull_message(self, value: Any) -> None:
        if isinstance(value, dict):
            self.pull_log.appendPlainText(str(value.get("message", "")))
            if value.get("percent") is not None:
                self.pull_progress.setRange(0, 100); self.pull_progress.setValue(int(value["percent"]))

    def test_selected_model(self) -> None:
        model = self.service.profile(self._selected_profile_id()).model_id
        self.ai_test_status.setText(
            f"Модель: {model}\n"
            "Состояние: запуск модели → ожидание ответа → проверка Structured JSON…\n"
            "Первый запуск большой модели может занять несколько минут."
        )
        self._start(lambda _: self.service.structured_preflight(model), self._preflight_ready)

    def _preflight_ready(self, result: dict[str, Any]) -> None:
        self.container.settings.setdefault("commercial_setup", {})["last_preflight"] = result
        model = str(result.get("model") or self.service.profile(self._selected_profile_id()).model_id)
        self.ai_test_status.setText(
            f"Модель {model} готова.\n"
            f"Проверка завершена за {result['duration_seconds']:.1f} с.\n"
            "Structured JSON получен и прошёл валидацию."
        )

    def _failed(self, message: str, details: str) -> None:
        self.pull_progress.hide(); self.ai_test_status.setText(f"Проблема: {message}")
        QMessageBox.warning(self, "Настройка", f"{message}\n\nТехнические подробности:\n{details[-1500:]}")

    def _browse(self, combo: QComboBox) -> None:
        value = QFileDialog.getExistingDirectory(self, "Выберите папку", combo.currentText())
        if value: combo.setEditText(value)

    def _page_changed(self, page_id: int) -> None:
        if page_id == 1 and self.report is None: self.refresh_report()
        if page_id == self.pageIds()[-1]:
            profile = self.service.profile(self._selected_profile_id())
            self.final_summary.setText(f"Лицензия: интерфейс готов\nAI: {profile.display_name} · {profile.model_id}\n"
                                       f"Ollama: {'готова' if self.report and self.report.ollama_api else 'можно настроить позже'}\n"
                                       f"После закрытия: создайте проект → расшифруйте → найдите моменты → проверьте → отрендерите MP4.")

    def _finish_setup(self) -> None:
        profile = self.service.apply_profile(self._selected_profile_id())
        setup = self.container.settings.setdefault("commercial_setup", {})
        for key, combo in self.folder_edits.items():
            path = combo.currentText().strip()
            if path:
                target = Path(path); target.mkdir(parents=True, exist_ok=True)
                probe = target / ".creator-assistant-write-test"; probe.write_text("ok", encoding="utf-8"); probe.unlink()
                setup[key] = str(target)
        setup.update({"completed": True, "schema_version": 1})
        self.container.settings["shorts_ai"]["model_profile"] = profile.id
        self.container.settings_store.save(self.container.settings)
        self.container.first_run = False; self.setup_saved.emit()

    def accept(self) -> None:
        try:
            self._finish_setup()
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "Папки", f"Не удалось сохранить выбранные папки: {exc}")
            return
        super().accept()


class LocalAIDialog(QDialog):
    def __init__(self, container, parent=None) -> None:
        if container.edition is not AppEdition.COMMERCIAL: raise RuntimeError("Commercial Edition only")
        super().__init__(parent); self.container = container; self.service = container.commercial_setup
        self.setWindowTitle("Локальный AI"); self.resize(720, 430)
        layout = QVBoxLayout(self); self.summary = QLabel(); self.summary.setWordWrap(True); layout.addWidget(self.summary)
        self.profile = QComboBox()
        for item in AI_PROFILES.values(): self.profile.addItem(f"{item.display_name} · {item.model_id}", item.id)
        self.profile.setCurrentIndex(max(0, self.profile.findData(self.service.selected_profile().id)))
        layout.addWidget(self.profile)
        actions = QHBoxLayout()
        for text, slot in (("Применить профиль", self.apply_profile), ("Проверить Ollama", self.refresh), ("Запустить Ollama", self.start_ollama),
                           ("Открыть мастер настройки", self.open_wizard), ("Скачать выбранную модель", self.open_wizard),
                           ("Проверить модель", self.test), ("Удалить выбранную модель", self.remove_model),
                           ("Скопировать отчёт для поддержки", self.copy_report), ("Открыть диагностический отчёт", self.open_report)):
            button = QPushButton(text); button.clicked.connect(slot); actions.addWidget(button)
        layout.addLayout(actions); self.details = QPlainTextEdit(); self.details.setReadOnly(True); layout.addWidget(self.details, 1)
        close = QPushButton("Закрыть"); close.clicked.connect(self.accept); layout.addWidget(close); self.refresh()

    def refresh(self) -> None:
        try:
            report = self.service.inspect(); selected = self.service.selected_profile()
            installed = any(str(item.get("name") or item.get("model")) == selected.model_id for item in report.installed_models)
            self.summary.setText(f"Профиль: {selected.display_name}\nМодель: {selected.model_id} · {'установлена' if installed else 'не установлена'}\n"
                                 f"Ollama: {report.ollama_version or 'API недоступен'}\nМодели: {report.ollama_models_path}\nСвободно: {_gb(report.ollama_free)}")
            self.details.setPlainText(json.dumps(report.safe_dict(), ensure_ascii=False, indent=2)); self._report = report
        except Exception as exc: self.summary.setText(f"Ollama недоступна: {exc}")

    def test(self) -> None:
        profile = self.apply_profile(show_message=False)
        try:
            result = self.service.structured_preflight(profile.model_id)
            self.container.settings.setdefault("commercial_setup", {})["last_preflight"] = result
            self.container.settings_store.save(self.container.settings)
            QMessageBox.information(self, "Локальный AI", f"{profile.model_id} готова · {result['duration_seconds']:.1f} с")
        except Exception as exc: QMessageBox.warning(self, "Локальный AI", str(exc))

    def open_wizard(self) -> None:
        CommercialSetupWizard(self.container, self).exec(); self.refresh()

    def apply_profile(self, show_message: bool = True):
        profile = self.service.apply_profile(str(self.profile.currentData()))
        self.container.settings_store.save(self.container.settings)
        self.container.rebuild()
        if show_message:
            QMessageBox.information(self, "Локальный AI", f"Для будущих анализов выбрана только {profile.model_id}. Существующие проекты не пересчитываются.")
        self.refresh()
        return profile

    def start_ollama(self) -> None:
        try:
            self.service.start_ollama(); QMessageBox.information(self, "Ollama", "Запуск Ollama выполнен. Нажмите «Проверить Ollama» через несколько секунд.")
        except Exception as exc: QMessageBox.warning(self, "Ollama", str(exc))

    def remove_model(self) -> None:
        profile = self.service.profile(str(self.profile.currentData()))
        installed_models = self._report.installed_models if hasattr(self, "_report") else []
        size = next((int(item.get("size", 0) or 0) for item in installed_models
                     if str(item.get("name") or item.get("model")) == profile.model_id), 0)
        if QMessageBox.question(self, "Удалить модель", f"Удалить только {profile.model_id} ({_gb(size)}) через Ollama?") != QMessageBox.Yes: return
        try: self.service.remove_model(profile.model_id); self.refresh()
        except Exception as exc: QMessageBox.warning(self, "Удаление модели", str(exc))

    def copy_report(self) -> None:
        QApplication.clipboard().setText(self.details.toPlainText())

    def open_report(self) -> None:
        try:
            report = json.loads(self.details.toPlainText())
            build = current_build_info()
            report.update({"app_edition": "commercial", "app_version": build.version,
                           "commit": build.commit, "selected_model": self.service.selected_profile().model_id,
                           "last_preflight": self.container.settings.get("commercial_setup", {}).get("last_preflight", {})})
            json_path, _ = self.service.write_report(report, local_data_root() / "diagnostics")
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(json_path)))
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "Диагностический отчёт", str(exc))
