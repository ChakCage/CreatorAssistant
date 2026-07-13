from PySide6.QtCore import QElapsedTimer, QTimer
from PySide6.QtWidgets import QGroupBox, QLabel, QProgressBar, QVBoxLayout


class AnalysisProgressPanel(QGroupBox):
    def __init__(self, parent=None) -> None:
        super().__init__("B. Ход анализа", parent)
        layout = QVBoxLayout(self)
        self.stage = QLabel("Ожидание источника")
        self.details = QLabel("После выбора видео можно будет запустить локальный анализ.")
        self.details.setWordWrap(True)
        self.backend = QLabel("Backend: — · модель: — · устройство: —")
        self.timing = QLabel("Прошло: 00:00 · ETA: недоступно")
        self.stage_progress = QProgressBar()
        self.stage_progress.setRange(0, 1)
        self.stage_progress.setValue(0)
        self.stage_progress.setFormat("Процент этапа не сообщается backend")
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setFormat("Общий прогресс: %p%")
        layout.addWidget(self.stage)
        layout.addWidget(self.details)
        layout.addWidget(self.backend)
        layout.addWidget(self.timing)
        layout.addWidget(self.stage_progress)
        layout.addWidget(self.progress)
        self.elapsed = QElapsedTimer()
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self._tick)

    def update_state(self, stage: str, details: str, percent: int = 0) -> None:
        self.stage.setText(stage)
        self.details.setText(details)
        self.progress.setValue(max(0, min(100, percent)))

    def start_operation(self, backend: str, model: str, device: str) -> None:
        self.backend.setText(f"Backend: {backend} · модель: {model} · устройство: {device}")
        self.stage_progress.setRange(0, 0)
        self.elapsed.start()
        self.timer.start()
        self._tick()

    def finish_operation(self) -> None:
        self._tick()
        self.timer.stop()
        self.stage_progress.setRange(0, 1)
        self.stage_progress.setValue(1)

    def _tick(self) -> None:
        seconds = max(0, self.elapsed.elapsed() // 1000) if self.elapsed.isValid() else 0
        self.timing.setText(f"Прошло: {seconds // 60:02d}:{seconds % 60:02d} · ETA: недоступно")
