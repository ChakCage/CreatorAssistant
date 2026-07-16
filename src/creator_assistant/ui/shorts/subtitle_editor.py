from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import logging
from pathlib import Path

from PySide6.QtCore import QObject, QEvent, QPoint, QProcess, QSettings, QRect, QRectF, QSize, Qt, QThread, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QColor, QBrush, QFont, QFontDatabase, QFontMetricsF, QImage, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (
    QFileDialog, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout,
    QHeaderView, QLabel, QListWidget, QGridLayout, QLineEdit, QMessageBox, QPushButton,
    QScrollArea, QSizePolicy, QSlider, QSplitter, QSpinBox, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

try:
    from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer, QVideoSink
    VERTICAL_MULTIMEDIA_AVAILABLE = True
except ImportError:
    QAudioOutput = QMediaPlayer = QVideoSink = None
    VERTICAL_MULTIMEDIA_AVAILABLE = False

from creator_assistant.domain.shorts.models import SubtitleCue
from creator_assistant.services.shorts.channel_assets import ChannelAssetStore
from creator_assistant.services.shorts.font_resolver import DEFAULT_FONT_FAMILY, resolve_font, resolved_qfont
from creator_assistant.services.shorts.overlay_layout import OverlayLayoutCalculator, layout_title_text
from creator_assistant.services.shorts.filter_graph_builder import ShortsFilterGraphBuilder
from creator_assistant.services.shorts.text_alignment import HorizontalTextAlignment, aligned_left
from creator_assistant.services.shorts.title_service import ShortTitleService
from creator_assistant.services.shorts.semantic_backend import OllamaSemanticScorer
from creator_assistant.domain.job import CancellationToken
from creator_assistant.services.shorts.subtitle_layout import (
    ASS_FONT_TO_QT_PIXEL_SCALE,
    SubtitleLayoutCalculator,
    ass_qt_bearing_offset,
    ass_qt_font_stretch,
)
from creator_assistant.services.shorts.subtitle_service import (
    STYLE_PRESETS, SubtitleService, resolved_style,
)
from creator_assistant.ui.shorts.vertical_layout_panel import VerticalLayoutPanel
from creator_assistant.ui.shorts.candidate_editor import SeekSlider, format_time

TITLE_STYLE_PRESETS = {
    "clean": {"size": 76, "bold": True, "outline": 4, "shadow": 2, "color": "#ffffff"},
    "large": {"size": 98, "bold": True, "outline": 6, "shadow": 3, "color": "#ffffff"},
    "gaming": {"size": 88, "bold": True, "outline": 7, "shadow": 5, "color": "#ffd54a"},
}


def _format_fps(value: float) -> str:
    if value <= 0:
        return "—"
    text = f"{value:.2f}".rstrip("0").rstrip(".")
    return text.replace(".", ",")


class _AiTitleWorker(QObject):
    finished = Signal(object)
    failed = Signal(str)

    def __init__(self, function) -> None:
        super().__init__()
        self.function = function

    @Slot()
    def run(self) -> None:
        try:
            self.finished.emit(self.function())
        except Exception as exc:
            self.failed.emit(str(exc) or exc.__class__.__name__)


class VerticalFramePreview(QWidget):
    resized = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setMinimumSize(216, 384)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        self.composition_size = (540, 960)
        self.candidate = None
        self.settings: dict = {}
        self.layout_settings: dict = {}
        self.branding_settings: dict = {}
        self.video_frame = QImage()
        self.exact_frame = QImage()
        self._blur_background = QPixmap()
        self._blur_source_key: object | None = None
        self.sample = "Длинный пример субтитров безопасно помещается в кадре"

    def set_preview(self, candidate, settings: dict, layout_settings: dict, sample: str | None = None, branding_settings: dict | None = None) -> None:
        self.candidate = candidate
        self.settings = dict(settings)
        self.layout_settings = dict(layout_settings)
        self.branding_settings = dict(branding_settings or {})
        if sample is not None:
            self.sample = sample.strip()
        self._last_mode = self.layout_settings.get("mode", "center_crop")
        self.update()

    def set_video_frame(self, image: QImage, *, preserve_exact: bool = False) -> None:
        if image and not image.isNull():
            self.video_frame = image.copy()
            self._blur_background = QPixmap()
            self._blur_source_key = None
            if not preserve_exact:
                self.exact_frame = QImage()
            self.update()

    def set_exact_frame(self, image: QImage) -> None:
        if image and not image.isNull():
            self.exact_frame = image.copy()
            self.update()

    def invalidate_exact_frame(self) -> None:
        self.exact_frame = QImage()
        self.update()

    def effective_preview_size(self) -> tuple[int, int]:
        scale = min(self.width() / 1080, self.height() / 1920)
        return max(1, round(1080 * scale)), max(1, round(1920 * scale))

    def set_target_preview_size(self, width: int, height: int) -> None:
        # Composition quality is independent from physical UI pixels.
        self.composition_size = (max(1, int(width)), max(1, int(height)))
        self.resized.emit()

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt API
        return QSize(360, 640)

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt API
        super().resizeEvent(event)
        self.resized.emit()

    def paintEvent(self, _event) -> None:  # noqa: N802 - Qt API
        # Paint the scene into a real backing image at the selected quality.
        # In Fit mode the widget stays the same physical size, while text, banner,
        # blur and video are still rasterized at 360p/540p/720p/1080p as selected.
        composition_w, composition_h = self.composition_size
        canvas = QImage(composition_w, composition_h, QImage.Format_ARGB32_Premultiplied)
        canvas.fill(QColor("#0b0f14"))
        painter = QPainter(canvas)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        if not self.exact_frame.isNull():
            painter.drawImage(QRect(0, 0, composition_w, composition_h), self.exact_frame)
            painter.end()
            display = QPainter(self)
            display.setRenderHint(QPainter.SmoothPixmapTransform)
            display.drawImage(self.rect(), canvas)
            display.end()
            return
        scale = composition_w / 1080
        left = 0.0
        top = 0.0
        painter.save()
        painter.translate(left, top)
        painter.scale(scale, scale)
        frame = QRect(0, 0, 1080, 1920)
        pixmap = QPixmap.fromImage(self.video_frame) if not self.video_frame.isNull() else QPixmap()
        if pixmap.isNull() and self.candidate and getattr(self.candidate, "thumbnail", ""):
            pixmap = QPixmap(str(Path(self.candidate.thumbnail)))
        if pixmap.isNull():
            painter.fillRect(frame, QColor("#182330"))
            painter.setPen(QColor("#60758a"))
            painter.drawText(frame, Qt.AlignCenter, "PREVIEW 1080×1920")
        elif self.layout_settings.get("mode", "center_crop") in {"blur_background", "solid_color"}:
            mode = self.layout_settings.get("mode")
            if mode == "blur_background":
                source_key = (
                    int(self.video_frame.cacheKey()) if not self.video_frame.isNull()
                    else str(getattr(self.candidate, "thumbnail", ""))
                )
                if self._blur_background.isNull() or source_key != self._blur_source_key:
                    expanded = pixmap.scaled(270, 480, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
                    crop_x = round(max(0, expanded.width() - 270) / 2)
                    crop_y = round(max(0, expanded.height() - 480) / 2)
                    background = expanded.copy(crop_x, crop_y, 270, 480)
                    # A cheap but genuine realtime blur: aggressively reduce the
                    # background before smooth upscaling, matching the optimized
                    # low-resolution background branch of the final FFmpeg graph.
                    background = background.scaled(45, 80, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
                    self._blur_background = background.scaled(270, 480, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
                    self._blur_source_key = source_key
                painter.drawPixmap(frame, self._blur_background, self._blur_background.rect())
            else:
                painter.fillRect(frame, QColor(str(self.layout_settings.get("background_color", "black"))))
            factor = int(self.layout_settings.get("foreground_scale", 100)) / 100
            foreground = pixmap.scaled(
                round(1080 * factor), round(1920 * factor),
                Qt.KeepAspectRatio, Qt.SmoothTransformation,
            )
            painter.drawPixmap(
                round((1080 - foreground.width()) / 2),
                round((1920 - foreground.height()) / 2),
                foreground,
            )
        else:
            center = int(self.layout_settings.get("crop_center", 50)) / 100
            scaled = pixmap.scaled(1080, 1920, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
            x = round(max(0, scaled.width() - 1080) * center)
            y = round(max(0, scaled.height() - 1920) / 2)
            painter.drawPixmap(frame, scaled, QRect(x, y, 1080, 1920))
        painter.restore()

        self._draw_branding(painter, scale, left, top)

        if not bool(self.branding_settings.get("show_subtitles", True)):
            painter.setPen(QColor(255, 255, 255, 70))
            painter.setBrush(Qt.NoBrush)
            painter.drawRect(QRectF(left + 90 * scale, top + 120 * scale, 900 * scale, 1560 * scale))
            painter.end()
            display = QPainter(self)
            display.setRenderHint(QPainter.SmoothPixmapTransform)
            display.drawImage(self.rect(), canvas)
            display.end()
            return

        banner_size = self._banner_source_size()
        layout = OverlayLayoutCalculator().subtitle_layout(
            self.sample, self.settings, self.branding_settings, banner_size,
        )
        style = resolved_style(self.settings)
        font = resolved_qfont(
            layout.font_name,
            bold=int(style.get("weight", 700)) >= 600,
            pixel_size=max(1.0, layout.font_size * ASS_FONT_TO_QT_PIXEL_SCALE),
        )
        font.setWeight(QFont.Weight(max(100, min(900, int(style.get("weight", 700))))))
        font.setStretch(ass_qt_font_stretch(layout.font_size))
        metrics = QFontMetricsF(font)
        box_left = layout.x if layout.alignment is HorizontalTextAlignment.LEFT else (
            layout.x - layout.width if layout.alignment is HorizontalTextAlignment.RIGHT else layout.x - layout.width / 2
        )
        logical_box = QRectF(box_left, layout.y - layout.height / 2, layout.width, layout.height)
        painter.save()
        painter.scale(scale, scale)
        if layout.background:
            painter.fillRect(logical_box, QColor(0, 0, 0, 130))
        text_path = QPainterPath()
        baselines = (layout.first_line_baseline, layout.second_line_baseline)
        for index, line in enumerate(layout.lines):
            line_width = metrics.horizontalAdvance(line)
            if layout.alignment is HorizontalTextAlignment.LEFT:
                line_x = layout.x
            elif layout.alignment is HorizontalTextAlignment.RIGHT:
                line_x = layout.x - line_width
            else:
                line_x = layout.x - line_width / 2
            line_x += ass_qt_bearing_offset(layout.font_size)
            text_path.addText(line_x, baselines[min(index, 1)], font, line)
        if layout.shadow:
            shadow = text_path.translated(layout.shadow, layout.shadow)
            painter.fillPath(shadow, QBrush(QColor(0, 0, 0, 190)))
        if layout.outline:
            outline_pen = QPen(QColor("#000000"))
            outline_pen.setWidthF(layout.outline * 2)
            outline_pen.setJoinStyle(Qt.RoundJoin)
            painter.strokePath(text_path, outline_pen)
        color = QColor("#ffd700") if str(self.settings.get("style", "clean")) == "gaming" else QColor("#ffffff")
        painter.fillPath(text_path, QBrush(color))
        painter.restore()
        painter.setPen(QColor(255, 255, 255, 70))
        painter.setBrush(Qt.NoBrush)
        painter.drawRect(QRectF(left + 90 * scale, top + 120 * scale, 900 * scale, 1560 * scale))
        painter.end()
        display = QPainter(self)
        display.setRenderHint(QPainter.SmoothPixmapTransform)
        target = self.rect()
        display.drawImage(target, canvas)
        display.end()

    def _banner_source_size(self) -> tuple[int, int] | None:
        banner = Path(str(self.branding_settings.get("channel_banner_path") or ""))
        pixmap = QPixmap(str(banner)) if banner.is_file() else QPixmap()
        if pixmap.isNull():
            return None
        return pixmap.width(), pixmap.height()

    def _draw_branding(self, painter: QPainter, scale: float, left: float, top: float) -> None:
        # Banner is below both text layers in preview, exactly as in FFmpeg.
        if bool(self.branding_settings.get("show_channel_card", False)):
            banner = Path(str(self.branding_settings.get("channel_banner_path") or ""))
            pixmap = QPixmap(str(banner)) if banner.is_file() else QPixmap()
            if not pixmap.isNull():
                rect = OverlayLayoutCalculator().banner_rect(pixmap.width(), pixmap.height(), self.branding_settings)
                scaled = pixmap.scaled(round(rect.width * scale), round(rect.height * scale), Qt.KeepAspectRatio, Qt.SmoothTransformation)
                painter.setOpacity(max(0, min(100, int(self.branding_settings.get("banner_opacity", 100)))) / 100)
                painter.drawPixmap(left + rect.x * scale, top + rect.y * scale, scaled)
                painter.setOpacity(1.0)
        if bool(self.branding_settings.get("show_title", False)):
            title = str(self.branding_settings.get("final_title_text") or "").strip()
            if title:
                title, effective_size = layout_title_text(
                    title,
                    int(self.branding_settings.get("title_size", 78)),
                    bool(self.branding_settings.get("title_bold", True)),
                    font_family=self._title_font_family(),
                )
                font = resolved_qfont(
                    self._title_font_family(),
                    bold=bool(self.branding_settings.get("title_bold", True)),
                    pixel_size=effective_size,
                )
                offset_x = max(-300, min(300, int(self.branding_settings.get("title_offset_x", 0) or 0)))
                alignment = HorizontalTextAlignment.parse(self.branding_settings.get("title_alignment", "center"))
                metrics = QFontMetricsF(font)
                base_y = int(self.branding_settings.get("title_y", 180))
                painter.save()
                painter.scale(scale, scale)
                for line_index, line in enumerate(title.splitlines() or [title]):
                    line_width = min(900.0, metrics.horizontalAdvance(line) + 4)
                    line_left = aligned_left(line_width, alignment, offset_x)
                    line_top = base_y + line_index * (effective_size + 8)
                    box = QRectF(line_left, line_top, line_width, effective_size + 8)
                    if bool(self.branding_settings.get("title_background", False)):
                        painter.fillRect(box.adjusted(-18, -10, 18, 10), QColor(0, 0, 0, 135))
                    raw_path = QPainterPath()
                    raw_path.addText(0, 0, font, line)
                    # FFmpeg drawtext's y denotes the top of the visible glyph
                    # raster.  Position the Qt outline by its real ink bounds,
                    # not by QFontMetrics.ascent(), which leaves a size-dependent
                    # 9-15 px gap above Segoe UI capitals.
                    # DirectWrite's Segoe UI left bearing is two logical pixels
                    # left of FFmpeg drawtext's FreeType raster at this canvas.
                    path = raw_path.translated(line_left + 2, line_top - raw_path.boundingRect().top())
                    shadow = max(0, int(self.branding_settings.get("title_shadow", 2)))
                    if shadow:
                        painter.fillPath(path.translated(shadow, shadow), QBrush(QColor(0, 0, 0, 190)))
                    outline = max(0, int(self.branding_settings.get("title_outline", 4)))
                    if outline:
                        pen = QPen(QColor("#000000"))
                        pen.setWidthF(outline * 2)
                        pen.setJoinStyle(Qt.RoundJoin)
                        painter.strokePath(path, pen)
                    painter.fillPath(path, QBrush(QColor(str(self.branding_settings.get("title_color", "#ffffff")))))
                painter.restore()

    def _title_font_family(self) -> str:
        if bool(self.branding_settings.get("use_subtitle_font_for_title", True)):
            style = resolved_style(self.settings)
            return str(style.get("font") or DEFAULT_FONT_FAMILY)
        return str(self.branding_settings.get("title_font_family") or DEFAULT_FONT_FAMILY)


class RealtimeCompositionRenderer(VerticalFramePreview):
    """Local interactive compositor that never invokes FFmpeg.

    All overlay geometry is calculated in the final 1080x1920 coordinate
    system by ``OverlayLayoutCalculator``.  The completed composition is then
    scaled to the selected preview resolution and viewport.
    """


class PreviewViewport(QScrollArea):
    resized = Signal()
    zoom_requested = Signal(int)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWidgetResizable(False)
        self.setAlignment(Qt.AlignCenter)
        self.setMinimumSize(240, 426)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setSizeAdjustPolicy(QScrollArea.SizeAdjustPolicy.AdjustIgnored)
        self.viewport().installEventFilter(self)
        self._drag_origin: QPoint | None = None
        self._scroll_origin = (0, 0)

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt API
        super().resizeEvent(event)
        self.resized.emit()

    def eventFilter(self, watched, event) -> bool:  # noqa: N802 - Qt API
        if watched is self.viewport():
            if event.type() == QEvent.Type.Wheel and event.modifiers() & Qt.ControlModifier:
                self.zoom_requested.emit(10 if event.angleDelta().y() > 0 else -10)
                event.accept()
                return True
            if event.type() == QEvent.Type.MouseButtonPress and event.button() == Qt.LeftButton:
                if self.horizontalScrollBar().maximum() or self.verticalScrollBar().maximum():
                    self._drag_origin = event.position().toPoint()
                    self._scroll_origin = (self.horizontalScrollBar().value(), self.verticalScrollBar().value())
                    self.viewport().setCursor(Qt.ClosedHandCursor)
                    return True
            if event.type() == QEvent.Type.MouseMove and self._drag_origin is not None:
                delta = event.position().toPoint() - self._drag_origin
                self.horizontalScrollBar().setValue(self._scroll_origin[0] - delta.x())
                self.verticalScrollBar().setValue(self._scroll_origin[1] - delta.y())
                return True
            if event.type() == QEvent.Type.MouseButtonRelease and self._drag_origin is not None:
                self._drag_origin = None
                self.viewport().unsetCursor()
                return True
        return super().eventFilter(watched, event)


class SubtitleEditor(QWidget):
    saved = Signal(object)
    configuration_changed = Signal(object)
    test_render_requested = Signal(object)
    defaults_requested = Signal(object)

    def __init__(self, semantic_backend=None, parent=None) -> None:
        super().__init__(parent)
        self.service = SubtitleService()
        self.candidate = self.transcript = self.paths = None
        self.original: list[SubtitleCue] = []
        self._loading = False
        self._dirty = False
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(250)
        self._save_timer.timeout.connect(self._apply_configuration)
        self.assets = ChannelAssetStore()
        self.title_service = ShortTitleService()
        self.semantic_backend = semantic_backend
        self._ai_thread: QThread | None = None
        self._ai_worker = None
        self._preview_generation_id = 0
        self._preview_position_ms = 0
        self._source_info = None
        self._proxy_info = None
        self._proxy_path: Path | None = None
        self._render_encoder = ""
        self._ffmpeg_path = ""
        self._exact_process: QProcess | None = None
        self._exact_target: Path | None = None
        self._exact_generation = -1
        self._preview_state = "Быстрый предпросмотр"
        self._resume_after_scrub = False
        layout = QVBoxLayout(self)
        heading_row = QHBoxLayout()
        self.heading = QLabel("Выберите кандидата")
        self.dirty_label = QLabel("Сохранено")
        self.dirty_label.setStyleSheet("color: #7fba7a;")
        heading_row.addWidget(self.heading, 1)
        heading_row.addWidget(self.dirty_label)
        layout.addLayout(heading_row)
        self.main_splitter = QSplitter(Qt.Vertical)
        self.top_splitter = QSplitter(Qt.Horizontal)
        self.settings_content = QWidget()
        self.settings_layout = QVBoxLayout(self.settings_content)
        self.settings_layout.setContentsMargins(4, 4, 4, 4)
        self.settings_layout.setSpacing(10)
        self.settings_scroll = QScrollArea()
        self.settings_scroll.setWidgetResizable(True)
        self.settings_scroll.setWidget(self.settings_content)
        self.vertical = VerticalLayoutPanel()
        branding_widget = QWidget()
        branding_form = QFormLayout(branding_widget)
        self.branding_preset = QComboBox()
        for label, value in (("Чистый", "clean"), ("Продвижение канала", "promotion"), ("Только видео", "video")):
            self.branding_preset.addItem(label, value)
        self.show_subtitles = QCheckBox("Показывать субтитры")
        self.show_subtitles.setChecked(True)
        self.show_title = QCheckBox("Показывать верхний заголовок")
        self.original_title_label = QLabel("—")
        self.original_title_label.setWordWrap(True)
        self.original_title_source = QLabel("—")
        self.original_title_source.setProperty("class", "muted")
        self.original_title_source.setWordWrap(True)
        self.title_text = QLineEdit()
        self.title_text.setPlaceholderText("Заголовок Short")
        title_buttons_widget = QWidget()
        title_buttons = QGridLayout(title_buttons_widget)
        title_buttons.setContentsMargins(0, 0, 0, 0)
        title_buttons.setHorizontalSpacing(6)
        title_buttons.setVerticalSpacing(6)
        self.title_from_video = QPushButton("Взять название видео")
        self.title_from_video.setText("Взять исходное название")
        self.title_translate = QPushButton("Перевести локальной моделью")
        self.title_hook = QPushButton("Короткий заголовок")
        self.title_reset = QPushButton("Сбросить")
        self.title_translate.setMinimumWidth(210)
        for button in (self.title_from_video, self.title_translate, self.title_hook, self.title_reset):
            button.setMinimumHeight(30)
            button.setSizePolicy(button.sizePolicy().horizontalPolicy(), button.sizePolicy().verticalPolicy())
        title_buttons.addWidget(self.title_from_video, 0, 0)
        title_buttons.addWidget(self.title_translate, 0, 1)
        title_buttons.addWidget(self.title_hook, 1, 0)
        title_buttons.addWidget(self.title_reset, 1, 1)
        self.title_style = QComboBox()
        for label, value in (("Чистый", "clean"), ("Крупный", "large"), ("Игровой", "gaming")):
            self.title_style.addItem(label, value)
        self.title_size = QSpinBox(); self.title_size.setRange(36, 140); self.title_size.setValue(78)
        self.title_bold = QCheckBox("Жирный"); self.title_bold.setChecked(True)
        self.title_outline = QSpinBox(); self.title_outline.setRange(0, 16); self.title_outline.setValue(4)
        self.title_shadow = QSpinBox(); self.title_shadow.setRange(0, 16); self.title_shadow.setValue(2)
        self.title_y = QSpinBox(); self.title_y.setRange(60, 650); self.title_y.setValue(180)
        self.use_subtitle_font_for_title = QCheckBox("Использовать шрифт субтитров для заголовка")
        self.use_subtitle_font_for_title.setChecked(True)
        self.title_font = QComboBox()
        self._fill_font_combo(self.title_font, DEFAULT_FONT_FAMILY)
        self.title_alignment = QComboBox()
        for label, value in (("Слева", "left"), ("По центру", "center"), ("Справа", "right")):
            self.title_alignment.addItem(label, value)
        self.title_alignment.setCurrentIndex(max(0, self.title_alignment.findData("center")))
        self.title_offset_x = QSpinBox(); self.title_offset_x.setRange(-300, 300); self.title_offset_x.setSuffix(" px"); self.title_offset_x.setValue(0)
        self.show_channel_card = QCheckBox("Показывать карточку канала")
        self.channel_profile = QComboBox()
        self.channel_profile.addItem("Не выбрано", "")
        self.banner_import = QPushButton("Импорт / замена баннера")
        self.banner_preview = QLabel("Баннер не выбран")
        self.banner_preview.setWordWrap(True)
        self.banner_scale = QSpinBox(); self.banner_scale.setRange(20, 200); self.banner_scale.setSuffix("%"); self.banner_scale.setValue(100)
        self.banner_x = QSpinBox(); self.banner_x.setRange(-480, 480); self.banner_x.setSuffix(" px"); self.banner_x.setValue(0)
        self.banner_y = QSpinBox(); self.banner_y.setRange(-720, 240); self.banner_y.setSuffix(" px"); self.banner_y.setValue(0)
        self.banner_opacity = QSpinBox(); self.banner_opacity.setRange(0, 100); self.banner_opacity.setSuffix("%"); self.banner_opacity.setValue(100)
        self.banner_reset_position = QPushButton("Сбросить положение")
        self.banner_save_profile = QPushButton("Сохранить как настройки профиля канала")
        self._refresh_profiles()
        branding_form.addRow("Пресет", self.branding_preset)
        branding_form.addRow(self.show_subtitles)
        branding_form.addRow(self.show_title)
        branding_form.addRow("Исходное название", self.original_title_label)
        branding_form.addRow("Источник", self.original_title_source)
        branding_form.addRow("Заголовок", self.title_text)
        branding_form.addRow(title_buttons_widget)
        branding_form.addRow("Стиль заголовка", self.title_style)
        branding_form.addRow("Размер заголовка", self.title_size)
        branding_form.addRow(self.title_bold)
        branding_form.addRow("Обводка заголовка", self.title_outline)
        branding_form.addRow("Тень заголовка", self.title_shadow)
        branding_form.addRow("Y заголовка", self.title_y)
        branding_form.addRow(self.use_subtitle_font_for_title)
        branding_form.addRow("Шрифт заголовка", self.title_font)
        branding_form.addRow("Выравнивание заголовка", self.title_alignment)
        branding_form.addRow("X заголовка", self.title_offset_x)
        branding_form.addRow(self.show_channel_card)
        branding_form.addRow("Профиль канала", self.channel_profile)
        banner_actions = self._row(self.banner_import)
        self.banner_open_file = QPushButton("Открыть файл")
        self.banner_open_folder = QPushButton("Открыть папку")
        banner_actions.layout().addWidget(self.banner_open_file)
        banner_actions.layout().addWidget(self.banner_open_folder)
        branding_form.addRow(banner_actions)
        branding_form.addRow("Баннер", self.banner_preview)
        branding_form.addRow("Масштаб баннера", self.banner_scale)
        branding_form.addRow("Смещение по горизонтали", self.banner_x)
        branding_form.addRow("Смещение по вертикали", self.banner_y)
        branding_form.addRow("Прозрачность", self.banner_opacity)
        branding_form.addRow(self.banner_reset_position)
        branding_form.addRow(self.banner_save_profile)
        style_widget = QWidget()
        style_form = QFormLayout(style_widget)
        self.style = QComboBox()
        for label, value in (("Чистый", "clean"), ("Крупный", "large"), ("Игровой", "gaming")):
            self.style.addItem(label, value)
        self.subtitle_font = QComboBox()
        self._fill_font_combo(self.subtitle_font, DEFAULT_FONT_FAMILY)
        self.position = QComboBox()
        for label, value in (("Верхняя треть", "upper"), ("Центр", "center"), ("Нижняя треть", "lower")):
            self.position.addItem(label, value)
        self.subtitle_alignment = QComboBox()
        for label, value in (("По левому краю", "left"), ("По центру", "center"), ("По правому краю", "right")):
            self.subtitle_alignment.addItem(label, value)
        self.subtitle_alignment.setCurrentIndex(self.subtitle_alignment.findData("center"))
        self.subtitle_offset_x = QSpinBox(); self.subtitle_offset_x.setRange(-300, 300); self.subtitle_offset_x.setSuffix(" px"); self.subtitle_offset_x.setValue(0)
        self.size = QSpinBox(); self.size.setRange(44, 120); self.size.setValue(58)
        self.maximum = QSpinBox(); self.maximum.setRange(12, 80); self.maximum.setValue(36)
        self.maximum.setToolTip("Предварительный ориентир переноса. Финальная ширина рассчитывается по фактическому размеру текста в пикселях")
        self.lines = QSpinBox(); self.lines.setRange(1, 2); self.lines.setValue(2)
        self.outline = QSpinBox(); self.outline.setRange(0, 10); self.outline.setValue(3)
        self.shadow = QSpinBox(); self.shadow.setRange(0, 10); self.shadow.setValue(1)
        self.background = QCheckBox("Подложка под субтитрами")
        self.background.setToolTip("Добавляет полупрозрачную тёмную область за текстом, чтобы субтитры лучше читались на светлом фоне")
        self.margin = QSpinBox(); self.margin.setRange(80, 500); self.margin.setValue(120)
        self.auto_above_banner = QCheckBox("Автоматически размещать субтитры над баннером")
        self.auto_above_banner.setChecked(True)
        self.auto_above_banner.setToolTip("Не допускает пересечения области субтитров с карточкой канала")
        self.line_anchor_mode = QComboBox()
        for label, value in (
            ("Фиксировать первую строку", "first_line_fixed"),
            ("Прижимать к баннеру", "banner_bottom"),
            ("Центрировать блок", "block_center"),
        ):
            self.line_anchor_mode.addItem(label, value)
        self.line_anchor_mode.setToolTip(
            "Определяет поведение текста при переходе между одной и двумя строками"
        )
        self.banner_gap = QSpinBox()
        self.banner_gap.setRange(0, 150)
        self.banner_gap.setSingleStep(5)
        self.banner_gap.setSuffix(" px")
        self.banner_gap.setValue(15)
        self.banner_gap.setToolTip("Минимальное расстояние между областью субтитров и карточкой канала")
        self.offset = QSlider(Qt.Horizontal); self.offset.setRange(-300, 300); self.offset.setSingleStep(10); self.offset.setPageStep(10); self.offset.setValue(0)
        self.offset_value = QSpinBox(); self.offset_value.setRange(-300, 300); self.offset_value.setSingleStep(10); self.offset_value.setSuffix(" px")
        self.offset_reset = QPushButton("Сбросить")
        self.offset.valueChanged.connect(self.offset_value.setValue)
        self.offset_value.valueChanged.connect(self.offset.setValue)
        self.offset_reset.clicked.connect(lambda: self.offset.setValue(0))
        offset_row = QWidget()
        offset_layout = QHBoxLayout(offset_row)
        offset_layout.setContentsMargins(0, 0, 0, 0)
        offset_layout.addWidget(self.offset, 1)
        offset_layout.addWidget(self.offset_value)
        offset_layout.addWidget(self.offset_reset)
        for label, control in (("Стиль", self.style), ("Шрифт", self.subtitle_font), ("Положение", self.position), ("Выравнивание субтитров", self.subtitle_alignment), ("X субтитров", self.subtitle_offset_x), ("Размер", self.size), ("Примерная длина строки", self.maximum), ("Строк", self.lines), ("Обводка", self.outline), ("Тень", self.shadow), ("Безопасный отступ", self.margin)):
            style_form.addRow(label, control)
        style_form.addRow("Смещение по вертикали", offset_row)
        style_form.addRow(self.background)
        style_form.addRow(self.auto_above_banner)
        style_form.addRow("Положение при разном числе строк", self.line_anchor_mode)
        style_form.addRow("Расстояние до баннера", self.banner_gap)
        self.preview = RealtimeCompositionRenderer()
        self.preview_quality = QComboBox()
        for label, size, hint in (
            ("Быстрое 360×640", (360, 640), "Для плавного воспроизведения"),
            ("Среднее 540×960", (540, 960), "Оптимально для обычного редактирования"),
            ("Высокое 720×1280", (720, 1280), "Для проверки текста и композиции"),
            ("Full HD 1080×1920", (1080, 1920), "Для финальной проверки качества перед рендером"),
        ):
            self.preview_quality.addItem(label, size)
            self.preview_quality.setItemData(self.preview_quality.count() - 1, hint, Qt.ToolTipRole)
        self.preview_quality.setToolTip(
            "Качество изменяет внутреннее разрешение предпросмотра. Масштаб просмотра только "
            "увеличивает или уменьшает изображение и не добавляет детализации"
        )
        self._restore_preview_quality()
        self.preview.resized.connect(self._update_technical_info)
        self.preview_zoom = QComboBox()
        for label, value in (("Вписать", "fit"), ("50%", 50), ("75%", 75), ("100%", 100), ("200%", 200)):
            self.preview_zoom.addItem(label, value)
        self.preview_fit = QPushButton("Вписать в окно")
        self.preview_100 = QPushButton("100%")
        self.preview_detail = QPushButton("Проверить качество")
        self.preview_detail.setToolTip("Асинхронно создать точный FFmpeg-кадр через pipeline финального render")
        self.preview_interactive = QPushButton("Вернуться к интерактивному предпросмотру")
        self.preview_interactive.setVisible(False)
        self.preview_scroll = PreviewViewport()
        self.preview_scroll.setWidget(self.preview)
        self.preview_scroll.resized.connect(self._apply_preview_zoom)
        self.preview_scroll.zoom_requested.connect(self._zoom_preview_by)
        preview_panel = QWidget()
        preview_layout = QVBoxLayout(preview_panel)
        preview_layout.addWidget(self._row(QLabel("Качество композиции"), self.preview_quality))
        preview_layout.addWidget(self._row(QLabel("Масштаб просмотра"), self.preview_zoom, self.preview_fit, self.preview_100, self.preview_detail))
        preview_layout.addWidget(self.preview_interactive)
        preview_layout.addWidget(self.preview_scroll, 1)
        self.player = None
        self.audio = None
        self.video_sink = None
        if VERTICAL_MULTIMEDIA_AVAILABLE:
            self.audio = QAudioOutput(self)
            self.video_sink = QVideoSink(self)
            self.player = QMediaPlayer(self)
            self.player.setAudioOutput(self.audio)
            self.player.setVideoSink(self.video_sink)
            self.video_sink.videoFrameChanged.connect(self._video_frame_changed)
            self.player.positionChanged.connect(self._player_position_changed)
            self.player.playbackStateChanged.connect(self._playback_state_changed)
        self.timeline = SeekSlider(Qt.Horizontal)
        self.timeline.setRange(0, 0)
        self.timeline.seek_requested.connect(self._seek_relative)
        self.timeline.scrub_started.connect(self._begin_scrub)
        self.timeline.scrub_finished.connect(self._end_scrub)
        self.time_label = QLabel("00:00.000 / 00:00.000")
        timeline_row = QHBoxLayout()
        timeline_row.addWidget(self.timeline, 1)
        timeline_row.addWidget(self.time_label)
        preview_layout.addLayout(timeline_row)
        player_row = QHBoxLayout()
        self.play_pause = QPushButton("▶ / ⏸")
        self.play_pause.setToolTip("Воспроизвести или поставить вертикальный preview на паузу")
        self.play_pause.clicked.connect(self._toggle_playback)
        player_row.addWidget(self.play_pause)
        for label, delta in (("−5", -5.0), ("−1", -1.0), ("−0,1", -0.1), ("+0,1", .1), ("+1", 1.0), ("+5", 5.0)):
            button = QPushButton(label)
            button.setToolTip(f"Перемотать на {delta:+g} секунды")
            button.clicked.connect(lambda _checked=False, amount=delta: self._step_preview(amount))
            player_row.addWidget(button)
        self.preview_volume = QSlider(Qt.Horizontal)
        self.preview_volume.setRange(0, 100)
        self.preview_volume.setValue(80)
        self.preview_volume.setMaximumWidth(100)
        self.preview_volume.valueChanged.connect(lambda value: self.audio and self.audio.setVolume(value / 100))
        player_row.addWidget(QLabel("Громкость"))
        player_row.addWidget(self.preview_volume)
        self.fullscreen_preview = QPushButton("⛶")
        self.fullscreen_preview.setToolTip("Открыть полноэкранное предпросмотр")
        self.fullscreen_preview.clicked.connect(self._open_big_preview)
        self.refresh_preview = QPushButton("↻")
        self.refresh_preview.setToolTip("Обновить предпросмотр на текущем времени")
        self.refresh_preview.clicked.connect(self._refresh_current_preview)
        player_row.addWidget(self.refresh_preview)
        player_row.addWidget(self.fullscreen_preview)
        preview_layout.addLayout(player_row)
        self.preview_technical = QLabel("Предпросмотр: —\nРендер: —")
        self.preview_technical.setProperty("class", "muted")
        self.preview_technical.setWordWrap(True)
        preview_layout.addWidget(self.preview_technical)
        self.settings_layout.addWidget(self.vertical)
        self.settings_layout.addWidget(style_widget)
        self.settings_layout.addWidget(branding_widget)
        self.settings_layout.addStretch(1)
        self.top_splitter.addWidget(self.settings_scroll)
        self.top_splitter.addWidget(preview_panel)
        self.top_splitter.setStretchFactor(0, 1)
        self.top_splitter.setStretchFactor(1, 2)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(("Start", "End", "Text"))
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        table_panel = QWidget()
        table_layout = QVBoxLayout(table_panel)
        table_layout.setContentsMargins(0, 0, 0, 0)
        table_layout.addWidget(self.table, 1)
        actions = QHBoxLayout()
        for text, handler in (("Объединить", self._merge), ("Разделить", self._split), ("Удалить", self._delete), ("Восстановить исходный вариант", self._restore), ("Сохранить SRT и ASS", self._export), ("Рендер тестовых 5 секунд", self._test_render), ("Использовать эти настройки по умолчанию", self._save_defaults)):
            button = QPushButton(text)
            button.clicked.connect(handler)
            actions.addWidget(button)
        actions.addStretch(1)
        table_layout.addLayout(actions)
        self.main_splitter.addWidget(self.top_splitter)
        self.main_splitter.addWidget(table_panel)
        self.main_splitter.setStretchFactor(0, 3)
        self.main_splitter.setStretchFactor(1, 1)
        layout.addWidget(self.main_splitter, 1)
        self._restore_splitters()
        self.main_splitter.splitterMoved.connect(self._save_splitters)
        self.top_splitter.splitterMoved.connect(self._save_splitters)

        self.style.currentIndexChanged.connect(self._style_selected)
        self.title_style.currentIndexChanged.connect(self._title_style_selected)
        self.branding_preset.currentIndexChanged.connect(self._branding_preset_selected)
        self.title_from_video.clicked.connect(self._title_from_video_clicked)
        self.title_translate.clicked.connect(self._title_translate_clicked)
        self.title_hook.clicked.connect(self._title_hook_clicked)
        self.title_reset.clicked.connect(self._title_reset_clicked)
        self.banner_import.clicked.connect(self._import_banner)
        self.banner_open_file.clicked.connect(self._open_banner_file)
        self.banner_open_folder.clicked.connect(self._open_banner_folder)
        self.banner_reset_position.clicked.connect(self._reset_banner_position)
        self.banner_save_profile.clicked.connect(self._save_banner_profile_defaults)
        self.channel_profile.currentIndexChanged.connect(self._profile_selected)
        for control in (self.position, self.subtitle_alignment, self.subtitle_offset_x, self.subtitle_font, self.size, self.maximum, self.lines, self.outline, self.shadow, self.margin, self.offset, self.line_anchor_mode, self.banner_gap):
            signal = control.currentIndexChanged if isinstance(control, QComboBox) else control.valueChanged
            signal.connect(self._mark_dirty)
        for control in (self.branding_preset, self.title_font, self.title_alignment, self.title_offset_x, self.title_size, self.title_outline, self.title_shadow, self.title_y, self.channel_profile, self.banner_scale, self.banner_x, self.banner_y, self.banner_opacity):
            signal = control.currentIndexChanged if isinstance(control, QComboBox) else control.valueChanged
            signal.connect(self._mark_dirty)
        for checkbox in (self.show_subtitles, self.show_title, self.use_subtitle_font_for_title, self.title_bold, self.show_channel_card, self.auto_above_banner):
            checkbox.toggled.connect(self._mark_dirty)
        self.use_subtitle_font_for_title.toggled.connect(self.title_font.setDisabled)
        self.title_font.setDisabled(self.use_subtitle_font_for_title.isChecked())
        self.preview_quality.currentIndexChanged.connect(self._preview_quality_changed)
        self.preview_zoom.currentIndexChanged.connect(self._preview_zoom_changed)
        self.preview_fit.clicked.connect(lambda: self.preview_zoom.setCurrentIndex(self.preview_zoom.findData("fit")))
        self.preview_100.clicked.connect(lambda: self.preview_zoom.setCurrentIndex(self.preview_zoom.findData(100)))
        self.preview_detail.clicked.connect(self._check_quality)
        self.preview_interactive.clicked.connect(self._return_to_interactive_preview)
        self._restore_preview_zoom()
        self.title_text.textEdited.connect(self._mark_dirty)
        self.background.toggled.connect(self._mark_dirty)
        self.vertical.changed.connect(self._mark_dirty)
        self.table.itemChanged.connect(self._mark_dirty)
        self.table.cellClicked.connect(self._subtitle_row_clicked)

    @staticmethod
    def _row(*widgets: QWidget) -> QWidget:
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        for widget in widgets:
            layout.addWidget(widget)
        return row

    @staticmethod
    def _fill_font_combo(combo: QComboBox, preferred: str) -> None:
        families = QFontDatabase.families()
        priority = [DEFAULT_FONT_FAMILY, "Arial", "Arial Black", "Tahoma", "Verdana"]
        ordered = []
        for family in priority + families:
            if family and family not in ordered:
                ordered.append(family)
        combo.clear()
        for family in ordered:
            info = resolve_font(family)
            suffix = " · fallback" if info.fallback else ""
            combo.addItem(f"{family}{suffix}", family)
        combo.setCurrentIndex(max(0, combo.findData(preferred or DEFAULT_FONT_FAMILY)))

    def _restore_preview_quality(self) -> None:
        settings = QSettings("CreatorAssistant", "CreatorAssistant")
        stored = str(settings.value("shorts/vertical_editor/preview_quality", "540x960") or "540x960")
        mapping = {"360x640": 0, "540x960": 1, "720x1280": 2, "1080x1920": 3}
        self.preview_quality.setCurrentIndex(mapping.get(stored, 1))
        self._apply_preview_quality()

    def _preview_quality_changed(self) -> None:
        self._apply_preview_quality()
        size = self.preview_quality.currentData() or (540, 960)
        settings = QSettings("CreatorAssistant", "CreatorAssistant")
        settings.setValue("shorts/vertical_editor/preview_quality", f"{int(size[0])}x{int(size[1])}")
        self._preview_generation_id += 1
        self._sync_preview_time(self._preview_position_ms)
        self._mark_exact_stale()

    def _apply_preview_quality(self) -> None:
        size = self.preview_quality.currentData() if hasattr(self, "preview_quality") else (540, 960)
        width, height = size if isinstance(size, (tuple, list)) and len(size) == 2 else (540, 960)
        if hasattr(self, "preview"):
            self.preview.set_target_preview_size(int(width), int(height))
        if hasattr(self, "preview_scroll"):
            self._apply_preview_zoom()

    def _restore_preview_zoom(self) -> None:
        settings = QSettings("CreatorAssistant", "CreatorAssistant")
        stored = str(settings.value("shorts/vertical_editor/preview_zoom", "fit") or "fit")
        value = "fit" if stored == "fit" else int(stored) if stored.isdigit() else "fit"
        index = self.preview_zoom.findData(value)
        self.preview_zoom.blockSignals(True)
        self.preview_zoom.setCurrentIndex(max(0, index))
        self.preview_zoom.blockSignals(False)
        QTimer.singleShot(0, self._apply_preview_zoom)

    def _preview_zoom_changed(self) -> None:
        value = self.preview_zoom.currentData() or "fit"
        QSettings("CreatorAssistant", "CreatorAssistant").setValue("shorts/vertical_editor/preview_zoom", value)
        self._apply_preview_zoom()

    def _zoom_preview_by(self, delta: int) -> None:
        current = self.preview_zoom.currentData()
        current_value = 50 if current == "fit" else int(current or 50)
        choices = [50, 75, 100, 200]
        target = min(choices, key=lambda item: abs(item - max(50, min(200, current_value + delta))))
        self.preview_zoom.setCurrentIndex(self.preview_zoom.findData(target))

    def _apply_preview_zoom(self) -> None:
        if not hasattr(self, "preview_scroll"):
            return
        quality = self.preview_quality.currentData() or (540, 960)
        if not isinstance(quality, (tuple, list)) or len(quality) != 2:
            quality = (540, 960)
        composition_width, composition_height = (int(quality[0]), int(quality[1]))
        zoom = self.preview_zoom.currentData() or "fit"
        if zoom == "fit":
            available_width = max(1, self.preview_scroll.viewport().width() - 4)
            available_height = max(1, self.preview_scroll.viewport().height() - 4)
            scale = min(available_width / 9, available_height / 16)
            display_width = max(216, round(9 * scale))
            display_height = max(384, round(16 * scale))
        else:
            factor = int(zoom) / 100
            display_width = max(216, round(composition_width * factor))
            display_height = max(384, round(composition_height * factor))
        self.preview.setFixedSize(display_width, display_height)
        self._update_technical_info()

    def _restore_splitters(self) -> None:
        settings = QSettings("CreatorAssistant", "CreatorAssistant")
        main = settings.value("shorts/vertical_editor/main_splitter")
        top = settings.value("shorts/vertical_editor/top_splitter")
        if isinstance(main, list) and all(str(item).isdigit() for item in main):
            self.main_splitter.setSizes([int(item) for item in main])
        else:
            self.main_splitter.setSizes([720, 260])
        if isinstance(top, list) and all(str(item).isdigit() for item in top):
            self.top_splitter.setSizes([360, 640])
            self.top_splitter.setSizes([int(item) for item in top])
        else:
            self.top_splitter.setSizes([420, 720])

    def _save_splitters(self) -> None:
        settings = QSettings("CreatorAssistant", "CreatorAssistant")
        settings.setValue("shorts/vertical_editor/main_splitter", self.main_splitter.sizes())
        settings.setValue("shorts/vertical_editor/top_splitter", self.top_splitter.sizes())

    def _open_big_preview(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("Большое предпросмотр Short")
        dialog.resize(540, 960)
        layout = QVBoxLayout(dialog)
        preview = VerticalFramePreview()
        preview.setMinimumSize(540, 960)
        if not self.preview.video_frame.isNull():
            preview.set_video_frame(self.preview.video_frame)
        sample = self._cues()[0].text if self._cues() else "Пример безопасных субтитров"
        preview.set_preview(self.candidate, self.current_settings(), self.vertical.value(), sample, self.current_branding_settings())
        layout.addWidget(preview, 1)
        dialog.exec()

    def _refresh_current_preview(self) -> None:
        self._preview_generation_id += 1
        self._mark_exact_stale()
        if self.player and self.candidate:
            self.player.setPosition(round(self.candidate.start * 1000) + self._preview_position_ms)
        self._sync_preview_time(self._preview_position_ms)

    def apply_exact_preview_frame(self, image: QImage, generation_id: int) -> bool:
        """Apply an asynchronous exact frame only when it belongs to the latest composition."""
        if generation_id != self._preview_generation_id or image.isNull():
            return False
        self._preview_state = "Точный кадр FFmpeg"
        self.preview.set_exact_frame(image)
        self.preview_interactive.setVisible(True)
        self._update_technical_info()
        return True

    def set_technical_context(self, *, source_info=None, proxy_info=None, proxy_path: Path | None = None, render_encoder: str = "", ffmpeg_path: str = "") -> None:
        self._source_info = source_info
        self._proxy_info = proxy_info
        self._proxy_path = proxy_path
        self._render_encoder = render_encoder
        self._ffmpeg_path = str(ffmpeg_path or self._ffmpeg_path)
        self._update_technical_info()

    def _update_technical_info(self) -> None:
        if not hasattr(self, "preview_technical"):
            return
        display_w, display_h = self.preview.effective_preview_size()
        composition_w, composition_h = self.preview.composition_size
        exact = self._preview_state.startswith("Точный кадр FFmpeg")
        preview_info = self._source_info if exact else (self._proxy_info or self._source_info)
        preview_fps = _format_fps(float(getattr(preview_info, "fps", 0.0) or 0.0))
        source_label = "Original" if exact and self._source_info else ("Proxy" if self._proxy_info else ("Источник" if self._source_info else "—"))
        render_fps = _format_fps(float(getattr(self._source_info, "fps", 0.0) or 0.0))
        encoder = self._render_encoder or "H.264 NVENC"
        zoom = self.preview_zoom.currentData() if hasattr(self, "preview_zoom") else "fit"
        zoom_label = "Вписано в окно" if zoom == "fit" else f"{int(zoom or 100)}%"
        source_resolution = (
            f"{getattr(self._source_info, 'width', 0)}×{getattr(self._source_info, 'height', 0)} original"
            if self._source_info else "—"
        )
        playback_resolution = (
            f"{getattr(self._proxy_info, 'width', 0)}×{getattr(self._proxy_info, 'height', 0)} proxy"
            if self._proxy_info else source_resolution
        )
        self.preview_technical.setText(
            f"Качество композиции: {composition_w}×{composition_h}\n"
            f"Источник видео: {source_resolution}\n"
            f"Playback: {playback_resolution} · {preview_fps} FPS\n"
            f"Точный кадр: {composition_w}×{composition_h} FFmpeg · {self._preview_state}\n"
            f"Отображение: {zoom_label} · {display_w}×{display_h} · {source_label}\n"
            f"Итоговый рендер: 1080×1920 · {render_fps} FPS · {encoder}"
        )
        proxy_resolution = (
            f"{getattr(self._proxy_info, 'width', 0)}×{getattr(self._proxy_info, 'height', 0)} · "
            f"{_format_fps(float(getattr(self._proxy_info, 'fps', 0.0) or 0.0))} FPS"
            if self._proxy_info else "—"
        )
        self.preview_technical.setToolTip(
            "Диагностика preview/render\n"
            f"Proxy-файл: {self._proxy_path or '—'}\n"
            f"Proxy: {proxy_resolution}\n"
            f"Внутренняя композиция: {composition_w}×{composition_h}\n"
            f"Realtime viewport: {display_w}×{display_h} · {preview_fps} FPS · {source_label}\n"
            f"Точный single-frame preview: {composition_w}×{composition_h}\n"
            f"Итоговый render: 1080×1920 · {render_fps} FPS · {encoder}\n"
            f"{self._font_diagnostics()}"
        )

    def _font_diagnostics(self) -> str:
        if not hasattr(self, "subtitle_font"):
            return "Fonts: —"
        subtitle_family = str(self.subtitle_font.currentData() or DEFAULT_FONT_FAMILY)
        subtitle_info = resolve_font(subtitle_family)
        title_family = subtitle_family
        if hasattr(self, "use_subtitle_font_for_title") and not self.use_subtitle_font_for_title.isChecked():
            title_family = str(self.title_font.currentData() or DEFAULT_FONT_FAMILY)
        title_info = resolve_font(title_family)
        subtitle_file = subtitle_info.file_for_weight(True)
        title_file = title_info.file_for_weight(bool(self.title_bold.isChecked()) if hasattr(self, "title_bold") else True)
        return (
            "Fonts:\n"
            f"  Subtitle: family={subtitle_info.family}; ASS FontName={subtitle_info.ass_font_name}; "
            f"file={subtitle_file or '—'}; fallback={subtitle_info.fallback}\n"
            f"  Title: family={title_info.family}; drawtext file={title_file or '—'}; "
            f"fallback={title_info.fallback}"
        )

    def _clip_duration_ms(self) -> int:
        return max(0, round((self.candidate.duration if self.candidate else 0) * 1000))

    def _toggle_playback(self) -> None:
        if not self.player or not self.candidate:
            return
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.pause()
            return
        start = round(self.candidate.start * 1000)
        end = round(self.candidate.end * 1000)
        if self.player.position() < start or self.player.position() >= end:
            self.player.setPosition(start)
        self.player.play()

    @Slot(object)
    def _playback_state_changed(self, state) -> None:
        if not self.player:
            return
        if state == QMediaPlayer.PlayingState:
            self._return_to_interactive_preview()

    def _seek_relative(self, relative_ms: int) -> None:
        if not self.candidate:
            return
        relative_ms = max(0, min(int(relative_ms), self._clip_duration_ms()))
        self._preview_position_ms = relative_ms
        if self.player:
            self.player.setPosition(round(self.candidate.start * 1000) + relative_ms)
        self._sync_preview_time(relative_ms)
        self._mark_exact_stale()

    def _step_preview(self, seconds: float) -> None:
        self._seek_relative(self._preview_position_ms + round(seconds * 1000))

    def _begin_scrub(self) -> None:
        if not self.player:
            return
        self._resume_after_scrub = self.player.playbackState() == QMediaPlayer.PlayingState
        if self._resume_after_scrub:
            self.player.pause()

    def _end_scrub(self, relative_ms: int) -> None:
        self._seek_relative(relative_ms)
        if self.player and self._resume_after_scrub:
            self.player.play()
        self._resume_after_scrub = False

    @Slot(object)
    def _video_frame_changed(self, frame) -> None:
        if not frame or not frame.isValid():
            return
        image = frame.toImage()
        if not image.isNull():
            playing = bool(self.player and self.player.playbackState() == QMediaPlayer.PlayingState)
            if playing or self.preview.exact_frame.isNull():
                self._preview_state = "Быстрый предпросмотр"
            self.preview.set_video_frame(image, preserve_exact=not playing)
            self._update_technical_info()

    def _schedule_exact_preview(self) -> None:
        # Kept as a compatibility hook: exact FFmpeg preview is explicit-only.
        return

    def _check_quality(self) -> None:
        if self.player and self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.pause()
        self._request_exact_preview()

    def _return_to_interactive_preview(self) -> None:
        self.preview.invalidate_exact_frame()
        self.preview_interactive.setVisible(False)
        self._preview_state = "Интерактивный предпросмотр"
        self._update_preview()
        self._update_technical_info()

    def _mark_exact_stale(self) -> None:
        had_exact = not self.preview.exact_frame.isNull()
        self.preview.invalidate_exact_frame()
        self.preview_interactive.setVisible(False)
        self._preview_state = (
            "Композиция изменена — точный кадр требует обновления"
            if had_exact else "Интерактивный предпросмотр"
        )

    def _request_exact_preview(self) -> None:
        if not self.candidate or not self.paths or not self._source_info or not self._ffmpeg_path:
            return
        if self.player and self.player.playbackState() == QMediaPlayer.PlayingState:
            return
        if self._exact_process and self._exact_process.state() != QProcess.NotRunning:
            self._exact_process.kill()
        generation = self._preview_generation_id
        cache = Path(self.paths.cache)
        cache.mkdir(parents=True, exist_ok=True)
        ass = cache / f"{self.candidate.id}.exact_preview.ass"
        cue_text = self.preview.sample.strip()
        cues = [SubtitleCue(0.0, 1.0, cue_text)] if cue_text else []
        settings = self.current_settings()
        branding = self.current_branding_settings()
        self.service.write(cues, cache / f"{self.candidate.id}.exact_preview.srt", ass, settings, branding)
        exact_candidate = replace(self.candidate)
        exact_candidate.subtitle_settings = settings
        exact_candidate.layout_settings = self.vertical.value()
        exact_candidate.branding_settings = branding
        banner = Path(str(branding.get("channel_banner_path") or ""))
        has_banner = bool(branding.get("show_channel_card", False)) and banner.is_file()
        quality = self.preview_quality.currentData() or (540, 960)
        graph = ShortsFilterGraphBuilder().build(
            exact_candidate, self._source_info, ass.name if cues and branding.get("show_subtitles", True) else "",
            input_clipped=True, has_channel_banner=has_banner,
            output_size=(int(quality[0]), int(quality[1])),
        )
        # A still image has no audio output; discard the builder's audio branch.
        graph = graph.rsplit(";[0:a:0]", 1)[0]
        target = cache / f"{self.candidate.id}.exact_preview.{generation}.{quality[0]}x{quality[1]}.png"
        target.unlink(missing_ok=True)
        args = ["-hide_banner", "-loglevel", "error", "-y", "-ss", f"{self.candidate.start + self._preview_position_ms / 1000:.3f}", "-i", str(self._source_info.path)]
        if has_banner:
            args += ["-loop", "1", "-i", str(banner)]
        args += ["-filter_complex", graph, "-map", "[v]", "-frames:v", "1", str(target)]
        process = QProcess(self)
        process.setWorkingDirectory(str(cache))
        process.finished.connect(lambda code, _status, p=process, g=generation, t=target: self._exact_preview_finished(p, g, t, code))
        self._exact_process = process
        self._exact_target = target
        self._exact_generation = generation
        self._preview_state = f"Точный кадр FFmpeg: создание {quality[0]}×{quality[1]}"
        self.preview_detail.setEnabled(False)
        self._update_technical_info()
        process.start(self._ffmpeg_path, args)

    def _exact_preview_finished(self, process: QProcess, generation: int, target: Path, exit_code: int) -> None:
        if process is not self._exact_process:
            process.deleteLater()
            return
        self._exact_process = None
        self.preview_detail.setEnabled(True)
        if exit_code == 0 and target.is_file():
            self.apply_exact_preview_frame(QImage(str(target)), generation)
        else:
            error = bytes(process.readAllStandardError()).decode("utf-8", "replace").strip()
            logging.getLogger("creator_assistant").warning("Exact Shorts preview failed: %s", error)
            self._preview_state = "Быстрый предпросмотр"
            self._update_technical_info()
        process.deleteLater()

    @Slot(int)
    def _player_position_changed(self, absolute_ms: int) -> None:
        if not self.candidate:
            return
        start = round(self.candidate.start * 1000)
        duration = self._clip_duration_ms()
        relative = max(0, min(duration, int(absolute_ms) - start))
        self._preview_position_ms = relative
        self.timeline.setValue(relative)
        self._sync_preview_time(relative)
        if self.player and absolute_ms >= start + duration:
            self.player.pause()
            self.player.setPosition(start + duration)

    def _sync_preview_time(self, relative_ms: int) -> None:
        duration = self._clip_duration_ms()
        self.time_label.setText(f"{format_time(relative_ms)} / {format_time(duration)}")
        cue_text = ""
        active_row = -1
        seconds = relative_ms / 1000
        for row, cue in enumerate(self._cues()):
            if cue.start <= seconds < cue.end:
                cue_text, active_row = cue.text, row
                break
        if active_row >= 0 and self.table.currentRow() != active_row:
            self.table.blockSignals(True)
            self.table.selectRow(active_row)
            self.table.blockSignals(False)
        self.preview.set_preview(
            self.candidate, self.current_settings(), self.vertical.value(), cue_text,
            self.current_branding_settings(),
        )

    def _subtitle_row_clicked(self, row: int, _column: int) -> None:
        cues = self._cues()
        if 0 <= row < len(cues):
            self._seek_relative(round(cues[row].start * 1000))

    def _refresh_profiles(self) -> None:
        current = self.channel_profile.currentData() if hasattr(self, "channel_profile") else ""
        self.channel_profile.blockSignals(True)
        self.channel_profile.clear()
        self.channel_profile.addItem("Не выбрано", "")
        for profile in self.assets.profiles():
            self.channel_profile.addItem(profile.display_name, profile.id)
            index = self.channel_profile.count() - 1
            self.channel_profile.setItemData(index, f"{profile.handle} · {', '.join(profile.aliases)}", Qt.ToolTipRole)
        if current:
            self.channel_profile.setCurrentIndex(max(0, self.channel_profile.findData(current)))
        self.channel_profile.blockSignals(False)

    def _load_branding(self, settings: dict) -> None:
        self._refresh_profiles()
        self.branding_preset.setCurrentIndex(max(0, self.branding_preset.findData(settings.get("preset", "clean"))))
        self.show_subtitles.setChecked(bool(settings.get("show_subtitles", True)))
        self.show_title.setChecked(bool(settings.get("show_title", False)))
        title_suggestions = dict(settings.get("title_suggestions") or {})
        self.title_text.setText(str(
            title_suggestions.get("manual_title")
            or settings.get("final_title_text")
            or settings.get("short_hook_title")
            or ""
        ))
        self.original_title_label.setText(str(settings.get("original_video_title") or "—"))
        self.original_title_source.setText(str(settings.get("original_video_title_source") or "—"))
        self.title_style.setCurrentIndex(max(0, self.title_style.findData(settings.get("title_style", "clean"))))
        self.title_size.setValue(int(settings.get("title_size", 78)))
        self.title_bold.setChecked(bool(settings.get("title_bold", True)))
        self.title_outline.setValue(int(settings.get("title_outline", 4)))
        self.title_shadow.setValue(int(settings.get("title_shadow", 2)))
        self.title_y.setValue(int(settings.get("title_y", 180)))
        self.use_subtitle_font_for_title.setChecked(bool(settings.get("use_subtitle_font_for_title", True)))
        self.title_font.setCurrentIndex(max(0, self.title_font.findData(str(settings.get("title_font_family") or DEFAULT_FONT_FAMILY))))
        self.title_font.setDisabled(self.use_subtitle_font_for_title.isChecked())
        self.title_alignment.setCurrentIndex(max(0, self.title_alignment.findData(str(settings.get("title_alignment", "center")))))
        self.title_offset_x.setValue(int(settings.get("title_offset_x", 0)))
        profile_id = str(settings.get("channel_profile_id", ""))
        self.channel_profile.setCurrentIndex(max(0, self.channel_profile.findData(profile_id)))
        self.show_channel_card.setChecked(bool(settings.get("show_channel_card", False)))
        self.banner_scale.setValue(int(settings.get("banner_scale", 100)))
        # Legacy banner_x was absolute, so an unknown old X=50 must not become a +50 offset.
        self.banner_x.setValue(int(settings.get("banner_offset_x", 0)))
        self.banner_y.setValue(int(settings.get("banner_offset_y", int(settings.get("banner_y", 1600)) - 1600 if "banner_y" in settings else 0)))
        self.banner_opacity.setValue(int(settings.get("banner_opacity", 100)))
        self._update_banner_label()

    def _profile_selected(self) -> None:
        self._update_banner_label()
        self._mark_dirty()

    def _update_banner_label(self) -> None:
        profile_id = str(self.channel_profile.currentData() or "")
        path = self.assets.banner_path(profile_id)
        self.banner_preview.setText(f"{path.name}\nБаннер загружен ✓" if path else "Баннер не выбран")
        self.banner_preview.setToolTip(str(path or ""))

    def _branding_preset_selected(self) -> None:
        if self._loading:
            return
        preset = str(self.branding_preset.currentData())
        if preset == "video":
            self.show_title.setChecked(False)
            self.show_channel_card.setChecked(False)
            self.show_subtitles.setChecked(False)
        elif preset == "promotion":
            self.show_channel_card.setChecked(True)
            self.show_title.setChecked(True)
            self.show_subtitles.setChecked(True)
            self.auto_above_banner.setChecked(True)
            self.line_anchor_mode.setCurrentIndex(self.line_anchor_mode.findData("first_line_fixed"))
        else:
            self.show_title.setChecked(False)
            self.show_channel_card.setChecked(False)
            self.show_subtitles.setChecked(True)
        self._mark_dirty()

    def _title_from_video_clicked(self) -> None:
        if not self.candidate:
            return
        value = str((self.candidate.branding_settings or {}).get("original_video_title") or getattr(self.candidate, "title", "") or "")
        if value:
            self.title_text.setText(value)
            self._mark_dirty()

    def _title_translate_clicked(self) -> None:
        if not self.candidate:
            return
        branding = dict(self.candidate.branding_settings or {})
        translated = str(branding.get("translated_video_title") or "").strip()
        if not translated:
            QMessageBox.warning(
                self,
                "Перевод названия",
                "Для этого проекта русский перевод ещё не подготовлен. Запустите анализ или ручную подготовку AI-заголовков.",
            )
            return
        self.title_text.setText(translated)
        self.show_title.setChecked(True)
        self._mark_dirty()

    def _title_hook_clicked(self) -> None:
        if not self.candidate:
            return
        branding = dict(self.candidate.branding_settings or {})
        title_suggestions = dict(branding.get("title_suggestions") or {})
        suggestions = list(title_suggestions.get("suggestions") or branding.get("short_hook_suggestions") or [])
        if not suggestions:
            QMessageBox.information(
                self,
                "Короткие заголовки",
                "Для этого кандидата варианты заголовка ещё не подготовлены.",
            )
            return
        self._hooks_ready(title_suggestions or {"suggestions": suggestions})

    def _run_ai_title_task(self, function, callback) -> None:
        if not isinstance(self.semantic_backend, OllamaSemanticScorer):
            QMessageBox.warning(
                self, "Локальная модель",
                "Ollama не включена. Выберите локальную модель в настройках Shorts AI.",
            )
            return
        if self._ai_thread and self._ai_thread.isRunning():
            return
        candidate_id = self.candidate.id if self.candidate else ""
        self.title_translate.setEnabled(False)
        self.title_hook.setEnabled(False)
        self.dirty_label.setText(f"Ollama · {self.semantic_backend.model} · обработка…")
        thread = QThread(self)
        worker = _AiTitleWorker(function)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)

        def ready(value) -> None:
            if self.candidate and self.candidate.id == candidate_id:
                callback(value)

        def failed(message: str) -> None:
            QMessageBox.warning(self, "Локальная модель", message)
            self._set_saved_state()

        worker.finished.connect(ready)
        worker.failed.connect(failed)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        worker.failed.connect(worker.deleteLater)
        thread.finished.connect(self._ai_task_finished)
        self._ai_thread, self._ai_worker = thread, worker
        thread.start()

    def _ai_task_finished(self) -> None:
        self.title_translate.setEnabled(True)
        self.title_hook.setEnabled(True)
        if self._ai_thread:
            self._ai_thread.deleteLater()
        self._ai_thread = None
        self._ai_worker = None

    def _translation_ready(self, translated: str) -> None:
        branding = dict(self.candidate.branding_settings or {})
        branding["translated_video_title"] = str(translated).strip()
        self.candidate.branding_settings = branding
        self.title_text.setText(str(translated).strip())
        self.show_title.setChecked(True)
        self._mark_dirty()

    def _hooks_ready(self, response) -> None:
        if hasattr(response, "model_dump"):
            payload = response.model_dump()
        elif isinstance(response, dict):
            payload = dict(response)
        else:
            payload = {
                "suggestions": [
                    item.model_dump() if hasattr(item, "model_dump") else item
                    for item in getattr(response, "suggestions", [])
                ],
                "recommended_id": getattr(response, "recommended_id", ""),
            }
        suggestions = [
            item if isinstance(item, dict) else item.model_dump()
            for item in list(payload.get("suggestions") or [])
            if str((item.get("text") if isinstance(item, dict) else getattr(item, "text", "")) or "").strip()
        ]
        if not suggestions:
            QMessageBox.information(
                self,
                "Короткие заголовки",
                "Для этого кандидата варианты заголовка ещё не подготовлены.",
            )
            self._set_saved_state()
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("Выберите короткий заголовок")
        dialog.resize(620, 300)
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel("Выберите подготовленный вариант. После выбора текст можно редактировать."))
        choices = QListWidget()
        recommended_id = str(payload.get("recommended_id") or "")
        recommended_row = 0
        for row, suggestion in enumerate(suggestions):
            marker = "  ·  Рекомендуется" if str(suggestion.get("id") or "") == recommended_id else ""
            choices.addItem(f"{suggestion.get('text', '')}  ·  {suggestion.get('score', '—')}/100{marker}\n{suggestion.get('reason', '')}")
            choices.item(choices.count() - 1).setData(Qt.UserRole, suggestion)
            if marker:
                recommended_row = row
        choices.setCurrentRow(recommended_row)
        layout.addWidget(choices, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() != QDialog.Accepted or not choices.currentItem():
            self._set_saved_state()
            return
        selected = choices.currentItem().data(Qt.UserRole) or {}
        hook = str(selected.get("text") if isinstance(selected, dict) else selected).strip()
        branding = dict(self.candidate.branding_settings or {})
        title_suggestions = dict(branding.get("title_suggestions") or {})
        if title_suggestions:
            title_suggestions["selected_id"] = selected.get("id") if isinstance(selected, dict) else None
            title_suggestions["manual_title"] = None
            branding["title_suggestions"] = title_suggestions
        branding["short_hook_title"] = hook
        branding["short_hook_suggestions"] = suggestions
        self.candidate.branding_settings = branding
        self.title_text.setText(hook)
        self.show_title.setChecked(True)
        self._mark_dirty()

    def _title_reset_clicked(self) -> None:
        self.title_text.clear()
        self.show_title.setChecked(False)
        self._mark_dirty()

    def _open_banner_file(self) -> None:
        path = self.assets.banner_path(str(self.channel_profile.currentData() or ""))
        if path and path.is_file():
            import os
            os.startfile(str(path))

    def _open_banner_folder(self) -> None:
        path = self.assets.banner_path(str(self.channel_profile.currentData() or ""))
        if path and path.is_file():
            import os
            os.startfile(str(path.parent))

    def _import_banner(self) -> None:
        selected, _ = QFileDialog.getOpenFileName(
            self,
            "Выберите баннер канала",
            str(Path.home()),
            "Изображения (*.png *.jpg *.jpeg *.webp);;Все файлы (*)",
        )
        if not selected:
            return
        profile_id = str(self.channel_profile.currentData() or "").strip() or Path(selected).stem.replace("_subscribe", "")
        profile = self.assets.import_banner(profile_id, Path(selected), {
            "display_name": profile_id,
            "source_author": profile_id,
            "aliases": [profile_id],
        })
        self._refresh_profiles()
        self.channel_profile.setCurrentIndex(max(0, self.channel_profile.findData(profile.id)))
        self.show_channel_card.setChecked(True)
        self._update_banner_label()
        self._mark_dirty()

    def set_context(self, candidate, transcript, paths) -> None:
        if self._dirty:
            self._apply_configuration()
        self._loading = True
        self.candidate, self.transcript, self.paths = candidate, transcript, paths
        self.heading.setText(f"{candidate.id} · настройки сохраняются автоматически")
        settings = candidate.subtitle_settings or {}
        stored_cues = settings.get("cues")
        if stored_cues:
            cues = [SubtitleCue(float(item["start"]), float(item["end"]), str(item["text"])) for item in stored_cues]
        else:
            srt = paths.subtitles / f"{candidate.id}.srt"
            cues = self.service.parse_srt(srt) if srt.is_file() else self.service.generate(transcript, candidate, self.maximum.value(), self.lines.value())
        self.original = deepcopy(self.service.generate(transcript, candidate, self.maximum.value(), self.lines.value()))
        self.style.setCurrentIndex(max(0, self.style.findData(settings.get("style", "clean"))))
        self.position.setCurrentIndex(max(0, self.position.findData(settings.get("position", "lower"))))
        self.subtitle_alignment.setCurrentIndex(max(0, self.subtitle_alignment.findData(settings.get("alignment", "center"))))
        self.subtitle_offset_x.setValue(int(settings.get("horizontal_offset", 0)))
        preset = STYLE_PRESETS.get(str(settings.get("style", "clean")), STYLE_PRESETS["clean"])
        font_family = str(settings.get("font_family") or preset.get("font") or DEFAULT_FONT_FAMILY)
        self.subtitle_font.setCurrentIndex(max(0, self.subtitle_font.findData(font_family)))
        self.size.setValue(int(settings.get("size", preset["size"])))
        self.maximum.setValue(int(settings.get("maximum", 36)))
        self.lines.setValue(min(2, int(settings.get("lines", 2))))
        self.outline.setValue(int(settings.get("outline", preset["outline"])))
        self.shadow.setValue(int(settings.get("shadow", preset["shadow"])))
        self.background.setChecked(bool(settings.get("background", False)))
        self.auto_above_banner.setChecked(bool(settings.get("auto_above_banner", True)))
        self.line_anchor_mode.setCurrentIndex(max(0, self.line_anchor_mode.findData(settings.get("line_anchor_mode", "first_line_fixed"))))
        self.banner_gap.setValue(max(0, min(150, int(settings.get("banner_gap", 15) or 0))))
        self.margin.setValue(int(settings.get("safe_margin", 120)))
        self.offset.setValue(int(settings.get("vertical_offset", 0)))
        self.vertical.set_value(candidate.layout_settings or {})
        self._load_branding(candidate.branding_settings or {})
        self._show(cues)
        duration_ms = self._clip_duration_ms()
        self.timeline.setRange(0, duration_ms)
        self.timeline.setValue(0)
        self._preview_position_ms = 0
        self.time_label.setText(f"{format_time(0)} / {format_time(duration_ms)}")
        if self.player and hasattr(paths, "cache"):
            proxy = Path(paths.cache) / "analysis_proxy.mp4"
            self.player.pause()
            if proxy.is_file():
                self.player.setSource(QUrl.fromLocalFile(str(proxy)))
                self.player.setPosition(round(candidate.start * 1000))
        self._loading = False
        self._dirty = False
        self._update_preview()
        self._mark_exact_stale()
        self._update_technical_info()
        self._set_saved_state()

    def rebuild_for_boundaries(self, candidate, transcript) -> list[SubtitleCue]:
        """Rebuild only this candidate's local cues after its start/end changes."""
        if self.candidate is not candidate:
            return []
        self._save_timer.stop()
        settings = self.current_settings()
        cues = self.service.generate(
            transcript,
            candidate,
            int(settings.get("maximum", 36)),
            min(2, int(settings.get("lines", 2))),
        )
        self.transcript = transcript
        self.original = deepcopy(cues)
        self._show(cues)
        settings["cues"] = [
            {"start": cue.start, "end": cue.end, "text": cue.text}
            for cue in cues
        ]
        candidate.subtitle_settings = settings
        candidate.layout_settings = self.vertical.value()
        self._dirty = False
        self._update_preview()
        self._set_saved_state()
        return cues

    def _show(self, cues: list[SubtitleCue]) -> None:
        self.table.blockSignals(True)
        self.table.setRowCount(len(cues))
        for row, cue in enumerate(cues):
            self.table.setItem(row, 0, QTableWidgetItem(f"{cue.start:.3f}"))
            self.table.setItem(row, 1, QTableWidgetItem(f"{cue.end:.3f}"))
            self.table.setItem(row, 2, QTableWidgetItem(cue.text))
        self.table.blockSignals(False)
        self.table.resizeRowsToContents()

    def _cues(self) -> list[SubtitleCue]:
        result = []
        for row in range(self.table.rowCount()):
            try:
                start = float(self.table.item(row, 0).text().replace(",", "."))
                end = float(self.table.item(row, 1).text().replace(",", "."))
            except (AttributeError, ValueError):
                continue
            text = self.table.item(row, 2).text() if self.table.item(row, 2) else ""
            clip_duration = self.candidate.duration if self.candidate else end
            end = min(end, clip_duration)
            if text.strip() and end > start >= 0 and start < clip_duration:
                result.append(SubtitleCue(start, end, text.strip()))
        return result

    def current_settings(self) -> dict:
        return {
            "style": self.style.currentData(), "position": self.position.currentData(),
            "alignment": HorizontalTextAlignment.parse(self.subtitle_alignment.currentData()).value,
            "horizontal_offset": self.subtitle_offset_x.value(),
            "font_family": self.subtitle_font.currentData() or DEFAULT_FONT_FAMILY,
            "size": self.size.value(), "maximum": self.maximum.value(), "lines": self.lines.value(),
            "outline": self.outline.value(), "shadow": self.shadow.value(),
            "background": self.background.isChecked(), "safe_margin": self.margin.value(),
            "vertical_offset": self.offset.value(),
            "auto_above_banner": self.auto_above_banner.isChecked(),
            "line_anchor_mode": self.line_anchor_mode.currentData() or "first_line_fixed",
            "banner_gap": self.banner_gap.value(),
            "minimum_size": 44,
            "cues": [{"start": cue.start, "end": cue.end, "text": cue.text} for cue in self._cues()],
        }

    def current_branding_settings(self) -> dict:
        profile_id = str(self.channel_profile.currentData() or "")
        banner = self.assets.banner_path(profile_id)
        existing = dict(self.candidate.branding_settings or {}) if self.candidate else {}
        title_suggestions = dict(existing.get("title_suggestions") or {})
        final_title = self.title_text.text().strip()
        selected_text = ""
        selected_id = str(title_suggestions.get("selected_id") or title_suggestions.get("recommended_id") or "")
        for item in title_suggestions.get("suggestions") or []:
            if str(item.get("id") or "") == selected_id:
                selected_text = str(item.get("text") or "").strip()
                break
        if title_suggestions and final_title and selected_text and final_title != selected_text:
            title_suggestions["manual_title"] = final_title
        return {
            "preset": self.branding_preset.currentData(),
            "show_subtitles": self.show_subtitles.isChecked(),
            "show_title": self.show_title.isChecked(),
            "original_video_title": existing.get("original_video_title", ""),
            "original_video_title_source": existing.get("original_video_title_source", ""),
            "translated_video_title": existing.get("translated_video_title", ""),
            "short_hook_title": existing.get("short_hook_title", ""),
            "short_hook_suggestions": existing.get("short_hook_suggestions", []),
            "title_suggestions": title_suggestions,
            "final_title_text": final_title,
            "title_style": self.title_style.currentData(),
            "title_size": self.title_size.value(),
            "title_bold": self.title_bold.isChecked(),
            "use_subtitle_font_for_title": self.use_subtitle_font_for_title.isChecked(),
            "title_font_family": self.title_font.currentData() or DEFAULT_FONT_FAMILY,
            "title_alignment": self.title_alignment.currentData() or "center",
            "title_offset_x": self.title_offset_x.value(),
            "title_color": TITLE_STYLE_PRESETS.get(str(self.title_style.currentData()), TITLE_STYLE_PRESETS["clean"])["color"],
            "title_outline": self.title_outline.value(),
            "title_shadow": self.title_shadow.value(),
            "title_background": False,
            "title_y": self.title_y.value(),
            "title_max_lines": 2,
            "show_channel_card": self.show_channel_card.isChecked(),
            "channel_profile_id": profile_id,
            "channel_banner_path": str(banner or ""),
            "banner_scale": self.banner_scale.value(),
            "banner_anchor": "bottom_center",
            "banner_fit_mode": "contain",
            "banner_offset_x": self.banner_x.value(),
            "banner_offset_y": self.banner_y.value(),
            "banner_opacity": self.banner_opacity.value(),
            "safe_margin": 80,
        }

    def _reset_banner_position(self) -> None:
        self.banner_x.setValue(0)
        self.banner_y.setValue(0)
        self._mark_dirty()

    def _save_banner_profile_defaults(self) -> None:
        profile_id = str(self.channel_profile.currentData() or "")
        if profile_id:
            self.assets.save_banner_defaults(profile_id, self.current_branding_settings())
            self.banner_preview.setText(self.banner_preview.text() + "\nНастройки профиля сохранены ✓")

    def _style_selected(self) -> None:
        if self._loading:
            return
        preset = STYLE_PRESETS.get(str(self.style.currentData()), STYLE_PRESETS["clean"])
        self.size.setValue(preset["size"])
        self.outline.setValue(preset["outline"])
        self.shadow.setValue(preset["shadow"])
        self.maximum.setValue({"clean": 36, "large": 24, "gaming": 28}.get(str(self.style.currentData()), 36))
        self._mark_dirty()

    def _title_style_selected(self) -> None:
        if self._loading:
            return
        preset = TITLE_STYLE_PRESETS.get(str(self.title_style.currentData()), TITLE_STYLE_PRESETS["clean"])
        self.title_size.setValue(preset["size"])
        self.title_bold.setChecked(preset["bold"])
        self.title_outline.setValue(preset["outline"])
        self.title_shadow.setValue(preset["shadow"])
        self._mark_dirty()

    def _mark_dirty(self, *_args) -> None:
        if self._loading or not self.candidate:
            return
        self._clamp_offset_to_safe_area()
        self._dirty = True
        self._preview_generation_id += 1
        self._mark_exact_stale()
        self.dirty_label.setText("Изменено · автосохранение…")
        self.dirty_label.setStyleSheet("color: #e6b450;")
        self._update_preview()
        self._save_timer.start()

    def _clamp_offset_to_safe_area(self) -> None:
        sample = self._cues()[0].text if self._cues() else "Пример безопасных субтитров"
        branding = self.current_branding_settings()
        banner_path = Path(str(branding.get("channel_banner_path") or ""))
        banner_pixmap = QPixmap(str(banner_path)) if banner_path.is_file() else QPixmap()
        banner_size = None if banner_pixmap.isNull() else (banner_pixmap.width(), banner_pixmap.height())
        layout = OverlayLayoutCalculator().subtitle_layout(
            sample, self.current_settings(), branding, banner_size,
        )
        if layout.clamped_vertical_offset != self.offset.value():
            self.offset.blockSignals(True)
            self.offset_value.blockSignals(True)
            self.offset.setValue(layout.clamped_vertical_offset)
            self.offset_value.setValue(layout.clamped_vertical_offset)
            self.offset.blockSignals(False)
            self.offset_value.blockSignals(False)
            self.dirty_label.setText("Достигнут безопасный предел позиции")

        if layout.clamped_horizontal_offset != self.subtitle_offset_x.value():
            self.subtitle_offset_x.blockSignals(True)
            self.subtitle_offset_x.setValue(layout.clamped_horizontal_offset)
            self.subtitle_offset_x.blockSignals(False)

    def _apply_configuration(self) -> None:
        if not self.candidate:
            return
        self.candidate.subtitle_settings = self.current_settings()
        self.candidate.layout_settings = self.vertical.value()
        self.candidate.branding_settings = self.current_branding_settings()
        self._dirty = False
        self.configuration_changed.emit(self.candidate)

    def mark_saved(self) -> None:
        self._dirty = False
        self._set_saved_state()

    def _set_saved_state(self) -> None:
        self.dirty_label.setText("Сохранено автоматически")
        self.dirty_label.setStyleSheet("color: #7fba7a;")

    def _update_preview(self) -> None:
        sample = ""
        seconds = self._preview_position_ms / 1000
        for cue in self._cues():
            if cue.start <= seconds < cue.end:
                sample = cue.text
                break
        self.preview.set_preview(self.candidate, self.current_settings(), self.vertical.value(), sample, self.current_branding_settings())

    def _merge(self) -> None:
        rows = sorted({index.row() for index in self.table.selectedIndexes()})
        cues = self._cues()
        if len(rows) < 2 or rows[-1] - rows[0] + 1 != len(rows) or rows[-1] >= len(cues):
            return
        merged = SubtitleCue(cues[rows[0]].start, cues[rows[-1]].end, " ".join(cues[row].text.replace("\n", " ") for row in rows))
        self._show(cues[:rows[0]] + [merged] + cues[rows[-1] + 1:])
        self._mark_dirty()

    def _split(self) -> None:
        row = self.table.currentRow()
        cues = self._cues()
        if row < 0 or row >= len(cues):
            return
        cue = cues[row]
        words = cue.text.replace("\n", " ").split()
        if len(words) < 2:
            return
        cut, middle = len(words) // 2, (cue.start + cue.end) / 2
        replacement = [SubtitleCue(cue.start, middle, " ".join(words[:cut])), SubtitleCue(middle, cue.end, " ".join(words[cut:]))]
        self._show(cues[:row] + replacement + cues[row + 1:])
        self._mark_dirty()

    def _delete(self) -> None:
        rows = {index.row() for index in self.table.selectedIndexes()}
        self._show([cue for index, cue in enumerate(self._cues()) if index not in rows])
        self._mark_dirty()

    def _restore(self) -> None:
        self._show(deepcopy(self.original))
        self._mark_dirty()

    def _export(self) -> None:
        if not self.candidate or not self.paths:
            return
        self._apply_configuration()
        self.service.write(
            self._cues(), self.paths.subtitles / f"{self.candidate.id}.srt",
            self.paths.subtitles / f"{self.candidate.id}.ass", self.current_settings(),
            self.current_branding_settings(),
        )
        self.saved.emit(self.candidate)

    def _test_render(self) -> None:
        if self.candidate:
            self._apply_configuration()
            self.test_render_requested.emit(self.candidate)

    def _save_defaults(self) -> None:
        if self.candidate:
            self._apply_configuration()
            self.defaults_requested.emit(self.candidate)

