from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtWidgets import (
    QComboBox, QDoubleSpinBox, QFormLayout, QHBoxLayout, QLabel, QPushButton,
    QSlider, QStyle, QVBoxLayout, QWidget,
)

try:
    from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
    from PySide6.QtMultimediaWidgets import QVideoWidget
    MULTIMEDIA_AVAILABLE = True
except ImportError:
    QAudioOutput = QMediaPlayer = QVideoWidget = None
    MULTIMEDIA_AVAILABLE = False

from creator_assistant.domain.shorts.models import Candidate, Transcript


def format_time(milliseconds: int) -> str:
    milliseconds = max(0, int(milliseconds))
    minutes, remainder = divmod(milliseconds, 60_000)
    seconds, millis = divmod(remainder, 1000)
    return f"{minutes:02d}:{seconds:02d}.{millis:03d}"


class SeekSlider(QSlider):
    """A click-to-seek slider with a continuously draggable handle."""

    seek_requested = Signal(int)
    scrub_started = Signal()
    scrub_finished = Signal(int)

    def __init__(self, orientation, parent=None) -> None:
        super().__init__(orientation, parent)
        self._dragging = False

    def _value_from_x(self, x: float) -> int:
        handle = self.style().pixelMetric(QStyle.PM_SliderLength, None, self)
        span = max(1, self.width() - handle)
        position = max(0, min(span, round(x - handle / 2)))
        return QStyle.sliderValueFromPosition(self.minimum(), self.maximum(), position, span)

    def _seek_from_event(self, event) -> int:
        value = self._value_from_x(event.position().x())
        self.setValue(value)
        self.seek_requested.emit(value)
        return value

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt API
        if event.button() == Qt.LeftButton:
            self._dragging = True
            self.scrub_started.emit()
            self._seek_from_event(event)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 - Qt API
        if self._dragging:
            self._seek_from_event(event)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt API
        if self._dragging and event.button() == Qt.LeftButton:
            value = self._seek_from_event(event)
            self._dragging = False
            self.scrub_finished.emit(value)
            event.accept()
            return
        super().mouseReleaseEvent(event)


class CandidateEditor(QWidget):
    boundaries_saved = Signal(object, float, float)
    active_boundary_changed = Signal(object, float, float, str)
    status_changed = Signal(object, str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.candidate: Candidate | None = None
        self.transcript: Transcript | None = None
        self.proxy: Path | None = None
        self._updating_timeline = False
        self._resume_after_scrub = False
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
            self.player.durationChanged.connect(self._duration_changed)
            layout.addWidget(self.video, 1)
        else:
            self.player = None
            layout.addWidget(QLabel("QtMultimedia недоступен. Используйте внешний проигрыватель."), 1)

        timeline_row = QHBoxLayout()
        self.timeline = SeekSlider(Qt.Horizontal)
        self.timeline.setRange(0, 0)
        self.timeline.seek_requested.connect(self._seek_relative)
        self.timeline.sliderMoved.connect(self._seek_relative)
        self.timeline.scrub_started.connect(self._begin_scrub)
        self.timeline.scrub_finished.connect(self._end_scrub)
        self.time_label = QLabel("00:00.000 / 00:00.000")
        self.time_label.setMinimumWidth(155)
        timeline_row.addWidget(self.timeline, 1)
        timeline_row.addWidget(self.time_label)
        layout.addLayout(timeline_row)

        playback = QHBoxLayout()
        self.play = QPushButton("Воспроизвести / пауза")
        self.external = QPushButton("Открыть внешне")
        for label, delta in (("−5 c", -5.0), ("−1 c", -1.0), ("−0,1 c", -0.1), ("+0,1 c", 0.1), ("+1 c", 1.0), ("+5 c", 5.0)):
            button = QPushButton(label)
            button.clicked.connect(lambda _checked=False, amount=delta: self._step(amount))
            playback.addWidget(button)
        self.speed = QComboBox()
        for value in (0.75, 1.0, 1.25, 1.5):
            self.speed.addItem(f"{value:g}×", value)
        self.speed.setCurrentIndex(1)
        self.volume = QSlider(Qt.Horizontal)
        self.volume.setRange(0, 100)
        self.volume.setValue(80)
        playback.insertWidget(0, self.play)
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
        self.alternatives.setToolTip(
            "Другие варианты начала и конца этого же сюжетного момента."
        )
        self.alternatives.currentIndexChanged.connect(self._choose_alternative)
        form.addRow("Начало", self._boundary_row(self.start))
        form.addRow("Конец", self._boundary_row(self.end))
        form.addRow("Длительность", self.duration)
        form.addRow("Другие варианты границ этого Short", self.alternatives)
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

    def _boundary_row(self, control):
        widget = QWidget()
        row = QHBoxLayout(widget)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(control)
        for delta in (-5, -1, -0.1, 0.1, 1, 5):
            button = QPushButton(f"{delta:+g}")
            button.clicked.connect(lambda _checked=False, c=control, d=delta: c.setValue(max(0, c.value() + d)))
            row.addWidget(button)
        return widget

    def set_candidate(self, candidate: Candidate, proxy: Path, transcript: Transcript | None = None) -> None:
        if self.player:
            self.player.pause()
        self.candidate, self.proxy, self.transcript = candidate, proxy, transcript
        self.heading.setText(f"{candidate.id} · score {candidate.score:.1f}\n{candidate.text}")
        self.start.setValue(candidate.start)
        self.end.setValue(candidate.end)
        self.alternatives.blockSignals(True)
        self.alternatives.clear()
        variants = self._display_variants(candidate)
        for variant in variants:
            self.alternatives.addItem(variant["label"], variant)
        current_id = candidate.selected_boundary_variant_id or "main"
        index = 0
        for row in range(self.alternatives.count()):
            value = self.alternatives.itemData(row)
            if isinstance(value, dict) and value.get("id") == current_id:
                index = row
                break
        if index >= 0:
            self.alternatives.setCurrentIndex(index)
        self.alternatives.blockSignals(False)
        self._reset_timeline()
        if self.player:
            self.player.setVideoOutput(self.video)
            self.player.setSource(QUrl.fromLocalFile(str(proxy)))
            self.player.setPosition(round(candidate.start * 1000))

    def _clip_duration_ms(self) -> int:
        return max(0, round((self.end.value() - self.start.value()) * 1000))

    def _reset_timeline(self) -> None:
        duration = self._clip_duration_ms()
        self.timeline.setRange(0, duration)
        self.timeline.setValue(0)
        self.time_label.setText(f"{format_time(0)} / {format_time(duration)}")

    def _toggle_play(self) -> None:
        if not self.player or not self.candidate:
            return
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.pause()
        else:
            position = self.player.position()
            start, end = round(self.start.value() * 1000), round(self.end.value() * 1000)
            if position < start or position >= end:
                self.player.setPosition(start)
            self.player.play()

    def _seek_relative(self, relative_ms: int) -> None:
        if not self.player or not self.candidate:
            return
        relative_ms = max(0, min(int(relative_ms), self._clip_duration_ms()))
        self.player.setPosition(round(self.start.value() * 1000) + relative_ms)

    def _step(self, seconds: float) -> None:
        if not self.player:
            return
        relative = self.player.position() - round(self.start.value() * 1000)
        self._seek_relative(relative + round(seconds * 1000))

    def _begin_scrub(self) -> None:
        if not self.player:
            return
        self._resume_after_scrub = self.player.playbackState() == QMediaPlayer.PlayingState
        if self._resume_after_scrub:
            self.player.pause()

    def _end_scrub(self, _relative_ms: int) -> None:
        if self.player and self._resume_after_scrub:
            self.player.play()
        self._resume_after_scrub = False

    def _position_changed(self, position: int) -> None:
        if not self.candidate:
            return
        start_ms = round(self.start.value() * 1000)
        duration_ms = self._clip_duration_ms()
        relative = max(0, min(duration_ms, position - start_ms))
        self._updating_timeline = True
        self.timeline.setValue(relative)
        self._updating_timeline = False
        self.time_label.setText(f"{format_time(relative)} / {format_time(duration_ms)}")
        if self.player and position >= start_ms + duration_ms:
            self.player.pause()
            self.player.setPosition(start_ms + duration_ms)

    def _duration_changed(self, _duration: int) -> None:
        self._reset_timeline()
        if self.player and self.candidate:
            self.player.setPosition(round(self.start.value() * 1000))

    def _open_external(self) -> None:
        if self.proxy and self.proxy.is_file() and os.name == "nt":
            os.startfile(str(self.proxy))

    def release_media(self) -> None:
        if self.player:
            self.player.stop()
            self.player.setVideoOutput(None)
            self.player.setSource(QUrl())

    def _update_duration(self) -> None:
        value = self.end.value() - self.start.value()
        self.duration.setText(f"{value:.3f} сек")
        self._reset_timeline()
        if value <= 0:
            self.warning.setText("Конец должен быть позже начала.")
        elif value > 60:
            self.warning.setText("Длительность больше 60 секунд. Проверьте лимиты площадки.")
        else:
            self.warning.setText("")

    def _choose_alternative(self) -> None:
        value = self.alternatives.currentData()
        if isinstance(value, dict):
            start, end = float(value["start"]), float(value["end"])
            self.start.setValue(start)
            self.end.setValue(end)
            self._seek_relative(0)
            if self.candidate:
                self.active_boundary_changed.emit(self.candidate, start, end, str(value.get("id", "main")))

    def _save(self) -> None:
        if self.candidate:
            self.boundaries_saved.emit(self.candidate, self.start.value(), self.end.value())

    def _display_variants(self, candidate: Candidate) -> list[dict]:
        main = {
            "id": "main",
            "start": candidate.start,
            "end": candidate.end,
            "score": candidate.final_score or candidate.score,
            "reason": "текущие рабочие границы",
        }
        alternatives = []
        for index, bounds in enumerate(candidate.alternatives, 1):
            if len(bounds) != 2:
                continue
            start, end = float(bounds[0]), float(bounds[1])
            if end <= start or end - start < 10 or end - start > 60:
                continue
            if abs(start - candidate.start) < 0.5 and abs(end - candidate.end) < 0.5:
                continue
            duration_score = 100 - abs((end - start) - min(58, max(35, candidate.duration))) * 1.2
            difference_score = min(12, abs(start - candidate.start) + abs(end - candidate.end))
            score = round((candidate.final_score or candidate.score) * 0.8 + duration_score * 0.15 + difference_score * 0.05, 1)
            alternatives.append({
                "id": f"alt_{index:03d}",
                "start": start,
                "end": end,
                "score": score,
                "reason": "похожий сюжет с другой точкой входа/выхода",
            })
        alternatives.sort(key=lambda item: -float(item["score"]))
        deduped = []
        for item in alternatives:
            if any(abs(item["start"] - old["start"]) < 1.5 and abs(item["end"] - old["end"]) < 1.5 for old in deduped):
                continue
            deduped.append(item)
            if len(deduped) == 3:
                break
        variants = [main] + deduped
        for index, item in enumerate(variants):
            item["label"] = self._variant_label(item, index)
        return variants

    def _variant_label(self, variant: dict, index: int) -> str:
        start, end = float(variant["start"]), float(variant["end"])
        text = self._transcript_for_range(start, end)
        words = text.split()
        beginning = " ".join(words[:8]) or "—"
        ending = " ".join(words[-8:]) or "—"
        duration = end - start
        title = "Основные границы" if index == 0 else f"Вариант {index}"
        return (
            f"{title} · {duration:.0f} сек · {start:.2f}–{end:.2f} · "
            f"Начало: «{beginning}» · Финал: «{ending}» · Оценка: {variant['score']:.0f}"
        )

    def _transcript_for_range(self, start: float, end: float) -> str:
        if not self.transcript:
            return self.candidate.text if self.candidate else ""
        parts = [
            segment.text.strip()
            for segment in self.transcript.segments
            if segment.end > start and segment.start < end and segment.text.strip()
        ]
        return " ".join(parts)
