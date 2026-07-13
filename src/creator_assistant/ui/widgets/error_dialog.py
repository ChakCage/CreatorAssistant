import os
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import QMimeData, Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QLabel, QPushButton, QTextEdit, QVBoxLayout


class ErrorDialog(QDialog):
    def __init__(
        self,
        message: str,
        details: str,
        parent=None,
        retry_callback: Optional[Callable[[], None]] = None,
        folder: Optional[Path] = None,
    ) -> None:
        super().__init__(parent)
        self.details = details
        self.setWindowTitle("Creator Assistant — ошибка")
        self.resize(720, 440)
        layout = QVBoxLayout(self)
        label = QLabel(message)
        label.setWordWrap(True)
        label.setProperty("class", "errorText")
        layout.addWidget(label)
        details_view = QTextEdit(details)
        details_view.setReadOnly(True)
        details_view.setVisible(False)
        layout.addWidget(details_view)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.button(QDialogButtonBox.Close).setText("Закрыть")
        copy_button = buttons.addButton("Скопировать", QDialogButtonBox.ActionRole)
        show_button = buttons.addButton("Технические подробности", QDialogButtonBox.ActionRole)
        if folder and folder.is_dir():
            open_button = buttons.addButton("Открыть папку", QDialogButtonBox.ActionRole)
            open_button.clicked.connect(lambda: os.startfile(str(folder)))
        if retry_callback:
            retry_button = buttons.addButton("Повторить проверку", QDialogButtonBox.ActionRole)
            retry_button.clicked.connect(self.accept)
            retry_button.clicked.connect(retry_callback)
        copy_button.clicked.connect(self.copy_details)
        show_button.clicked.connect(lambda: details_view.setVisible(not details_view.isVisible()))
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def copy_details(self) -> None:
        QGuiApplication.clipboard().setText(self.details)
