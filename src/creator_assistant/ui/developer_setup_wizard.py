from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QThread, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication, QDialog, QHBoxLayout, QLabel, QMessageBox, QPlainTextEdit,
    QProgressBar, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout,
    QWizard, QWizardPage,
)

from creator_assistant.infrastructure.settings_store import local_data_root
from creator_assistant.product import AppEdition, DEVELOPER_AI_MODEL
from creator_assistant.services.developer_setup import MODEL_ESTIMATED_BYTES, DeveloperSetupService
from creator_assistant.ui.workers import FunctionWorker, UiWorkerBridge


def _gb(value: int) -> str:
    return f"{value / 1024**3:.1f} ГБ" if value else "не определено"


class DeveloperSetupWizard(QWizard):
    """Repeatable standalone setup flow for Developer Preview."""

    setup_saved = Signal()

    def __init__(self, container, parent=None) -> None:
        if container.edition is not AppEdition.DEVELOPER:
            raise RuntimeError("Developer setup wizard is available only in Developer Preview")
        super().__init__(parent)
        self.container = container
        self.service: DeveloperSetupService = container.developer_setup
        self.report = None
        self._threads: list[QThread] = []
        self._cancelled = False
        self.setWindowTitle("Настройка Creator Assistant Developer Preview")
        self.setMinimumSize(880, 640)
        self.setWizardStyle(QWizard.ModernStyle)
        self._create_pages()
        self.currentIdChanged.connect(self._page_changed)

    def _page(self, title: str, subtitle: str) -> tuple[QWizardPage, QVBoxLayout]:
        page = QWizardPage()
        page.setTitle(title)
        page.setSubTitle(subtitle)
        layout = QVBoxLayout(page)
        layout.setSpacing(12)
        self.addPage(page)
        return page, layout

    def _create_pages(self) -> None:
        _, layout = self._page("Creator Assistant готов к настройке", "Standalone Developer Preview — без подписки и серверной активации.")
        intro = QLabel(
            "Все функции редакции Developer доступны локально. Мастер проверит компоненты, "
            "но ничего крупного не скачает без вашего нажатия. Пропуск AI не мешает работе "
            "Подготовки проекта, редактора Shorts и остальных не-AI инструментов."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)
        layout.addStretch(1)

        _, layout = self._page("Проверка компонентов", "Автоматизируемые компоненты устанавливаются в общий пользовательский runtime и не требуют ручного PATH.")
        self.component_summary = QLabel("Проверка ещё не выполнена")
        self.component_summary.setWordWrap(True)
        self.component_table = QTableWidget(0, 4)
        self.component_table.setHorizontalHeaderLabels(("Компонент", "Статус", "Версия / путь", "Действие"))
        self.component_table.horizontalHeader().setStretchLastSection(True)
        actions = QHBoxLayout()
        refresh = QPushButton("Проверить снова")
        install_tools = QPushButton("Установить FFmpeg и yt-dlp")
        copy = QPushButton("Скопировать отчёт")
        refresh.clicked.connect(self.refresh_report)
        install_tools.clicked.connect(self.install_open_source_tools)
        copy.clicked.connect(self.copy_report)
        for button in (refresh, install_tools, copy):
            actions.addWidget(button)
        actions.addStretch(1)
        layout.addWidget(self.component_summary)
        layout.addWidget(self.component_table, 1)
        layout.addLayout(actions)

        _, layout = self._page("Локальный AI", f"Фактический AI stack: Ollama + {DEVELOPER_AI_MODEL}.")
        self.ai_status = QLabel("Не проверено")
        self.ai_status.setWordWrap(True)
        ai_actions = QHBoxLayout()
        auto = QPushButton("Установить автоматически")
        manual = QPushButton("Настроить вручную")
        skip = QPushButton("Пропустить пока")
        auto.clicked.connect(self.install_ollama)
        manual.clicked.connect(lambda: QDesktopServices.openUrl(QUrl("https://ollama.com/download/windows")))
        skip.clicked.connect(self.skip_ai)
        for button in (auto, manual, skip):
            ai_actions.addWidget(button)
        ai_actions.addStretch(1)
        self.model_progress = QProgressBar()
        self.model_progress.hide()
        self.operation_log = QPlainTextEdit()
        self.operation_log.setReadOnly(True)
        self.operation_log.setMaximumBlockCount(300)
        model_actions = QHBoxLayout()
        download = QPushButton(f"Скачать {DEVELOPER_AI_MODEL}")
        cancel = QPushButton("Отменить загрузку")
        download.clicked.connect(self.download_model)
        cancel.clicked.connect(lambda: setattr(self, "_cancelled", True))
        model_actions.addWidget(download)
        model_actions.addWidget(cancel)
        model_actions.addStretch(1)
        layout.addWidget(self.ai_status)
        layout.addLayout(ai_actions)
        layout.addWidget(QLabel(f"Модель занимает примерно {MODEL_ESTIMATED_BYTES / 1024**3:.0f} ГБ. Загрузка начинается только по кнопке."))
        layout.addWidget(self.model_progress)
        layout.addWidget(self.operation_log, 1)
        layout.addLayout(model_actions)

        _, layout = self._page("Проверка AI", "Минимальный локальный inference проверяет точную модель и structured JSON.")
        self.smoke_status = QLabel("Проверка ещё не выполнена")
        self.smoke_status.setWordWrap(True)
        test = QPushButton("Запустить AI smoke test")
        test.clicked.connect(self.test_ai)
        layout.addWidget(self.smoke_status)
        layout.addWidget(test)
        layout.addStretch(1)

        _, layout = self._page("Готово", "Мастер всегда доступен через Настройки → Компоненты / диагностика.")
        self.final_summary = QLabel()
        self.final_summary.setWordWrap(True)
        layout.addWidget(self.final_summary)
        layout.addStretch(1)

    def _start(
        self, function: Callable, finished: Callable, *,
        progress: Callable[[Any], None] | None = None,
    ) -> None:
        thread = QThread(self)
        worker = FunctionWorker(lambda emit: function(emit))
        worker.moveToThread(thread)
        bridge = UiWorkerBridge({
            "finished": finished,
            "failed": self._failed,
            "progress": progress or (lambda _value: None),
            "thread_finished": lambda: self._threads.remove(thread) if thread in self._threads else None,
        }, parent=self)
        worker.finished.connect(bridge.finished)
        worker.failed.connect(bridge.failed)
        worker.progress.connect(bridge.progress)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(bridge.thread_finished)
        thread.started.connect(worker.run)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread._worker = worker
        thread._bridge = bridge
        self._threads.append(thread)
        thread.start()

    def refresh_report(self) -> None:
        self.component_summary.setText("Проверяю компьютер и локальные компоненты…")
        self._start(lambda _emit: self.service.inspect(), self._show_report)

    def _show_report(self, report) -> None:
        self.report = report
        rows = [
            ("Windows", "готово", f"{report.windows} · {report.architecture}", "—"),
            ("RAM", "готово" if report.ram_total >= 16 * 1024**3 else "предупреждение", _gb(report.ram_total), "—"),
            ("GPU", "готово" if report.gpu != "Не обнаружен" else "CPU fallback", f"{report.gpu} · VRAM {_gb(report.vram_total)}", "—"),
            ("Ollama models", "готово" if report.model_free >= MODEL_ESTIMATED_BYTES + 8 * 1024**3 else "мало места", f"{_gb(report.model_free)} свободно · {report.ollama_models_path}", "выбрать OLLAMA_MODELS вручную"),
        ]
        for item in report.components:
            labels = {"ready": "готово", "action": "требуется действие", "warning": "необязательно", "optional": "необязательно"}
            rows.append((item.label, labels.get(item.status, item.status), item.value or item.details, "автоматически" if item.automatic else "выбрать путь / официальный сайт"))
        self.component_table.setRowCount(len(rows))
        for row, values in enumerate(rows):
            for column, value in enumerate(values):
                self.component_table.setItem(row, column, QTableWidgetItem(str(value)))
        required_ready = all(item.status == "ready" for item in report.components if item.key in {"yt_dlp", "ffmpeg", "ffprobe"})
        self.component_summary.setText("Основные media-компоненты готовы." if required_ready else "Найдены недостающие media-компоненты; их можно установить одной кнопкой.")
        self.ai_status.setText(
            f"Ollama: {report.ollama_version or ('найдена, API не запущен' if report.ollama_path else 'не установлена')}\n"
            f"Модель {DEVELOPER_AI_MODEL}: {'установлена' if report.model_installed else 'не установлена'}\n"
            f"Каталог моделей: {report.ollama_models_path} · свободно {_gb(report.model_free)}"
        )

    def copy_report(self) -> None:
        if self.report is None:
            self.refresh_report()
            return
        QApplication.clipboard().setText(json.dumps(self.report.safe_dict(), ensure_ascii=False, indent=2))

    def install_open_source_tools(self) -> None:
        self._cancelled = False
        self.model_progress.show()
        self.operation_log.clear()

        def work(emit):
            report = self.service.inspect()
            statuses = {item.key: item.status for item in report.components}
            if statuses.get("yt_dlp") != "ready":
                self.service.install_yt_dlp(emit, lambda: self._cancelled)
            if statuses.get("ffmpeg") != "ready" or statuses.get("ffprobe") != "ready":
                self.service.install_ffmpeg(emit, lambda: self._cancelled)
            return True

        self._start(work, lambda _value: self._tools_ready(), progress=self._progress)

    def _tools_ready(self) -> None:
        self.container.auto_detect_dependencies()
        self.model_progress.hide()
        self.refresh_report()

    def install_ollama(self) -> None:
        if QMessageBox.question(
            self, "Установка Ollama",
            "Скачать официальный подписанный OllamaSetup.exe и установить его для текущего пользователя?",
        ) != QMessageBox.Yes:
            return
        self._cancelled = False
        self.model_progress.show()
        self._start(
            lambda emit: self.service.install_ollama(emit, lambda: self._cancelled),
            lambda _value: self._ollama_ready(), progress=self._progress,
        )

    def _ollama_ready(self) -> None:
        try:
            self.service.start_ollama()
        except RuntimeError:
            pass
        self.model_progress.hide()
        self.refresh_report()

    def download_model(self) -> None:
        report = self.service.inspect()
        required = MODEL_ESTIMATED_BYTES + 8 * 1024**3
        if not report.ollama_api:
            QMessageBox.warning(self, "Локальный AI", "Ollama API localhost:11434 недоступен. Установите или запустите Ollama.")
            return
        if report.model_free < required:
            QMessageBox.warning(self, "Недостаточно места", f"Нужно не менее {_gb(required)}, доступно {_gb(report.model_free)} в {report.ollama_models_path}.")
            return
        if QMessageBox.question(self, "Большая загрузка", f"Скачать {DEVELOPER_AI_MODEL} (примерно {_gb(MODEL_ESTIMATED_BYTES)})?") != QMessageBox.Yes:
            return
        self._cancelled = False
        self.model_progress.show()
        self._start(lambda emit: self.service.pull_model(emit, lambda: self._cancelled), lambda _value: self._model_ready(), progress=self._progress)

    def _model_ready(self) -> None:
        self.model_progress.hide()
        self.refresh_report()

    def _progress(self, value: Any) -> None:
        if not isinstance(value, dict):
            return
        self.operation_log.appendPlainText(str(value.get("message", "")))
        percent = value.get("percent")
        if percent is None:
            self.model_progress.setRange(0, 0)
        else:
            self.model_progress.setRange(0, 100)
            self.model_progress.setValue(int(percent))

    def skip_ai(self) -> None:
        self.container.settings.setdefault("developer_setup", {})["ai_skipped"] = True
        self.ai_status.setText(f"AI пока пропущен. Для AI-функций будет предложена настройка {DEVELOPER_AI_MODEL}; остальные функции доступны.")

    def test_ai(self) -> None:
        self.smoke_status.setText(f"Запускаю {DEVELOPER_AI_MODEL}; первый ответ может занять несколько минут…")
        self._start(lambda _emit: self.service.ai_smoke_test(), self._ai_ready)

    def _ai_ready(self, result: dict[str, Any]) -> None:
        self.container.settings.setdefault("developer_setup", {})["last_preflight"] = result
        self.smoke_status.setText(f"Локальная модель готова. Ответ получен за {result['duration_seconds']:.1f} с.")

    def _failed(self, message: str, details: str) -> None:
        self.model_progress.hide()
        self.operation_log.appendPlainText(details[-2000:])
        QMessageBox.warning(self, "Настройка компонента", f"{message}\n\n{details[-1200:]}")

    def _page_changed(self, page_id: int) -> None:
        if page_id == 1 and self.report is None:
            self.refresh_report()
        if page_id == self.pageIds()[-1]:
            report = self.report or self.service.inspect()
            self.final_summary.setText(
                "Creator Assistant Developer Preview готов к локальной работе.\n"
                f"Лицензия и backend: не используются\n"
                f"Media tools: {'готовы' if all(item.status == 'ready' for item in report.components if item.key in {'yt_dlp', 'ffmpeg', 'ffprobe'}) else 'можно настроить позже'}\n"
                f"AI: {DEVELOPER_AI_MODEL} · {'готов' if report.model_installed else 'можно настроить позже'}"
            )

    def accept(self) -> None:
        setup = self.container.settings.setdefault("developer_setup", {})
        setup.update({"completed": True, "schema_version": 1})
        self.container.settings_store.save(self.container.settings)
        self.container.first_run = False
        self.setup_saved.emit()
        super().accept()


class DeveloperComponentsDialog(QDialog):
    def __init__(self, container, parent=None) -> None:
        if container.edition is not AppEdition.DEVELOPER:
            raise RuntimeError("Developer Preview only")
        super().__init__(parent)
        self.container = container
        self.setWindowTitle("Компоненты / диагностика — Developer Preview")
        self.resize(680, 260)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            f"AI runtime: Ollama\nМодель: {DEVELOPER_AI_MODEL}\n"
            "Лицензирование и Creator Assistant backend этой редакцией не используются."
        ))
        actions = QHBoxLayout()
        wizard = QPushButton("Открыть мастер настройки")
        diagnostics = QPushButton("Открыть полную диагностику")
        wizard.clicked.connect(lambda: DeveloperSetupWizard(container, self).exec())
        diagnostics.clicked.connect(self.accept)
        actions.addWidget(wizard)
        actions.addWidget(diagnostics)
        layout.addLayout(actions)
        close = QPushButton("Закрыть")
        close.clicked.connect(self.accept)
        layout.addWidget(close)

