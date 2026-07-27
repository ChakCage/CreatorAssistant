from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def main_menu() -> InlineKeyboardMarkup:
    labels = [
        ("🎟 Получить тестовый доступ", "beta_access"), ("📅 Моя подписка", "subscription"),
        ("🔑 Код активации", "activation"), ("💻 Мои устройства", "devices"),
        ("⬇️ Скачать Creator Assistant", "download"), ("📖 Инструкция", "help"),
        ("🐞 Сообщить об ошибке", "feedback"), ("🛟 Поддержка", "support"),
    ]
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=text, callback_data=data)] for text, data in labels])


def checkout_keyboard(url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Перейти к тестовой оплате", url=url)]])


def plans_text(plans: list[dict]) -> str:
    if not plans: return "Сейчас нет доступных тарифов."
    return "\n\n".join(
        f"{p['name']}\n{p['amount_minor'] / 100:.2f} {p['currency']} / {p['duration_days']} дней\n"
        f"Устройств: {p['device_limit']}\nФункции: {', '.join(p.get('features', []))}"
        for p in plans
    )
