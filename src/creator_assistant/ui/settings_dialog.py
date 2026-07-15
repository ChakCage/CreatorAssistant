from __future__ import annotations

from copy import deepcopy
import os
from pathlib import Path
import time
from typing import Any, Dict

from PySide6.QtCore import QThread
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
    QMessageBox,
)

from creator_assistant.ui.workers import FunctionWorker, UiWorkerBridge
from creator_assistant.domain.youtube_auth import BROWSERS
from creator_assistant.services.storage_service import default_temp_root
from creator_assistant.services.shorts.semantic_backend import OllamaSemanticScorer
from creator_assistant.domain.author_presets import merge_presets
from creator_assistant.infrastructure.project_index import ProjectRoot
from creator_assistant.infrastructure.windows_paths import discover_author_folders


class SettingsDialog(QDialog):
    def __init__(self, settings_or_container, parent=None) -> None:
        super().__init__(parent)
        self.container = settings_or_container if hasattr(settings_or_container, "settings") else None
        settings: Dict[str, Any] = self.container.settings if self.container else settings_or_container
        self.result_settings = deepcopy(settings)
        self.path_edits: Dict[str, QLineEdit] = {}
        self.naming_edits: Dict[str, QLineEdit] = {}
        self._threads: list[QThread] = []
        self._notify_auto_search = False
        self.setWindowTitle("Настройки Creator Assistant")
        self.setMinimumSize(620, 480)
        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(14, 14, 14, 14)
        self.scroll_area = QScrollArea()
        self.scroll_area.setObjectName("settingsScrollArea")
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QScrollArea.NoFrame)
        content = QWidget()
        content.setObjectName("settingsContent")
        content.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.MinimumExpanding)
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(4, 4, 12, 20)
        content_layout.setSpacing(12)

        paths_group = QGroupBox("Пути")
        paths_form = QFormLayout(paths_group)
        path_fields = (
            ("youtube_root", "Корневая папка YouTube", True),
            ("temp_root", "Временные файлы заданий", True),
            ("yt_dlp_path", "yt-dlp.exe", False),
            ("ffmpeg_path", "ffmpeg.exe", False),
            ("ffprobe_path", "ffprobe.exe", False),
            ("uvr_path", "UVR_Launcher.exe", False),
            ("reaper_path", "reaper.exe", False),
        )
        for key, label, directory in path_fields:
            edit = QLineEdit(str(settings.get(key, "")))
            self._show_full_path(edit, edit.text())
            button = QPushButton("Обзор…")
            button.clicked.connect(lambda checked=False, e=edit, d=directory: self._browse(e, d))
            row = QHBoxLayout()
            row.addWidget(edit, 1)
            row.addWidget(button)
            paths_form.addRow(label, row)
            self.path_edits[key] = edit
        self.auto_find_button = QPushButton("Найти программы автоматически")
        self.auto_find_button.setToolTip("Проверить сохранённые пути и найти отсутствующие программы в фоне")
        self.auto_find_button.clicked.connect(lambda: self._start_auto_search(True))
        paths_form.addRow("", self.auto_find_button)
        content_layout.addWidget(paths_group)

        vegas_group = QGroupBox("VEGAS Pro")
        vegas_form = QFormLayout(vegas_group)
        self.vegas_edit = QLineEdit(str(settings.get("vegas_path", "")))
        self._show_full_path(self.vegas_edit, self.vegas_edit.text())
        vegas_browse = QPushButton("Обзор…")
        vegas_browse.clicked.connect(lambda: self._browse(self.vegas_edit, False))
        vegas_path_row = QHBoxLayout()
        vegas_path_row.addWidget(self.vegas_edit, 1)
        vegas_path_row.addWidget(vegas_browse)
        vegas_form.addRow("Путь к VEGAS Pro", vegas_path_row)
        self.path_edits["vegas_path"] = self.vegas_edit
        self.vegas_version = QLabel("Не проверено")
        self.vegas_version.setProperty("class", "muted")
        vegas_form.addRow("Версия", self.vegas_version)
        self.create_vegas_default = QCheckBox("Создавать проект VEGAS по умолчанию")
        self.create_vegas_default.setChecked(bool(settings.get("create_vegas_project_default", True)))
        self.auto_open_vegas = QCheckBox("Автоматически открывать VEGAS-проект после завершения")
        self.auto_open_vegas.setChecked(bool(settings.get("auto_open_vegas_project", False)))
        vegas_form.addRow(self.create_vegas_default)
        vegas_form.addRow("Видео для проекта", QLabel("Максимальное SDR-видео"))
        vegas_form.addRow("Звук", QLabel("Instrumental Only"))
        vegas_form.addRow(self.auto_open_vegas)
        vegas_actions = QHBoxLayout()
        find_vegas = QPushButton("Найти VEGAS автоматически")
        verify_vegas = QPushButton("Проверить VEGAS")
        test_vegas = QPushButton("Создать тестовый проект")
        open_vegas_script = QPushButton("Открыть папку скрипта")
        find_vegas.clicked.connect(self._find_vegas)
        verify_vegas.clicked.connect(self._verify_vegas)
        test_vegas.clicked.connect(self._open_vegas_diagnostics)
        open_vegas_script.clicked.connect(self._open_vegas_script_folder)
        for button in (find_vegas, verify_vegas, test_vegas, open_vegas_script):
            button.setEnabled(self.container is not None)
            vegas_actions.addWidget(button)
        vegas_form.addRow(vegas_actions)
        content_layout.addWidget(vegas_group)
        self._update_vegas_status()

        authors_group = QGroupBox("Авторы и проекты")
        authors_form = QFormLayout(authors_group)
        self.suggest_remember_author = QCheckBox("Предлагать запомнить выбор автора")
        self.suggest_remember_author.setChecked(bool(settings.get("suggest_remember_author", True)))
        self.project_rescan_button = QPushButton("Пересканировать проекты")
        self.project_rescan_status = QLabel("Индекс используется только как ускоряющий кэш")
        self.project_rescan_status.setProperty("class", "muted")
        self.project_rescan_status.setWordWrap(True)
        self.project_rescan_button.setEnabled(self.container is not None)
        self.project_rescan_button.clicked.connect(self._start_project_rescan)
        authors_form.addRow(self.suggest_remember_author)
        authors_form.addRow(self.project_rescan_button)
        authors_form.addRow(self.project_rescan_status)
        content_layout.addWidget(authors_group)

        shorts_group = QGroupBox("Shorts")
        shorts_form = QFormLayout(shorts_group)
        self.auto_shorts_project_folder = QCheckBox("Автоматически выбирать папку проекта Shorts")
        self.auto_shorts_project_folder.setChecked(bool(settings.get("auto_shorts_project_folder", True)))
        self.auto_shorts_project_folder.setToolTip(
            "Для проекта Creator Assistant используется папка Shorts; для отдельного видео — <название> Shorts."
        )
        shorts_form.addRow(self.auto_shorts_project_folder)
        ai = settings.get("shorts_ai", {})
        self.shorts_ai_enabled = QCheckBox("Использовать локальную AI-оценку кандидатов")
        self.shorts_ai_enabled.setChecked(bool(ai.get("enabled", True)))
        self.shorts_ai_backend = QComboBox()
        self.shorts_ai_backend.addItem("Ollama (локально)", "ollama")
        self.shorts_ai_backend.addItem("Отключено", "disabled")
        self.shorts_ai_backend.setCurrentIndex(max(0, self.shorts_ai_backend.findData(ai.get("backend", "ollama"))))
        self.shorts_ai_endpoint = QLineEdit(str(ai.get("endpoint", "http://127.0.0.1:11434")))
        self.shorts_ai_model = QLineEdit(str(ai.get("model", "qwen3:14b")))
        self.shorts_ai_mode = QComboBox()
        for label, value in (("Быстро", "fast"), ("Сбалансированно", "balanced"), ("Качество", "quality")):
            self.shorts_ai_mode.addItem(label, value)
        self.shorts_ai_mode.setCurrentIndex(max(0, self.shorts_ai_mode.findData(ai.get("mode", "balanced"))))
        self.shorts_ai_preliminary = QSpinBox()
        self.shorts_ai_preliminary.setRange(10, 80)
        self.shorts_ai_preliminary.setValue(int(ai.get("preliminary_count", 40)))
        self.shorts_ai_final = QSpinBox()
        self.shorts_ai_final.setRange(1, 20)
        self.shorts_ai_final.setValue(int(ai.get("final_count", 5)))
        self.shorts_ai_timeout = QSpinBox()
        self.shorts_ai_timeout.setRange(10, 900)
        self.shorts_ai_timeout.setSuffix(" сек")
        self.shorts_ai_timeout.setValue(int(ai.get("timeout", 180)))
        self.shorts_ai_fallback = QCheckBox("Fallback на эвристику при ошибке")
        self.shorts_ai_fallback.setChecked(bool(ai.get("fallback", True)))
        self.shorts_ai_cache = QCheckBox("Кэшировать смысловую оценку")
        self.shorts_ai_cache.setChecked(bool(ai.get("cache", True)))
        self.shorts_ai_global = QCheckBox("Глобально сравнивать финалистов")
        self.shorts_ai_global.setChecked(bool(ai.get("global_comparison", True)))
        self.shorts_ai_show_reasons = QCheckBox("Показывать объяснения AI")
        self.shorts_ai_show_reasons.setChecked(bool(ai.get("show_reasons", True)))
        self.shorts_ai_status = QLabel("Проверка не выполнена")
        self.shorts_ai_status.setWordWrap(True)
        self.shorts_ai_test = QPushButton("Проверить Ollama и модель")
        self.shorts_ai_test.clicked.connect(self._test_shorts_ai)
        shorts_form.addRow(self.shorts_ai_enabled)
        shorts_form.addRow("Backend", self.shorts_ai_backend)
        shorts_form.addRow("API", self.shorts_ai_endpoint)
        shorts_form.addRow("Модель", self.shorts_ai_model)
        shorts_form.addRow("Режим", self.shorts_ai_mode)
        shorts_form.addRow("Предварительных", self.shorts_ai_preliminary)
        shorts_form.addRow("Финальных", self.shorts_ai_final)
        shorts_form.addRow("Timeout", self.shorts_ai_timeout)
        shorts_form.addRow(self.shorts_ai_fallback)
        shorts_form.addRow(self.shorts_ai_cache)
        shorts_form.addRow(self.shorts_ai_global)
        shorts_form.addRow(self.shorts_ai_show_reasons)
        shorts_form.addRow(self.shorts_ai_test, self.shorts_ai_status)
        content_layout.addWidget(shorts_group)

        access_group = QGroupBox("Доступ к YouTube")
        access_form = QFormLayout(access_group)
        access = settings.get("youtube_access", {})
        self.youtube_auth_mode = QComboBox()
        self.youtube_auth_mode.addItem("Автоматический", "automatic")
        self.youtube_auth_mode.addItem("Без авторизации", "none")
        self.youtube_auth_mode.addItem("Cookies Firefox", "firefox")
        self.youtube_auth_mode.addItem("Cookies Chrome", "chrome")
        self.youtube_auth_mode.addItem("Файл cookies.txt", "cookies_file")
        configured_mode = str(access.get("mode", "automatic"))
        if configured_mode == "browser":
            configured_mode = str(access.get("browser", "firefox"))
        self.youtube_auth_mode.setCurrentIndex(max(0, self.youtube_auth_mode.findData(configured_mode)))
        self.youtube_browser = QComboBox()
        for value, label in BROWSERS.items():
            self.youtube_browser.addItem(label, value)
        self.youtube_browser.setCurrentIndex(max(0, self.youtube_browser.findData(access.get("browser", "chrome"))))
        self.youtube_profile = QLineEdit(str(access.get("browser_profile", "")))
        self.youtube_profile.setPlaceholderText("Default, Profile 1, Profile 2…")
        self.youtube_cookies_file = QLineEdit(str(access.get("cookies_file", "")))
        cookies_button = QPushButton("Обзор…")
        cookies_button.clicked.connect(self._browse_cookies_file)
        cookies_row = QHBoxLayout()
        cookies_row.addWidget(self.youtube_cookies_file, 1)
        cookies_row.addWidget(cookies_button)
        self.youtube_always_use = QCheckBox("Всегда использовать выбранную авторизацию")
        self.youtube_always_use.setChecked(bool(access.get("always_use", False)))
        warning = QLabel("Файл cookies может предоставлять доступ к вашим аккаунтам. Не передавайте его другим людям.")
        warning.setWordWrap(True)
        warning.setProperty("class", "muted")
        access_form.addRow("Режим", self.youtube_auth_mode)
        access_form.addRow("Браузер", self.youtube_browser)
        access_form.addRow("Профиль браузера", self.youtube_profile)
        access_form.addRow("Файл cookies.txt", cookies_row)
        access_form.addRow(self.youtube_always_use)
        access_form.addRow(warning)
        access_actions = QHBoxLayout()
        anonymous_test = QPushButton("Проверить анонимный доступ")
        cookies_test = QPushButton("Проверить браузерные cookies")
        po_test = QPushButton("Проверить PO Token Provider")
        reset_access = QPushButton("Сбросить настройки доступа")
        anonymous_test.clicked.connect(self._open_youtube_diagnostics)
        cookies_test.clicked.connect(self._open_youtube_diagnostics)
        po_test.clicked.connect(self._open_youtube_diagnostics)
        reset_access.clicked.connect(self._reset_youtube_access)
        for button in (anonymous_test, cookies_test, po_test, reset_access):
            access_actions.addWidget(button)
        access_form.addRow(access_actions)
        self.youtube_auth_mode.currentIndexChanged.connect(self._sync_youtube_access)
        self._sync_youtube_access()
        content_layout.addWidget(access_group)

        options_group = QGroupBox("Обработка")
        options_form = QFormLayout(options_group)
        self.use_gpu = QCheckBox("Использовать GPU для UVR")
        self.use_gpu.setChecked(bool(settings.get("use_gpu", True)))
        self.proxy_height = QComboBox()
        for height in (480, 720, 1080):
            self.proxy_height.addItem(f"{height}p", height)
        self.proxy_height.setCurrentIndex(max(0, self.proxy_height.findData(int(settings.get("reaper_proxy_height", 720)))))
        self.nvenc = QCheckBox("Предпочитать NVIDIA NVENC для прокси")
        self.nvenc.setChecked(bool(settings.get("prefer_nvenc", True)))
        self.open_folder = QCheckBox("Открывать папку после завершения")
        self.open_folder.setChecked(bool(settings.get("open_folder_after_completion", True)))
        self.initial_audio = QComboBox()
        self.initial_audio.addItem("Оригинальный звук видео", "original")
        self.initial_audio.addItem("Инструментал", "instrumental")
        self.initial_audio.addItem("Оба", "both")
        index = self.initial_audio.findData(settings.get("reaper_initial_audio", "original"))
        self.initial_audio.setCurrentIndex(max(index, 0))
        options_form.addRow(self.use_gpu)
        options_form.addRow("Качество прокси REAPER", self.proxy_height)
        options_form.addRow(self.nvenc)
        options_form.addRow(self.open_folder)
        options_form.addRow("Звук при первом открытии RPP", self.initial_audio)
        content_layout.addWidget(options_group)

        whisper_group = QGroupBox("Распознавание речи")
        whisper_form = QFormLayout(whisper_group)
        self.whisper_backend = QComboBox()
        for label, value in (
            ("Автоматически", "auto"), ("Найденный локальный Whisper", "existing"),
            ("Управляемый Whisper", "managed"), ("Faster Whisper", "faster"), ("Отключено", "disabled"),
        ):
            self.whisper_backend.addItem(label, value)
        self.whisper_backend.setCurrentIndex(max(0, self.whisper_backend.findData(settings.get("whisper_backend", "auto"))))
        self.whisper_model = QComboBox()
        models_dir = Path(str(settings.get("whisper_model_dir") or (Path.home() / ".cache" / "whisper")))
        discovered_models = [item.stem for item in models_dir.glob("*.pt")]
        configured_model = str(settings.get("whisper_model", "large-v3-turbo"))
        for model in dict.fromkeys(discovered_models + [configured_model, "turbo", "medium", "small"]):
            self.whisper_model.addItem(model, model)
        self.whisper_model.setCurrentIndex(max(0, self.whisper_model.findData(configured_model)))
        self.whisper_language = QComboBox()
        for label, value in (("Русский", "ru"), ("Английский", "en"), ("Автоматически", "auto")):
            self.whisper_language.addItem(label, value)
        self.whisper_language.setCurrentIndex(max(0, self.whisper_language.findData(settings.get("whisper_language", "ru"))))
        self.whisper_device = QComboBox()
        for label, value in (("Автоматически", "auto"), ("GPU", "gpu"), ("CPU", "cpu")):
            self.whisper_device.addItem(label, value)
        self.whisper_device.setCurrentIndex(max(0, self.whisper_device.findData(settings.get("whisper_device", "auto"))))
        self.whisper_profile = QComboBox()
        for label, value in (("Быстро", "fast"), ("Сбалансированно", "balanced"), ("Максимальная точность", "accurate")):
            self.whisper_profile.addItem(label, value)
        self.whisper_profile.setCurrentIndex(max(0, self.whisper_profile.findData(settings.get("whisper_profile", "balanced"))))
        self.whisper_use_gpu = QCheckBox("Использовать GPU")
        self.whisper_use_gpu.setChecked(bool(settings.get("whisper_use_gpu", True)))
        self.whisper_fp16 = QCheckBox("Использовать FP16, если поддерживается")
        self.whisper_fp16.setChecked(bool(settings.get("whisper_fp16", True)))
        self.whisper_words = QCheckBox("Получать word timestamps")
        self.whisper_words.setChecked(bool(settings.get("whisper_word_timestamps", True)))
        self.whisper_dictionary_enabled = QCheckBox("Использовать пользовательский словарь")
        self.whisper_dictionary_enabled.setChecked(bool(settings.get("whisper_use_dictionary", True)))
        self.whisper_dictionary = QPlainTextEdit("\n".join(settings.get("whisper_dictionary", [])))
        self.whisper_dictionary.setMaximumHeight(100)
        self.whisper_status = QLabel("Модели: " + str(models_dir))
        self.whisper_status.setWordWrap(True)
        whisper_form.addRow("Backend", self.whisper_backend)
        whisper_form.addRow("Модель", self.whisper_model)
        whisper_form.addRow("Язык", self.whisper_language)
        whisper_form.addRow("Устройство", self.whisper_device)
        whisper_form.addRow("Профиль", self.whisper_profile)
        whisper_form.addRow(self.whisper_use_gpu)
        whisper_form.addRow(self.whisper_fp16)
        whisper_form.addRow(self.whisper_words)
        whisper_form.addRow(self.whisper_dictionary_enabled)
        whisper_form.addRow("Словарь (по одному термину)", self.whisper_dictionary)
        whisper_form.addRow(self.whisper_status)
        whisper_actions = QHBoxLayout()
        self.whisper_find_button = QPushButton("Найти Whisper")
        self.whisper_test_button = QPushButton("Проверить Whisper")
        self.whisper_models_button = QPushButton("Открыть модели")
        self.whisper_install_button = QPushButton("Установить управляемый backend")
        self.whisper_cache_button = QPushButton("Очистить временный кэш")
        self.whisper_find_button.clicked.connect(self._find_whisper)
        self.whisper_test_button.clicked.connect(self._open_whisper_diagnostics)
        self.whisper_models_button.clicked.connect(self._open_whisper_models)
        self.whisper_install_button.clicked.connect(self._managed_whisper_info)
        self.whisper_cache_button.clicked.connect(self._clear_whisper_temp)
        for button in (self.whisper_find_button, self.whisper_test_button, self.whisper_models_button, self.whisper_install_button, self.whisper_cache_button):
            whisper_actions.addWidget(button)
        whisper_form.addRow(whisper_actions)
        content_layout.addWidget(whisper_group)

        naming_group = QGroupBox("Шаблоны имён")
        naming_form = QFormLayout(naming_group)
        naming = settings.get("naming", {})
        for key, label in (
            ("maximum", "Максимальное видео"),
            ("proxy", "Видео-прокси"),
            ("audio", "Оригинальное аудио"),
            ("instrumental", "Инструментал"),
            ("preview", "Превью"),
        ):
            edit = QLineEdit(str(naming.get(key, "")))
            naming_form.addRow(label, edit)
            self.naming_edits[key] = edit
        hint = QLabel("Доступны {title}, {height} и {proxy_height}.")
        hint.setProperty("class", "muted")
        naming_form.addRow(hint)
        content_layout.addWidget(naming_group)
        content_layout.addStretch(1)
        self.scroll_area.setWidget(content)
        root_layout.addWidget(self.scroll_area, 1)
        self.buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        self.buttons.setObjectName("settingsButtons")
        self.buttons.button(QDialogButtonBox.Save).setText("Сохранить")
        self.buttons.button(QDialogButtonBox.Cancel).setText("Отмена")
        self.buttons.accepted.connect(self._save)
        self.buttons.rejected.connect(self.reject)
        root_layout.addWidget(self.buttons, 0)
        self._fit_to_screen()

    def _browse(self, edit: QLineEdit, directory: bool) -> None:
        current = edit.text() or str(Path.home())
        if directory:
            selected = QFileDialog.getExistingDirectory(self, "Выберите папку", current)
        else:
            selected, _ = QFileDialog.getOpenFileName(self, "Выберите программу", current, "Программы (*.exe);;Все файлы (*)")
        if selected:
            self._show_full_path(edit, selected)
            if edit is self.vegas_edit:
                self._update_vegas_status()

    def _update_vegas_status(self) -> None:
        path = Path(self.vegas_edit.text().strip())
        if not path.is_file():
            self.vegas_version.setText("VEGAS не найден")
            return
        api = path.with_name("ScriptPortal.Vegas.dll")
        version = ""
        if self.container is not None:
            version = self.container.detector._windows_file_version(path)
        api_status = "Script API найден" if api.is_file() else "Script API не найден"
        self.vegas_version.setText(f"{version or 'версия не определена'}; {api_status}")

    def _find_vegas(self) -> None:
        if self.container is None:
            return
        candidate = dict(self.result_settings)
        candidate["vegas_path"] = self.vegas_edit.text().strip()
        resolution = self.container.detector.discover(candidate).get("vegas")
        if resolution and resolution.path:
            self._show_full_path(self.vegas_edit, resolution.path)
            self._update_vegas_status()
            QMessageBox.information(self, "VEGAS Pro", "Найден VEGAS Pro:\n" + resolution.path)
        else:
            QMessageBox.warning(self, "VEGAS Pro", "VEGAS Pro не найден автоматически.")

    def _verify_vegas(self) -> None:
        self._update_vegas_status()
        path = Path(self.vegas_edit.text().strip())
        if path.is_file() and path.with_name("ScriptPortal.Vegas.dll").is_file():
            QMessageBox.information(self, "VEGAS Pro", "VEGAS Pro и официальный ScriptPortal API доступны.\n" + self.vegas_version.text())
        else:
            QMessageBox.warning(self, "VEGAS Pro", "Укажите vegas.exe из папки, где находится ScriptPortal.Vegas.dll.")

    def _open_vegas_diagnostics(self) -> None:
        if self.container is None:
            return
        from creator_assistant.ui.diagnostics_dialog import DiagnosticsDialog
        dialog = DiagnosticsDialog(self.container, self)
        dialog.exec()

    def _open_vegas_script_folder(self) -> None:
        if self.container is None:
            return
        from creator_assistant.services.vegas_service import VegasService
        service = VegasService(self.vegas_edit.text().strip())
        script = service.ensure_script()
        os.startfile(str(script.parent))

    def _browse_cookies_file(self) -> None:
        selected, _ = QFileDialog.getOpenFileName(
            self,
            "Выберите cookies.txt",
            self.youtube_cookies_file.text() or str(Path.home()),
            "Cookies (*.txt);;Все файлы (*)",
        )
        if selected:
            self._show_full_path(self.youtube_cookies_file, selected)

    def _sync_youtube_access(self) -> None:
        mode = self.youtube_auth_mode.currentData()
        is_browser = mode in BROWSERS
        if is_browser:
            self.youtube_browser.setCurrentIndex(max(0, self.youtube_browser.findData(mode)))
        self.youtube_browser.setEnabled(False)
        self.youtube_profile.setEnabled(is_browser)
        self.youtube_cookies_file.setEnabled(mode == "cookies_file")

    def _open_youtube_diagnostics(self) -> None:
        if not self.container:
            QMessageBox.information(self, "Диагностика YouTube", "Сохраните настройки и откройте диагностику приложения.")
            return
        from creator_assistant.ui.diagnostics_dialog import DiagnosticsDialog

        DiagnosticsDialog(self.container, self).exec()

    def _find_whisper(self) -> None:
        if not self.container:
            return
        from creator_assistant.services.shorts.transcription.existing_whisper import ExistingWhisperBackend
        backend = ExistingWhisperBackend(self.container.runner, self.result_settings)
        capabilities = backend.capabilities()
        self.result_settings["whisper_python"] = backend.python
        self.result_settings["whisper_model_dir"] = str(backend.model_dir)
        self.whisper_status.setText(
            f"{'Найден' if capabilities.available else 'Не найден'}: {capabilities.executable}\n"
            f"Версия: {capabilities.version or 'не определена'}; модели: {', '.join(capabilities.models) or 'нет'}"
        )

    def _open_whisper_diagnostics(self) -> None:
        if not self.container:
            return
        from creator_assistant.ui.diagnostics_dialog import DiagnosticsDialog
        dialog = DiagnosticsDialog(self.container, self)
        dialog.exec()

    def _open_whisper_models(self) -> None:
        folder = Path(str(self.result_settings.get("whisper_model_dir") or (Path.home() / ".cache" / "whisper")))
        folder.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            os.startfile(str(folder))

    def _managed_whisper_info(self) -> None:
        if not self.container:
            return
        runtime = self.container.whisper_runtime
        if runtime.is_ready():
            QMessageBox.information(
                self, "Управляемый Whisper",
                f"Изолированный runtime уже установлен:\n{runtime.python}\n\nМодели: {runtime.models}",
            )
            return
        runtime.prepare_directories()
        answer = QMessageBox.question(
            self, "Установить управляемый Whisper",
            "Будет создан отдельный Python runtime и установлен openai-whisper. Системный Python и .venv Creator Assistant не изменяются. "
            "Операция может скачать крупные ML-пакеты. Уже существующая совместимая модель будет использована без повторной загрузки.\n\n"
            f"Runtime: {runtime.root}\nМодели: {runtime.models}\n\nПродолжить?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        from creator_assistant.services.shorts.transcription.existing_whisper import find_existing_python
        commands = runtime.install_commands(find_existing_python())
        self.whisper_install_button.setEnabled(False)
        self.whisper_install_button.setText("Установка…")
        thread = QThread(self)

        def work(progress):
            for index, command in enumerate(commands, 1):
                progress(f"Шаг {index}/{len(commands)}: {' '.join(command[:3])}")
                self.container.runner.run(command, timeout=60 * 60)
            return str(runtime.python)

        worker = FunctionWorker(work)
        terminal = {"kind": "", "args": ()}

        def queue(kind, *args):
            terminal.update(kind=kind, args=args)

        def cleanup():
            if thread in self._threads:
                self._threads.remove(thread)
            bridge.deleteLater()
            self.whisper_install_button.setEnabled(True)
            self.whisper_install_button.setText("Установить управляемый backend")
            if terminal["kind"] == "finished":
                self.result_settings["whisper_backend"] = "managed"
                self.whisper_backend.setCurrentIndex(max(0, self.whisper_backend.findData("managed")))
                self.whisper_status.setText(f"Управляемый runtime установлен: {runtime.python}\nМодели: {runtime.models}")
                QMessageBox.information(self, "Управляемый Whisper", "Установка завершена. Нажмите «Сохранить», чтобы выбрать этот backend.")
            elif terminal["kind"] == "failed":
                QMessageBox.warning(self, "Управляемый Whisper", str(terminal["args"][0]))

        bridge = UiWorkerBridge({
            "finished": lambda value: queue("finished", value),
            "failed": lambda message, details: queue("failed", message, details),
            "cancelled": lambda: queue("cancelled"),
            "manual_action_required": lambda value: queue("failed", str(value), ""),
            "runtime_install_required": lambda value: queue("failed", str(value), ""),
            "authentication_required": lambda value: queue("failed", str(value), ""),
            "cookies_unavailable": lambda value: queue("failed", str(value), ""),
            "media_forbidden": lambda value: queue("failed", str(value), ""),
            "thread_finished": cleanup,
        }, parent=self)
        thread.worker = worker
        thread.bridge = bridge
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(bridge.finished)
        worker.failed.connect(bridge.failed)
        worker.cancelled.connect(bridge.cancelled)
        for signal in (
            worker.finished, worker.failed, worker.cancelled, worker.manual_action_required,
            worker.runtime_install_required, worker.authentication_required, worker.cookies_unavailable,
            worker.media_forbidden, worker.waiting_for_disk_space, worker.gpu_memory_required,
            worker.system_memory_required, worker.audio_output_missing,
        ):
            signal.connect(worker.deleteLater)
            signal.connect(thread.quit)
        thread.finished.connect(bridge.thread_finished)
        thread.finished.connect(thread.deleteLater)
        self._threads.append(thread)
        thread.start()

    def _clear_whisper_temp(self) -> None:
        if not self.container:
            return
        temp = self.container.whisper_runtime.root.parent / "temp"
        if temp.is_dir():
            import shutil
            shutil.rmtree(temp)
        QMessageBox.information(self, "Whisper", "Временный кэш Whisper очищен. Модели и расшифровки проектов сохранены.")

    def _reset_youtube_access(self) -> None:
        index = self.youtube_auth_mode.findData("automatic")
        self.youtube_auth_mode.setCurrentIndex(max(0, index))
        self.youtube_browser.setCurrentIndex(max(0, self.youtube_browser.findData("firefox")))
        self.youtube_profile.clear()
        self.youtube_cookies_file.clear()
        self.youtube_always_use.setChecked(False)
        self.result_settings["youtube_access"] = {
            "mode": "automatic",
            "browser": "",
            "browser_profile": "",
            "cookies_file": "",
            "always_use": False,
            "schema_version": 2,
        }
        if self.container:
            self.container.reset_youtube_access()
        QMessageBox.information(self, "Доступ к YouTube", "Настройки доступа сброшены. Новые публичные Job начинаются анонимно.")

    @staticmethod
    def _show_full_path(edit: QLineEdit, path: str) -> None:
        edit.setText(path)
        edit.setToolTip(path)
        edit.deselect()
        edit.setCursorPosition(0)

    def _fit_to_screen(self) -> None:
        screen = self.screen() or QApplication.primaryScreen()
        if not screen:
            self.resize(820, 700)
            return
        available = screen.availableGeometry()
        width = min(860, max(620, available.width() - 48))
        height = min(760, max(480, available.height() - 48))
        self.setMinimumSize(min(620, width), min(480, height))
        self.resize(width, height)

    def _has_missing_or_invalid_paths(self) -> bool:
        for key in ("yt_dlp_path", "ffmpeg_path", "ffprobe_path", "uvr_path", "reaper_path", "vegas_path"):
            value = str(self.result_settings.get(key, ""))
            if not value or not Path(value).is_file():
                return True
        return False

    def _start_auto_search(self, notify: bool = True) -> None:
        if not self.container or self._threads:
            return
        self._notify_auto_search = notify
        self.auto_find_button.setEnabled(False)
        self.auto_find_button.setText("Поиск программ…")
        thread = QThread(self)
        worker = FunctionWorker(lambda progress: self.container.auto_detect_dependencies())
        thread.setObjectName("settings-auto-search-thread")
        worker.setObjectName("settings-auto-search-worker")
        terminal = {"kind": "", "args": ()}

        def queue(kind, *args):
            terminal.update(kind=kind, args=args)

        def cleanup():
            if thread in self._threads:
                self._threads.remove(thread)
            bridge.deleteLater()
            kind, args = terminal["kind"], terminal["args"]
            if kind == "finished":
                self._auto_search_ready(*args)
            elif kind == "failed":
                self._auto_search_failed(*args)
            elif kind == "cancelled":
                self._auto_search_failed("Поиск отменён", "")
            elif kind:
                self._auto_search_failed(str(args[0]) if args else "Операция прервана", "")

        bridge = UiWorkerBridge(
            {
                "finished": lambda value: queue("finished", value),
                "failed": lambda message, details: queue("failed", message, details),
                "cancelled": lambda: queue("cancelled"),
                "manual_action_required": lambda value: queue("manual_action_required", value),
                "runtime_install_required": lambda value: queue("runtime_install_required", value),
                "authentication_required": lambda value: queue("authentication_required", value),
                "cookies_unavailable": lambda value: queue("cookies_unavailable", value),
                "media_forbidden": lambda value: queue("media_forbidden", value),
                "thread_finished": cleanup,
            },
            parent=self,
        )
        thread.worker = worker
        thread.bridge = bridge
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(bridge.finished)
        worker.failed.connect(bridge.failed)
        worker.cancelled.connect(bridge.cancelled)
        worker.manual_action_required.connect(bridge.manual_action_required)
        worker.runtime_install_required.connect(bridge.runtime_install_required)
        worker.authentication_required.connect(bridge.authentication_required)
        worker.cookies_unavailable.connect(bridge.cookies_unavailable)
        worker.media_forbidden.connect(bridge.media_forbidden)
        for signal in (
            worker.finished, worker.failed, worker.cancelled, worker.manual_action_required,
            worker.runtime_install_required, worker.authentication_required,
            worker.cookies_unavailable, worker.media_forbidden,
            worker.waiting_for_disk_space, worker.gpu_memory_required,
            worker.system_memory_required, worker.audio_output_missing,
        ):
            signal.connect(worker.deleteLater)
            signal.connect(thread.quit)
        thread.finished.connect(bridge.thread_finished)
        thread.finished.connect(thread.deleteLater)
        self._threads.append(thread)
        thread.start()

    def _start_project_rescan(self) -> None:
        if not self.container or self._threads:
            return
        index = getattr(getattr(self.container, "projects", None), "project_index", None)
        if index is None:
            self.project_rescan_status.setText("Индекс проектов недоступен")
            return
        folders = discover_author_folders(
            Path(self.result_settings.get("youtube_root", r"E:\YouTube")),
            self.result_settings.get("author_paths", []),
        )
        presets = merge_presets(self.result_settings.get("author_presets", []), folders)
        roots = [ProjectRoot(Path(item.root_path), item.preset_id, item.display_name) for item in presets]
        self.project_rescan_button.setEnabled(False)
        self.project_rescan_status.setText("Сканирование настроенных папок в фоне…")
        thread = QThread(self)
        worker = FunctionWorker(lambda _progress: index.rescan(roots))
        terminal = {}

        def cleanup():
            if thread in self._threads:
                self._threads.remove(thread)
            bridge.deleteLater()
            self.project_rescan_button.setEnabled(True)
            if "result" in terminal:
                result = terminal["result"]
                active = sum(1 for item in result.get("projects", []) if not item.get("stale"))
                warnings = result.get("warnings", [])
                suffix = ("\n" + "\n".join(warnings)) if warnings else ""
                self.project_rescan_status.setText(f"Найдено проектов: {active}{suffix}")
            elif "error" in terminal:
                self.project_rescan_status.setText("Ошибка пересканирования: " + str(terminal["error"][0]))

        bridge = UiWorkerBridge(
            {
                "finished": lambda result: terminal.update(result=result),
                "failed": lambda message, details: terminal.update(error=(message, details)),
                "thread_finished": cleanup,
            },
            parent=self,
        )
        thread.worker = worker
        thread.bridge = bridge
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(bridge.finished)
        worker.failed.connect(bridge.failed)
        for signal in (worker.finished, worker.failed):
            signal.connect(worker.deleteLater)
            signal.connect(thread.quit)
        thread.finished.connect(bridge.thread_finished)
        thread.finished.connect(thread.deleteLater)
        self._threads.append(thread)
        thread.start()

    def done(self, result: int) -> None:
        if self._threads:
            try:
                self.container.runner.cancel_active()
            except Exception:
                pass
            for thread in list(self._threads):
                thread.requestInterruption()
                thread.quit()
            if any(thread.isRunning() and not thread.wait(3000) for thread in list(self._threads)):
                self.auto_find_button.setText("Завершаю поиск…")
                return
        super().done(result)

    def _auto_search_ready(self, _resolutions) -> None:
        self.auto_find_button.setEnabled(True)
        self.auto_find_button.setText("Найти программы автоматически")
        self.result_settings = deepcopy(self.container.settings)
        found = []
        labels = {
            "yt_dlp_path": "yt-dlp",
            "ffmpeg_path": "FFmpeg",
            "ffprobe_path": "FFprobe",
            "uvr_path": "UVR",
            "reaper_path": "REAPER",
            "vegas_path": "VEGAS",
        }
        for key, label in labels.items():
            value = str(self.result_settings.get(key, ""))
            self._show_full_path(self.path_edits[key], value)
            found.append(f"{label}: {value or 'не найден'}")
        if self._notify_auto_search:
            QMessageBox.information(self, "Автоматический поиск", "\n".join(found))

    def _auto_search_failed(self, message: str, _details: str) -> None:
        self.auto_find_button.setEnabled(True)
        self.auto_find_button.setText("Найти программы автоматически")
        QMessageBox.warning(self, "Автоматический поиск", message)

    def _test_shorts_ai(self) -> None:
        self.shorts_ai_test.setEnabled(False)
        self.shorts_ai_status.setText("Проверяю локальный API…")
        QApplication.processEvents()
        try:
            backend = OllamaSemanticScorer(
                endpoint=self.shorts_ai_endpoint.text().strip(),
                model=self.shorts_ai_model.text().strip(),
                timeout=min(30, self.shorts_ai_timeout.value()),
            )
            info = backend.model_info()
            size = float(info.get("size", 0) or 0) / 1024**3
            self.shorts_ai_status.setText(
                f"Готово: {info.get('name') or info.get('model')} · {size:.1f} ГБ · localhost"
            )
        except Exception as exc:
            self.shorts_ai_status.setText(f"Недоступно: {exc}")
        finally:
            self.shorts_ai_test.setEnabled(True)

    def _save(self) -> None:
        save_clicked_at = time.monotonic()
        for key, edit in self.path_edits.items():
            self.result_settings[key] = edit.text().strip()
        self.result_settings["use_gpu"] = self.use_gpu.isChecked()
        try:
            proxy_height = int(self.proxy_height.currentData())
        except (TypeError, ValueError):
            QMessageBox.warning(self, "Creator Assistant", "Качество прокси должно быть 480p, 720p или 1080p.")
            return
        if proxy_height not in {480, 720, 1080}:
            QMessageBox.warning(self, "Creator Assistant", "Качество прокси должно быть 480p, 720p или 1080p.")
            return
        self.result_settings["reaper_proxy_height"] = proxy_height
        self.result_settings["prefer_nvenc"] = self.nvenc.isChecked()
        self.result_settings["open_folder_after_completion"] = self.open_folder.isChecked()
        self.result_settings["create_vegas_project_default"] = self.create_vegas_default.isChecked()
        self.result_settings["auto_open_vegas_project"] = self.auto_open_vegas.isChecked()
        self.result_settings["suggest_remember_author"] = self.suggest_remember_author.isChecked()
        self.result_settings["auto_shorts_project_folder"] = self.auto_shorts_project_folder.isChecked()
        previous_ai = self.result_settings.get("shorts_ai", {})
        self.result_settings["shorts_ai"] = {
            "enabled": self.shorts_ai_enabled.isChecked() and self.shorts_ai_backend.currentData() != "disabled",
            "backend": self.shorts_ai_backend.currentData(),
            "endpoint": self.shorts_ai_endpoint.text().strip(),
            "model": self.shorts_ai_model.text().strip(),
            "mode": self.shorts_ai_mode.currentData(),
            "preliminary_count": self.shorts_ai_preliminary.value(),
            "final_count": self.shorts_ai_final.value(),
            "timeout": self.shorts_ai_timeout.value(),
            "fallback": self.shorts_ai_fallback.isChecked(),
            "cache": self.shorts_ai_cache.isChecked(),
            "show_reasons": self.shorts_ai_show_reasons.isChecked(),
            "global_comparison": self.shorts_ai_global.isChecked(),
            "weights": previous_ai.get("weights", {"semantic": 0.55, "heuristic": 0.25, "activity": 0.15, "uniqueness": 0.05}),
        }
        self.result_settings["reaper_initial_audio"] = self.initial_audio.currentData()
        self.result_settings["whisper_backend"] = self.whisper_backend.currentData()
        self.result_settings["whisper_model"] = self.whisper_model.currentData()
        self.result_settings["whisper_language"] = self.whisper_language.currentData()
        self.result_settings["whisper_device"] = self.whisper_device.currentData()
        self.result_settings["whisper_profile"] = self.whisper_profile.currentData()
        self.result_settings["whisper_use_gpu"] = self.whisper_use_gpu.isChecked()
        self.result_settings["whisper_fp16"] = self.whisper_fp16.isChecked()
        self.result_settings["whisper_word_timestamps"] = self.whisper_words.isChecked()
        self.result_settings["whisper_use_dictionary"] = self.whisper_dictionary_enabled.isChecked()
        self.result_settings["whisper_dictionary"] = [line.strip() for line in self.whisper_dictionary.toPlainText().splitlines() if line.strip()]
        selected_access_mode = str(self.youtube_auth_mode.currentData())
        stored_mode = "browser" if selected_access_mode in BROWSERS else selected_access_mode
        stored_browser = selected_access_mode if selected_access_mode in BROWSERS else ""
        self.result_settings["youtube_access"] = {
            "mode": stored_mode,
            "browser": stored_browser,
            "browser_profile": self.youtube_profile.text().strip(),
            "cookies_file": self.youtube_cookies_file.text().strip(),
            "always_use": self.youtube_always_use.isChecked() and stored_mode in {"browser", "cookies_file"},
            "schema_version": 2,
        }
        self.result_settings["naming"] = {key: edit.text().strip() for key, edit in self.naming_edits.items()}
        if self.container:
            self.container.save_settings(self.result_settings, started_at=save_clicked_at)
        self.accept()
