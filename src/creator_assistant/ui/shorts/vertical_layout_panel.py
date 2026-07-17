from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QComboBox, QFormLayout, QGroupBox, QLabel, QSlider, QSpinBox


class VerticalLayoutPanel(QGroupBox):
    changed = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__("F. Вертикальный кадр", parent)
        form = QFormLayout(self)
        self.mode = QComboBox()
        self.mode.addItem("Center Crop", "center_crop")
        self.mode.addItem("Blur Background", "blur_background")
        self.mode.addItem("Color Background (black)", "solid_color")
        self.center = QSlider(Qt.Horizontal)
        self.center.setRange(0, 100)
        self.center.setValue(50)
        self.center_value = QLabel("50%")
        self.center.valueChanged.connect(lambda value: self.center_value.setText(f"{value}%"))
        self.foreground = QSlider(Qt.Horizontal)
        self.foreground.setRange(50, 550)
        self.foreground.setValue(100)
        self.foreground.setToolTip("Доступно только в режимах Blur Background и Solid Color Background.")
        self.foreground_value = QSpinBox()
        self.foreground_value.setRange(50, 550)
        self.foreground_value.setSuffix("%")
        self.foreground_value.setValue(100)
        self.foreground_value.setToolTip(self.foreground.toolTip())
        self.blur_radius = QSpinBox()
        self.blur_radius.setRange(2, 40)
        self.blur_radius.setValue(12)
        self.blur_radius.setToolTip("Радиус размытия уменьшенной фоновой копии")
        self.blur_power = QSpinBox()
        self.blur_power.setRange(1, 10)
        self.blur_power.setValue(6)
        self.blur_power.setToolTip("Количество проходов размытия уменьшенной фоновой копии")
        self.foreground.valueChanged.connect(self.foreground_value.setValue)
        self.foreground_value.valueChanged.connect(self.foreground.setValue)
        form.addRow("Режим", self.mode)
        form.addRow("Горизонтальный центр", self.center)
        form.addRow("Центр", self.center_value)
        form.addRow("Масштаб переднего слоя", self.foreground)
        form.addRow("Масштаб", self.foreground_value)
        form.addRow("Радиус размытия", self.blur_radius)
        form.addRow("Проходов размытия", self.blur_power)
        self.mode.currentIndexChanged.connect(self._sync)
        self.mode.currentIndexChanged.connect(self.changed)
        self.center.valueChanged.connect(self.changed)
        self.foreground.valueChanged.connect(self.changed)
        self.blur_radius.valueChanged.connect(self.changed)
        self.blur_power.valueChanged.connect(self.changed)
        self._sync()

    def _sync(self) -> None:
        crop = self.mode.currentData() == "center_crop"
        self.center.setEnabled(crop)
        self.foreground.setEnabled(not crop)
        self.foreground_value.setEnabled(not crop)
        blur = self.mode.currentData() == "blur_background"
        self.blur_radius.setEnabled(blur)
        self.blur_power.setEnabled(blur)
        self.foreground.setToolTip(
            "Доступно только в режимах Blur Background и Solid Color Background."
            if crop else "Доступно только в режимах Blur Background и Solid Color Background."
        )
        self.foreground_value.setToolTip(self.foreground.toolTip())

    def value(self) -> dict:
        return {
            "mode": self.mode.currentData(),
            "crop_center": self.center.value(),
            "foreground_scale": self.foreground.value(),
            "background_color": "black",
            "blur_radius": self.blur_radius.value(),
            "blur_power": self.blur_power.value(),
        }

    def set_value(self, value: dict) -> None:
        self.mode.setCurrentIndex(max(0, self.mode.findData(value.get("mode", "center_crop"))))
        self.center.setValue(int(value.get("crop_center", 50)))
        self.foreground.setValue(int(value.get("foreground_scale", 100)))
        self.blur_radius.setValue(int(value.get("blur_radius", 12)))
        self.blur_power.setValue(int(value.get("blur_power", 6)))
