from PySide6.QtWidgets import QGroupBox, QLabel, QProgressBar, QVBoxLayout


class AnalysisProgressPanel(QGroupBox):
    def __init__(self, parent=None) -> None:
        super().__init__("B. Ход анализа", parent)
        layout = QVBoxLayout(self)
        self.stage = QLabel("Ожидание источника")
        self.details = QLabel("После выбора видео можно будет запустить локальный анализ.")
        self.details.setWordWrap(True)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        layout.addWidget(self.stage)
        layout.addWidget(self.details)
        layout.addWidget(self.progress)

    def update_state(self, stage: str, details: str, percent: int = 0) -> None:
        self.stage.setText(stage)
        self.details.setText(details)
        self.progress.setValue(max(0, min(100, percent)))
