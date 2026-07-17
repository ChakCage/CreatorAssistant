from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QLabel, QScrollArea, QVBoxLayout, QWidget,
)

from creator_assistant.services.shorts.project_template import ProjectShortsTemplate


LAYOUT_NAMES = {
    "center_crop": "Обрезка по центру",
    "blur_background": "Размытый фон",
    "solid_color": "Цветной фон",
}
SUBTITLE_NAMES = {"clean": "Чистый", "large": "Крупный", "gaming": "Игровой"}
TITLE_MODE_NAMES = {
    "TRANSLATED_SOURCE_TITLE": "Русский перевод исходного названия",
}


def template_details(template: ProjectShortsTemplate, source: str) -> str:
    layout = template.layout
    subtitle = template.subtitle
    branding = template.branding
    render = template.render
    profile = str(branding.get("channel_profile_id") or "Определяется по автору")
    banner = "Включён; изображение определяется по профилю автора" if branding.get("show_channel_card") else "Отключён"
    return "\n".join((
        f"Источник шаблона: {source}",
        "Название: Шаблон оформления проекта",
        f"Режим кадра: {LAYOUT_NAMES.get(str(layout.get('mode', 'center_crop')), str(layout.get('mode', 'center_crop')))}",
        f"Масштаб переднего слоя: {int(layout.get('foreground_scale', 100))}%",
        f"Горизонтальный центр: {int(layout.get('crop_center', 50))}%",
        f"Размытие: радиус {int(layout.get('blur_radius', 12))}, проходов {int(layout.get('blur_power', 6))}",
        f"Субтитры: {SUBTITLE_NAMES.get(str(subtitle.get('style', 'clean')), str(subtitle.get('style', 'clean')))}",
        f"Шрифт и размер: {subtitle.get('font_family', 'Segoe UI')} · {int(subtitle.get('size', 58))}",
        f"Положение: X {int(subtitle.get('horizontal_offset', 0))} px · Y {int(subtitle.get('vertical_offset', 0))} px",
        f"Строки: до {int(subtitle.get('lines', 2))} · ориентир {int(subtitle.get('maximum', 36))} символов",
        f"Расстояние до баннера: {int(subtitle.get('banner_gap', 15))} px",
        f"Заголовок: {TITLE_MODE_NAMES.get(template.title_mode, template.title_mode)}",
        f"Стиль заголовка: {branding.get('title_style', 'clean')} · {int(branding.get('title_size', 78))}",
        f"Профиль канала: {profile}",
        f"Баннер: {banner}",
        f"Геометрия баннера: {int(branding.get('banner_scale', 100))}% · X {int(branding.get('banner_offset_x', 0))} · Y {int(branding.get('banner_offset_y', 0))} · прозрачность {int(branding.get('banner_opacity', 100))}%",
        f"Рендер: {int(render.get('width', 1080))}×{int(render.get('height', 1920))} · FPS {render.get('fps_policy', 'source')} · {render.get('encoder', 'h264_nvenc')} · {render.get('audio_codec', 'aac')}",
    ))


class ProjectTemplateDialog(QDialog):
    def __init__(self, template: ProjectShortsTemplate, source: str, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Шаблон оформления проекта")
        self.setMinimumWidth(720)
        self.resize(780, 620)
        root = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        content_layout = QVBoxLayout(content)
        label = QLabel(template_details(template, source))
        label.setWordWrap(True)
        label.setTextInteractionFlags(label.textInteractionFlags())
        label.setStyleSheet("line-height: 1.35; padding: 12px;")
        content_layout.addWidget(label)
        content_layout.addStretch(1)
        scroll.setWidget(content)
        root.addWidget(scroll, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.close)
        root.addWidget(buttons)
