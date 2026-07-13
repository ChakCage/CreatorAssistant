from __future__ import annotations

import datetime as dt
import math
import re
import struct
import wave
import shutil
import time
from copy import deepcopy
from pathlib import Path
from typing import Dict

from PySide6.QtCore import QThread
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from creator_assistant.app import ServiceContainer
from creator_assistant.domain.job import CancellationToken
from creator_assistant.domain.models import DependencyInfo, ProgressInfo
from creator_assistant.infrastructure.settings_store import local_data_root
from creator_assistant.ui.widgets.error_dialog import ErrorDialog
from creator_assistant.ui.workers import FunctionWorker, UiWorkerBridge
from creator_assistant.infrastructure.crash_logging import event as crash_event
from creator_assistant.domain.youtube_auth import BROWSERS, find_browser


PATH_KEYS = {
    "yt_dlp": "yt_dlp_path",
    "ffmpeg": "ffmpeg_path",
    "ffprobe": "ffprobe_path",
    "uvr": "uvr_path",
    "reaper": "reaper_path",
    "vegas": "vegas_path",
}


class DiagnosticsDialog(QDialog):
    def __init__(self, container: ServiceContainer, parent=None) -> None:
        super().__init__(parent)
        self.container = container
        self._threads: list[QThread] = []
        self.setWindowTitle("Диагностика зависимостей")
        self.resize(940, 570)
        layout = QVBoxLayout(self)
        self.summary = QLabel("Нажмите «Проверить», чтобы обновить сведения.")
        layout.addWidget(self.summary)
        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(("Компонент", "Статус", "Версия", "Найден", "Источник", "Подробности", ""))
        self.table.horizontalHeader().setStretchLastSection(False)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(5, QHeaderView.Stretch)
        self.table.setAlternatingRowColors(True)
        layout.addWidget(self.table, 1)
        actions = QHBoxLayout()
        self.check_button = QPushButton("Проверить")
        self.update_button = QPushButton("Обновить yt-dlp сейчас")
        self.uvr_test_button = QPushButton("Проверить автоматическое разделение")
        self.vegas_test_button = QPushButton("Проверить создание VEGAS-проекта")
        self.youtube_test_button = QPushButton("Проверить доступ к YouTube")
        self.shorts_test_button = QPushButton("Проверить модуль Shorts")
        close_button = QPushButton("Закрыть")
        self.check_button.clicked.connect(self.run_diagnostics)
        self.update_button.clicked.connect(self.update_yt_dlp)
        self.uvr_test_button.clicked.connect(self.test_uvr_backend)
        self.vegas_test_button.clicked.connect(self.test_vegas_project)
        self.youtube_test_button.clicked.connect(self.test_youtube_access)
        self.shorts_test_button.clicked.connect(self.test_shorts_module)
        close_button.clicked.connect(self.accept)
        actions.addWidget(self.check_button)
        actions.addWidget(self.update_button)
        actions.addWidget(self.uvr_test_button)
        actions.addWidget(self.vegas_test_button)
        actions.addWidget(self.youtube_test_button)
        actions.addWidget(self.shorts_test_button)
        actions.addStretch(1)
        actions.addWidget(close_button)
        layout.addLayout(actions)
        self.run_diagnostics()

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
                self.summary.setText("Завершаю диагностику…")
                return
        super().done(result)

    def _start(self, function, finished) -> None:
        thread = QThread(self)
        worker = FunctionWorker(function)
        thread.setObjectName("diagnostics-thread")
        worker.setObjectName("diagnostics-worker")
        terminal = {"kind": "", "args": ()}

        def queue(kind, *args):
            terminal.update(kind=kind, args=args)

        def enable_buttons():
            self.check_button.setEnabled(True)
            self.update_button.setEnabled(True)
            self.uvr_test_button.setEnabled(True)
            self.vegas_test_button.setEnabled(True)
            self.youtube_test_button.setEnabled(True)
            self.shorts_test_button.setEnabled(True)

        def cleanup():
            if thread in self._threads:
                self._threads.remove(thread)
            bridge.deleteLater()
            crash_event("Diagnostics QThread finished")
            kind, args = terminal["kind"], terminal["args"]
            if kind == "finished":
                finished(*args)
            elif kind == "failed":
                enable_buttons()
                ErrorDialog(args[0], args[1], self).exec()
            elif kind:
                enable_buttons()
                if args:
                    request = args[0]
                    ErrorDialog(str(request), getattr(request, "stderr", ""), self).exec()

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

    def run_diagnostics(self) -> None:
        self.check_button.setEnabled(False)
        self.summary.setText("Выполняется диагностика…")
        self._start(lambda progress: self._diagnostic_work(), self._show_results)

    def _diagnostic_work(self):
        resolutions = self.container.auto_detect_dependencies()
        items = self.container.detector.diagnose(self.container.settings, resolutions)
        runtime = self.container.audio_separator_runtime
        auth = self.container.youtube_auth
        browser_found = find_browser(auth.browser) if auth.mode == "browser" else None
        auth_details = f"Режим: {auth.summary}"
        if auth.mode == "browser":
            auth_details += f"; браузер: {BROWSERS.get(auth.browser, auth.browser)}; найден: {'да' if browser_found else 'нет'}"
        try:
            auth.arguments(validate=True)
            auth_status = "ok"
        except Exception as exc:
            auth_status = "error"
            auth_details += "; " + str(exc)
        items.append(DependencyInfo("youtube_access", "YouTube access", auth_status, str(browser_found or ""), "", auth_details, "настройки Creator Assistant"))
        yt_dlp_path = self.container.paths.get("yt_dlp", "")
        plugins_root = local_data_root() / "tools" / "yt-dlp-plugins"
        po_status = "не установлен"
        po_details = f"Управляемая папка: {plugins_root}"
        if yt_dlp_path:
            try:
                probe = self.container.runner.run([yt_dlp_path, "--ignore-config", "--verbose", "--version"], timeout=20)
                lower = probe.output.casefold()
                if "po token providers: none" in lower:
                    po_status = "не установлен"
                elif "po token providers:" in lower:
                    po_status = "работает"
                    po_details += "; yt-dlp обнаружил PO Token Provider"
                elif plugins_root.is_dir() and any(plugins_root.iterdir()):
                    po_status = "установлен"
                    po_details += "; файлы provider найдены, но yt-dlp его не подтвердил"
            except Exception as exc:
                po_status = "ошибка"
                po_details += "; " + str(exc)
        items.append(DependencyInfo(
            "youtube_po_token",
            "YouTube PO Token Provider",
            "ok" if po_status == "работает" else ("error" if po_status == "ошибка" else "warning"),
            str(plugins_root),
            "",
            f"Статус: {po_status}; {po_details}",
            "managed tools Creator Assistant",
        ))
        whisper = self.container.shorts_transcription_backend.capabilities()
        items.append(DependencyInfo(
            "whisper", "Whisper Shorts", "ok" if whisper.available else "error",
            whisper.executable, whisper.version,
            f"Модели: {', '.join(whisper.models) or 'не найдены'}; CUDA выбрана: {'да' if whisper.cuda else 'нет'}; word timestamps: {'да' if whisper.word_timestamps else 'нет'}; {whisper.details}",
            "локальный headless backend",
        ))
        ffmpeg_path = self.container.paths.get("ffmpeg", "")
        if ffmpeg_path:
            try:
                filters = self.container.runner.run([ffmpeg_path, "-hide_banner", "-filters"], timeout=30).output.casefold()
                required_filters = ("crop", "scale", "boxblur", "subtitles", "trim", "atrim")
                missing = [name for name in required_filters if not re.search(rf"\b{name}\b", filters)]
                filter_status = "ok" if not missing else "error"
                filter_details = "Доступны: " + ", ".join(required_filters) if not missing else "Не найдены: " + ", ".join(missing)
            except Exception as exc:
                filter_status, filter_details = "error", str(exc)
            items.append(DependencyInfo("shorts_ffmpeg", "FFmpeg Shorts filters", filter_status, ffmpeg_path, "", filter_details, "FFmpeg -filters"))
        try:
            from creator_assistant.ui.shorts.candidate_editor import MULTIMEDIA_AVAILABLE
            multimedia_status = "ok" if MULTIMEDIA_AVAILABLE else "error"
            multimedia_details = "QMediaPlayer/QAudioOutput/QVideoWidget доступны" if MULTIMEDIA_AVAILABLE else "QtMultimedia не импортирован"
        except Exception as exc:
            multimedia_status, multimedia_details = "error", str(exc)
        items.append(DependencyInfo("qt_multimedia", "QtMultimedia preview", multimedia_status, "", "PySide6", multimedia_details, "сборка приложения"))
        if runtime.is_ready():
            environment = runtime.environment_info()
            details = f"Runtime: {runtime.root}; Python: {runtime.python_executable}; {environment.get('onnxruntime', '')}"
            items.append(DependencyInfo("audio_separator", "Audio Separator — UVR MDX backend", "ok", str(runtime.python_executable), environment.get("version", "0.44.2"), details, "управляемый runtime Creator Assistant"))
            items.append(DependencyInfo("audio_separator_model", "Managed UVR model", "ok", str(runtime.model_path), runtime.sha256(runtime.model_path), f"Размер: {runtime.model_path.stat().st_size} байт", "локальная копия проверенной UVR-модели"))
        else:
            items.append(DependencyInfo("audio_separator", "Audio Separator Runtime", "error", details=f"Не установлен: {runtime.root}"))
        latest = str(self.container.settings.get("latest_yt_dlp_version", ""))
        update_error = ""
        checked = False
        if self.container.updater.check_due(self.container.settings):
            checked = True
            try:
                latest = self.container.updater.latest_version()
            except Exception as exc:
                update_error = str(exc)
        return items, latest, checked, update_error

    def _show_results(self, payload) -> None:
        items, latest, checked, update_error = payload
        if checked:
            self.container.settings["last_update_check"] = dt.date.today().isoformat()
            if latest:
                self.container.settings["latest_yt_dlp_version"] = latest
            self.container.settings_store.save(self.container.settings)
        yt_dlp = next((item for item in items if item.key == "yt_dlp"), None)
        if yt_dlp:
            if latest and latest != yt_dlp.version:
                yt_dlp.details = f"Доступно обновление: {latest}"
                if yt_dlp.status == "ok":
                    yt_dlp.status = "warning"
            elif latest:
                yt_dlp.details = "Установлена актуальная стабильная версия"
            elif update_error:
                yt_dlp.details = "Не удалось проверить обновление: " + update_error
        self.check_button.setEnabled(True)
        errors = sum(item.status == "error" for item in items)
        warnings = sum(item.status == "warning" for item in items)
        self.summary.setText(f"Проверка завершена: ошибок — {errors}, предупреждений — {warnings}.")
        self.table.setRowCount(len(items))
        colors = {"ok": QColor("#56d58b"), "warning": QColor("#f2c66d"), "error": QColor("#ff7b82")}
        labels = {"ok": "Найден", "warning": "Предупреждение", "error": "Ошибка"}
        for row, item in enumerate(items):
            values = (item.name, labels.get(item.status, item.status), item.version, item.path, item.source, item.details)
            for column, value in enumerate(values):
                cell = QTableWidgetItem(str(value))
                if column == 1:
                    cell.setForeground(colors.get(item.status, QColor("white")))
                self.table.setItem(row, column, cell)
            if item.key in PATH_KEYS:
                button = QPushButton("Выбрать…")
                button.clicked.connect(lambda checked=False, k=item.key: self._choose_path(k))
                self.table.setCellWidget(row, 6, button)

    def _choose_path(self, key: str) -> None:
        selected, _ = QFileDialog.getOpenFileName(self, "Выберите программу", "", "Программы (*.exe);;Все файлы (*)")
        if selected:
            updated = deepcopy(self.container.settings)
            updated[PATH_KEYS[key]] = selected
            self.container.save_settings(updated)
            self.run_diagnostics()

    def update_yt_dlp(self) -> None:
        executable = self.container.paths.get("yt_dlp", "")
        if not executable:
            QMessageBox.warning(self, "yt-dlp", "Сначала укажите путь к yt-dlp.")
            return
        token = CancellationToken()
        self.update_button.setEnabled(False)

        def done(output) -> None:
            self.update_button.setEnabled(True)
            self.container.settings["last_update_check"] = dt.date.today().isoformat()
            self.container.save_settings(self.container.settings)
            QMessageBox.information(self, "yt-dlp", "Обновление завершено.\n\n" + str(output)[-1200:])
            self.run_diagnostics()

        self._start(lambda progress: self.container.updater.update(executable, token), done)

    def test_uvr_backend(self) -> None:
        if not self.container.projects.direct_separator.available():
            QMessageBox.information(
                self,
                "Audio Separator backend",
                "Managed Audio Separator Runtime ещё не установлен. Он будет предложен при запуске проекта.",
            )
            return
        self.uvr_test_button.setEnabled(False)
        self.summary.setText("Выполняется автоматический тест Audio Separator…")
        token = CancellationToken()

        def work(progress):
            test_dir = local_data_root() / "jobs" / "diagnostics" / "temp"
            test_dir.mkdir(parents=True, exist_ok=True)
            source = test_dir / "diagnostic_input.wav"
            output = test_dir / "diagnostic_instrumental.flac"
            source.unlink(missing_ok=True)
            output.unlink(missing_ok=True)
            sample_rate = 48000
            try:
                with wave.open(str(source), "wb") as stream:
                    stream.setnchannels(2)
                    stream.setsampwidth(2)
                    stream.setframerate(sample_rate)
                    for index in range(sample_rate * 3):
                        left = int(7000 * math.sin(2 * math.pi * 220 * index / sample_rate))
                        right = int(7000 * math.sin(2 * math.pi * 330 * index / sample_rate))
                        stream.writeframesraw(struct.pack("<hh", left, right))
                separator = self.container.projects.direct_separator
                configure_job = getattr(separator, "configure_job", None)
                if configure_job:
                    configure_job("diagnostics")
                result = separator.separate(
                    source,
                    output,
                    token,
                    lambda message: progress(ProgressInfo("stem_separation", message)),
                )
                probe = self.container.projects.validator.validate_expected_audio(
                    result,
                    token,
                    duration=3.0,
                    require_flac=True,
                )
                audio = next(item for item in probe.get("streams", []) if item.get("codec_type") == "audio")
                return f"FLAC проверен: {audio.get('sample_rate')} Hz, {audio.get('channels')} канала. Тестовые файлы удалены."
            finally:
                source.unlink(missing_ok=True)
                output.unlink(missing_ok=True)

        def done(message):
            self.uvr_test_button.setEnabled(True)
            self.summary.setText("Автоматический тест разделения завершён успешно.")
            QMessageBox.information(self, "Audio Separator backend", str(message))

        self._start(work, done)

    def test_vegas_project(self) -> None:
        from creator_assistant.services.vegas_service import VegasService

        vegas_path = self.container.paths.get("vegas", "")
        ffmpeg_path = self.container.paths.get("ffmpeg", "")
        service = VegasService(vegas_path)
        if not service.available or not Path(ffmpeg_path).is_file():
            QMessageBox.warning(self, "VEGAS Pro", "Для теста нужны найденные VEGAS Pro, ScriptPortal.Vegas.dll и FFmpeg.")
            return
        self.vegas_test_button.setEnabled(False)
        self.summary.setText("Создаётся безопасный тестовый проект VEGAS…")
        token = CancellationToken()

        def work(progress):
            stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
            test_dir = local_data_root() / "_test_output" / "vegas" / stamp
            test_dir.mkdir(parents=True, exist_ok=False)
            video = test_dir / "Тест MAX's video.mp4"
            audio = test_dir / "Тест Instrumental.flac"
            output = test_dir / "Тест Creator Assistant.veg"
            self.container.runner.run([
                ffmpeg_path, "-hide_banner", "-y",
                "-f", "lavfi", "-i", "testsrc=size=320x180:rate=30",
                "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
                "-t", "2", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-shortest", str(video),
            ], cancellation=token, timeout=90)
            self.container.runner.run([
                ffmpeg_path, "-hide_banner", "-y", "-f", "lavfi", "-i",
                "sine=frequency=220:sample_rate=48000", "-t", "2", "-c:a", "flac", str(audio),
            ], cancellation=token, timeout=60)
            result = service.create_project(
                output=output,
                max_video=video,
                instrumental=audio,
                duration=2.0,
                temp_dir=test_dir / "job",
                cancellation=token,
                job_id="diagnostics-" + stamp,
            )
            return result, test_dir

        def done(payload):
            result, test_dir = payload
            self.vegas_test_button.setEnabled(True)
            self.summary.setText("Тест VEGAS завершён: 1 video track, 1 audio track, звук MAX не добавлен.")
            QMessageBox.information(
                self,
                "VEGAS Pro",
                "Тестовый проект успешно создан и проверен.\n\n"
                f"Видео: {result.video_media_path}\n"
                f"Аудио: {result.audio_media_path}\n"
                f"Проект: {result.path}\n\n"
                f"Тестовые файлы оставлены в: {test_dir}",
            )

        self._start(work, done)

    def test_youtube_access(self) -> None:
        self.youtube_test_button.setEnabled(False)
        self.summary.setText("Проверяется доступ к YouTube; файлы не скачиваются…")
        token = CancellationToken()

        def done(metadata):
            self.youtube_test_button.setEnabled(True)
            self.summary.setText("Тест метаданных YouTube пройден.")
            QMessageBox.information(
                self,
                "Доступ к YouTube",
                f"Успешно получены метаданные:\n{metadata.title}\n\nРежим: {self.container.youtube_auth.summary}",
            )

        self._start(
            lambda progress: self.container.metadata_controller.request(
                "https://www.youtube.com/watch?v=x5s3GZLF_Ac",
                token,
                "diagnostics",
                on_status=lambda message: progress(ProgressInfo("metadata", message)),
                force=True,
                anonymous_first=False,
            ).metadata,
            done,
        )

    def test_shorts_module(self) -> None:
        self.shorts_test_button.setEnabled(False)
        self.summary.setText("Выполняется реальный локальный тест Whisper (10–20 секунд)…")
        token = CancellationToken()

        def work(progress):
            test_dir = local_data_root() / "jobs" / "diagnostics" / "shorts-test"
            shutil.rmtree(test_dir, ignore_errors=True)
            test_dir.mkdir(parents=True, exist_ok=True)
            source = test_dir / "whisper_test_ru.wav"
            escaped = str(source).replace("'", "''")
            script = (
                "Add-Type -AssemblyName System.Speech; "
                "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                "$s.SelectVoice('Microsoft Irina Desktop'); "
                f"$s.SetOutputToWaveFile('{escaped}'); "
                "$s.Speak('Это реальная проверка локального распознавания речи. Minecraft, редстоун и хардкор.'); "
                "$s.Dispose()"
            )
            started = time.monotonic()
            try:
                self.container.runner.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script], cancellation=token, timeout=60)
                transcript = self.container.shorts_transcription.transcribe(source, test_dir / "Analysis", token)
                elapsed = time.monotonic() - started
                if transcript.language != "ru" or not any("а" <= char.casefold() <= "я" for char in transcript.text):
                    raise RuntimeError("Whisper не подтвердил русский язык или кириллицу.")
                capabilities = self.container.shorts_transcription_backend.capabilities()
                return (
                    f"Headless Whisper работает: {len(transcript.segments)} сегм., {elapsed:.1f} с.\n"
                    f"Язык: {transcript.language}; модель: {transcript.model}; устройство: "
                    f"{'GPU/CUDA' if capabilities.cuda else 'CPU'}; word timestamps: "
                    f"{'да' if any(segment.words for segment in transcript.segments) else 'нет'}.\n\n{transcript.text}"
                )
            finally:
                shutil.rmtree(test_dir, ignore_errors=True)

        def done(message):
            self.shorts_test_button.setEnabled(True)
            self.summary.setText("Реальный тест модуля Shorts завершён успешно.")
            QMessageBox.information(self, "Диагностика Shorts", str(message))

        self._start(work, done)
