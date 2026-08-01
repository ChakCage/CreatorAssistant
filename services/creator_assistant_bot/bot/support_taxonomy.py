from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class SupportCategory(str, Enum):
    ACTIVATION = "activation"
    APPLICATION = "application"
    RENDER_EXPORT = "render_export"
    OTHER = "other"


@dataclass(frozen=True)
class SupportCategorySpec:
    value: SupportCategory
    label: str

    @property
    def callback_data(self) -> str:
        return f"support_category:{self.value.value}"


SUPPORT_CATEGORIES: tuple[SupportCategorySpec, ...] = (
    SupportCategorySpec(SupportCategory.ACTIVATION, "Активация и лицензия"),
    SupportCategorySpec(SupportCategory.APPLICATION, "Работа приложения"),
    SupportCategorySpec(SupportCategory.RENDER_EXPORT, "Рендер и экспорт"),
    SupportCategorySpec(SupportCategory.OTHER, "Другое"),
)

_BY_VALUE = {item.value.value: item for item in SUPPORT_CATEGORIES}
# Existing historical rows used ``render``. It remains display-only and is
# never emitted by a new button or sent for a new ticket.
_LEGACY_LABELS = {"render": "Рендер и экспорт"}


def parse_support_category_callback(value: object) -> SupportCategory:
    prefix, separator, raw = str(value or "").partition(":")
    if prefix != "support_category" or separator != ":" or raw not in _BY_VALUE:
        raise ValueError("unknown support category callback")
    return _BY_VALUE[raw].value


def parse_support_category_value(value: object) -> SupportCategory:
    raw = str(value or "").strip().casefold()
    try:
        return SupportCategory(raw)
    except ValueError as exc:
        raise ValueError("unknown support category value") from exc


def support_category_label(value: object) -> str:
    raw = str(value or "").strip().casefold()
    if raw in _BY_VALUE:
        return _BY_VALUE[raw].label
    if raw in _LEGACY_LABELS:
        return _LEGACY_LABELS[raw]
    return "Неизвестная категория"
