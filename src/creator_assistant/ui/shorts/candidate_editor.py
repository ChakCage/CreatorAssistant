from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import QUrl, Signal
from PySide6.QtWidgets import (
    QComboBox, QDoubleSpinBox, QFormLayout, QHBoxLayout, QLabel, QPushButton,
    QSlider, QVBoxLayout, QWidget,
)
from PySide6.QtCore import Qt

try:
    from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
    from PySide6.QtMultimediaWidgets import QVideoWidget
    MULTIMEDIA_AVAILABLE = True
except ImportError:
    QAudioOutput = QMediaPlayer = QVideoWidget = None
    MULTIMEDIA_AVAILABLE = False

from creator_assistant.domain.shorts.models import Candidate


class CandidateEditor(QWidget):
    boundaries_saved = Signal(object, float, float)
    status_changed = Signal(object, str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.candidate: Candidate | None = None
        self.proxy: Path | None = None
        layout = QVBoxLayout(self)
        self.heading = QLabel("Кандидат не выбран")
        self.heading.setWordWrap(True)
        layout.addWidget(self.heading)
        if MULTIMEDIA_AVAILABLE:
            self.video = QVideoWidget()
            self.video.setMinimumHeight(260)
            self.audio = QAudioOutput(self)
            self.player = QMediaPlayer(self)
            self.player.setAudioOutput(self.audio)
            self.player.setVideoOutput(self.video)
            self.player.positionChanged.connect(self._position_changed)
            layout.addWidget(self.video, 1)
        else:
            self.player = None
            layout.addWidget(QLabel("QtMultimedia недоступен. Используйте внешний проигрыватель."), 1)
        playback = QHBoxLayout()
        self.play = QPushButton("Play / Pause")
        self.external = QPushButton("Открыть внешне")
        self.speed = QComboBox()
        for value in (0.75, 1.0, 1.25, 1.5):
            self.speed.addItem(f"{value:g}×", value)
        self.speed.setCurrentIndex(1)
        self.volume = QSlider(Qt.Horizontal)
        self.volume.setRange(0, 100)
        self.volume.setValue(80)
        playback.addWidget(self.play)
        playback.addWidget(self.external)
        playback.addWidget(self.speed)
        playback.addWidget(QLabel("Громкость"))
        playback.addWidget(self.volume)
        layout.addLayout(playback)
        form = QFormLayout()
        self.start = QDoubleSpinBox()
        self.end = QDoubleSpinBox()
        for control in (self.start, self.end):
            control.setRange(0, 24 * 60 * 60)
            control.setDecimals(3)
            control.setSuffix(" сек")
        self.duration = QLabel("0.000 сек")
        self.alternatives = QComboBox()
        self.alternatives.currentIndexChanged.connect(self._choose_alternative)
        form.addRow("Начало", self._boundary_row(self.start, "start"))
        form.addRow("Конец", self._boundary_row(self.end, "end"))
        form.addRow("Длительность", self.duration)
        form.addRow("Альтернативные границы", self.alternatives)
        layout.addLayout(form)
        self.warning = QLabel()
        self.warning.setWordWrap(True)
        layout.addWidget(self.warning)
        actions = QHBoxLayout()
        save = QPushButton("Сохранить границы")
        approve = QPushButton("Одобрить")
        reject = QPushButton("Отклонить")
        save.clicked.connect(self._save)
        approve.clicked.connect(lambda: self.candidate and self.status_changed.emit(self.candidate, "approved"))
        reject.clicked.connect(lambda: self.candidate and self.status_changed.emit(self.candidate, "rejected"))
        actions.addWidget(save)
        actions.addWidget(approve)
        actions.addWidget(reject)
        actions.addStretch(1)
        layout.addLayout(actions)
        self.play.clicked.connect(self._toggle_play)
        self.external.clicked.connect(self._open_external)
        self.speed.currentIndexChanged.connect(lambda: self.player and self.player.setPlaybackRate(float(self.speed.currentData())))
        self.volume.valueChanged.connect(lambda value: self.audio.setVolume(value / 100) if MULTIMEDIA_AVAILABLE else None)
        self.start.valueChanged.connect(self._update_duration)
        self.end.valueChanged.connect(self._update_duration)

    def _boundary_row(self, control, which):
        widget = QWidget()
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(control)
        for delta in (-5, -1, -0.1, 0.1, 1, 5):
            button = QPushButton(f"{delta:+g}")
            button.clicked.connect(lambda _checked=False, c=control, d=delta: c.setValue(max(0, c.value() + d)))
            layout.addWidget(button)
        return widget

    def set_candidate(self, candidate: Candidate, proxy: Path) -> None:
        self.candidate, self.proxy = candidate, proxy
        self.heading.setText(f"{candidate.id} · score {candidate.score:.1f}\n{candidate.text}")
        self.start.setValue(candidate.start)
        self.end.setValue(candidate.end)
        self.alternatives.blockSignals(True)
        self.alternatives.clear()
        self.alternatives.addItem(f"Основные: {candidate.start:.3f}–{candidate.end:.3f}", [candidate.start, candidate.end])
        for start, end in candidate.alternatives:
            self.alternatives.addItem(f"{start:.3f}–{end:.3f}", [start, end])
        self.alternatives.blockSignals(False)
        if self.player:
            self.player.setSource(QUrl.fromLocalFile(str(proxy)))
            self.player.setPosition(round(candidate.start * 1000))

    def _toggle_play(self) -> None:
        if not self.player or not self.candidate:
            return
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.pause()
        else:
            position = self.player.position() / 1000
            if position < self.start.value() or position >= self.end.value():
                self.player.setPosition(round(self.start.value() * 1000))
            self.player.play()

    def _position_changed(self, position: int) -> None:
        if self.player and self.candidate and position >= round(self.end.value() * 1000):
            self.player.pause()

    def _open_external(self) -> None:
        if self.proxy and self.proxy.is_file() and os.name == "nt":
            os.startfile(str(self.proxy))

    def _update_duration(self) -> None:
        value = self.end.value() - self.start.value()
        self.duration.setText(f"{value:.3f} сек")
        if value <= 0:
            self.warning.setText("Конец должен быть позже начала.")
        elif value > 60:
            self.warning.setText("Длительность больше 60 секунд. Сохранение разрешено, но проверьте лимиты площадки.")
        else:
            self.warning.setText("")

    def _choose_alternative(self) -> None:
        value = self.alternatives.currentData()
        if value and len(value) == 2:
            self.start.setValue(float(value[0]))
            self.end.setValue(float(value[1]))

    def _save(self) -> None:
        if self.candidate:
            self.boundaries_saved.emit(self.candidate, self.start.value(), self.end.value())
