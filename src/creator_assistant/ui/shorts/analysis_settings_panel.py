from PySide6.QtWidgets import QComboBox, QDoubleSpinBox, QFormLayout, QGroupBox, QSpinBox

from creator_assistant.services.shorts.candidate_generator import CandidateSettings


class AnalysisSettingsPanel(QGroupBox):
    def __init__(self, parent=None) -> None:
        super().__init__("Настройки кандидатов", parent)
        form = QFormLayout(self)
        self.minimum = QDoubleSpinBox()
        self.maximum = QDoubleSpinBox()
        self.desired = QDoubleSpinBox()
        for control, value in ((self.minimum, 25), (self.maximum, 60), (self.desired, 45)):
            control.setRange(5, 180)
            control.setSuffix(" сек")
            control.setValue(value)
        self.count = QSpinBox()
        self.count.setRange(1, 50)
        self.count.setValue(15)
        self.content_type = QComboBox()
        for label, value in (("Игровой ролик", "gaming"), ("Обучающий", "education"), ("Разговорный", "talking")):
            self.content_type.addItem(label, value)
        self.content_type.setToolTip(
            "Профиль влияет на эвристику, prompt локальной LLM, selection и cache key: "
            "игровой — динамика/конфликт/развязка; обучающий — проблема/решение; разговорный — мнение/история/вывод."
        )
        form.addRow("Минимум", self.minimum)
        form.addRow("Максимум", self.maximum)
        form.addRow("Желаемая", self.desired)
        form.addRow("Кандидатов", self.count)
        form.addRow("Тип контента", self.content_type)

    def value(self) -> CandidateSettings:
        minimum, maximum = self.minimum.value(), self.maximum.value()
        if maximum < minimum:
            maximum = minimum
        return CandidateSettings(
            minimum=minimum, maximum=maximum, desired=min(max(self.desired.value(), minimum), maximum),
            count=self.count.value(), content_type=str(self.content_type.currentData()),
        )
