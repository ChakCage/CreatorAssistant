from __future__ import annotations

from PySide6.QtWidgets import QComboBox, QDialog, QDialogButtonBox, QFormLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout

from creator_assistant.domain.youtube_auth import BROWSERS


class YouTubeAuthDialog(QDialog):
    USE = 1
    SETTINGS = 2
    ANONYMOUS = 3

    def __init__(self, browser: str = "firefox", profile: str = "", parent=None) -> None:
        super().__init__(parent)
        self.action = 0
        self.setWindowTitle("YouTube запросил подтверждение")
        layout = QVBoxLayout(self)
        text = QLabel(
            "YouTube не разрешил получить видео без браузерной сессии.\n\n"
            "Creator Assistant может использовать cookies из браузера, в котором вы вошли в YouTube."
        )
        text.setWordWrap(True)
        layout.addWidget(text)
        form = QFormLayout()
        self.browser = QComboBox()
        ordered_browsers = ["firefox", "chrome"] + [value for value in BROWSERS if value not in {"firefox", "chrome"}]
        for value in ordered_browsers:
            label = BROWSERS[value]
            self.browser.addItem(label, value)
        self.browser.setCurrentIndex(max(0, self.browser.findData(browser)))
        self.profile = QLineEdit(profile)
        self.profile.setPlaceholderText("Необязательно: Default, Profile 1…")
        form.addRow("Браузер", self.browser)
        form.addRow("Профиль", self.profile)
        layout.addLayout(form)
        buttons = QDialogButtonBox()
        use = buttons.addButton("Использовать cookies и повторить", QDialogButtonBox.AcceptRole)
        anonymous = buttons.addButton("Попробовать анонимно ещё раз", QDialogButtonBox.ActionRole)
        settings = buttons.addButton("Открыть настройки", QDialogButtonBox.ActionRole)
        cancel = buttons.addButton("Отмена", QDialogButtonBox.RejectRole)
        use.clicked.connect(self._use)
        anonymous.clicked.connect(self._anonymous)
        settings.clicked.connect(self._settings)
        cancel.clicked.connect(self.reject)
        layout.addWidget(buttons)

    def _use(self) -> None:
        self.action = self.USE
        self.accept()

    def _settings(self) -> None:
        self.action = self.SETTINGS
        self.accept()

    def _anonymous(self) -> None:
        self.action = self.ANONYMOUS
        self.accept()

    @property
    def selected_browser(self) -> str:
        return str(self.browser.currentData())

    @property
    def selected_profile(self) -> str:
        return self.profile.text().strip()
