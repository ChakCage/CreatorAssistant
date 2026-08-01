from __future__ import annotations

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout

from creator_assistant.infrastructure.build_info import current_build_info
from creator_assistant.services.licensing import LicenseClientError


ERRORS = {
    "INVALID_ACTIVATION_CODE": "Код активации неверен.", "ACTIVATION_CODE_EXPIRED": "Срок кода закончился. Получите новый код.",
    "ACTIVATION_CODE_USED": "Этот код уже использован.", "ACTIVATION_CODE_REVOKED": "Код отозван.",
    "TOO_MANY_ATTEMPTS": "Слишком много попыток. Повторите позже.", "SUBSCRIPTION_INACTIVE": "Подписка не активна.",
    "DEVICE_LIMIT_REACHED": "Достигнут лимит устройств. Отключите старое устройство.",
    "SERVER_UNAVAILABLE": "Сервер лицензий временно недоступен.",
    "REQUEST_TIMEOUT": "Сервер лицензий не ответил вовремя. Проверьте подключение и повторите попытку.",
    "TLS_ERROR": "Не удалось проверить защищённое соединение с сервером лицензий.",
    "INVALID_SERVER_RESPONSE": "Сервер лицензий вернул некорректный ответ.",
}


class LicenseDialog(QDialog):
    def __init__(self, container, parent=None) -> None:
        super().__init__(parent); self.container = container; self.service = container.licensing
        self.setWindowTitle("Подписка и лицензия"); self.resize(680, 470)
        layout = QVBoxLayout(self); self.status_label = QLabel(); self.status_label.setWordWrap(True); layout.addWidget(self.status_label)
        form = QFormLayout(); self.code = QLineEdit(); self.code.setPlaceholderText("CA-XXXX-XXXX-XXXX"); self.code.setMaxLength(20)
        form.addRow("Код активации", self.code); layout.addLayout(form)
        actions = QHBoxLayout()
        for text, callback in (("Активировать", self.activate), ("Проверить подключение", self.check_connection),
                               ("Обновить статус", self.refresh), ("Мои устройства", self.show_devices),
                               ("Отключить это устройство", self.deactivate)):
            button = QPushButton(text); button.clicked.connect(callback); actions.addWidget(button)
        layout.addLayout(actions)
        links = QHBoxLayout(); telegram = QPushButton("Получить код в Telegram"); purchase = QPushButton("Купить или продлить подписку"); open_bot = QPushButton("Открыть Telegram-бота"); support = QPushButton("Открыть поддержку")
        free_access = QPushButton("Попробовать бесплатно")
        bot_url = current_build_info().telegram_bot_url
        telegram.setEnabled(bool(bot_url)); purchase.setEnabled(bool(bot_url)); open_bot.setEnabled(bool(bot_url))
        telegram.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(bot_url + "?start=activation")))
        purchase.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(bot_url + "?start=buy")))
        open_bot.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(bot_url)))
        support.setEnabled(bool(bot_url))
        support.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(bot_url + "?start=support")))
        free_access.setEnabled(bool(bot_url)); free_access.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(bot_url + "?start=free")))
        links.addWidget(free_access); links.addWidget(telegram); links.addWidget(purchase); links.addWidget(open_bot); links.addWidget(support); links.addStretch(1); layout.addLayout(links)
        self.devices = QTableWidget(0, 4); self.devices.setHorizontalHeaderLabels(("Устройство", "Статус", "Первый вход", "Последняя проверка")); layout.addWidget(self.devices, 1)
        self.technical = QLabel(); self.technical.setWordWrap(True); self.technical.setTextInteractionFlags(self.technical.textInteractionFlags()); layout.addWidget(self.technical)
        close = QPushButton("Закрыть"); close.clicked.connect(self.accept); layout.addWidget(close); self.update_status()

    def update_status(self) -> None:
        value = self.service.status(); state = value.state.value if hasattr(value.state, "value") else str(value.state)
        free_text = ""
        if value.plan == "free_channel" and value.active:
            try:
                free = self.service.free_status()
                projects, shorts, devices = free.get("projects", {}), free.get("shorts_sources", {}), free.get("devices", {})
                free_text = (f"\nFREE-квоты (сервер): проекты {projects.get('used', 0)} из {projects.get('limit', 0)}; "
                             f"Shorts-источники {shorts.get('used', 0)} из {shorts.get('limit', 0)}; "
                             f"устройства {devices.get('used', 0)} из {devices.get('limit', 0)}")
            except Exception as exc:
                free_text = "\nFREE-квоты временно не синхронизированы: " + str(exc)
        self.status_label.setText(f"Статус: {state}\nТариф: {value.plan or '—'}\nОкончание: {value.expires_at or '—'}\n"
                                  f"Следующая проверка: раз в 24 часа\nOffline до: {value.offline_grace_until or '—'}\n"
                                  f"Последняя синхронизация: {value.last_refresh or '—'}{free_text}\n{value.message}")
        self.technical.setText(self.service.last_diagnostics.safe_text())

    def _error(self, exc: Exception) -> None:
        code = getattr(exc, "code", "LICENSE_ERROR"); request_id = getattr(exc, "request_id", "")
        self.technical.setText(self.service.last_diagnostics.safe_text())
        devices = getattr(exc, "details", {}).get("devices", [])
        if devices:
            self._populate_devices(devices)
        QMessageBox.warning(self, "Лицензия", ERRORS.get(code, str(exc)))

    def activate(self) -> None:
        try: self.service.activate(self.code.text().strip()); self.code.clear(); self.update_status()
        except Exception as exc: self._error(exc)

    def check_connection(self) -> None:
        try: self.service._request("GET", "/health", timeout=8); QMessageBox.information(self, "Лицензия", "Сервер доступен.")
        except Exception as exc: self._error(exc)

    def refresh(self) -> None:
        try: self.service.refresh(); self.update_status()
        except Exception as exc: self._error(exc)

    def show_devices(self) -> None:
        try: values = self.service.devices()
        except Exception as exc: self._error(exc); return
        self._populate_devices(values)

    def _populate_devices(self, values) -> None:
        self.devices.setRowCount(len(values))
        for row, item in enumerate(values):
            for column, key in enumerate(("name", "status", "first_seen_at", "last_seen_at")):
                self.devices.setItem(row, column, QTableWidgetItem(str(item.get(key, ""))))

    def deactivate(self) -> None:
        status = self.service.status()
        if not status.device_id: return
        if QMessageBox.question(self, "Деактивация", "Отключить это устройство и удалить локальную сессию?") != QMessageBox.Yes: return
        try: self.service.deactivate_device(status.device_id); self.update_status()
        except Exception as exc: self._error(exc)
