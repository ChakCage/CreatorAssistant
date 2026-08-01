from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .support_taxonomy import support_category_label


STATUS_TEXT = {
    "ACTIVE": "✅ Подписка активна",
    "EXPIRED": "⛔ Подписка закончилась",
    "CANCELLED": "🚫 Подписка отменена",
    "NOT_ACTIVATED": "⚪ Creator Assistant ещё не активирован",
    "OFFLINE_GRACE": "🟡 Временный офлайн-доступ",
    "GRACE": "🟡 Временный офлайн-доступ",
    "NONE": "⚪ Creator Assistant ещё не активирован",
}

DEVICE_STATUS_TEXT = {
    "ACTIVE": "активно",
    "DEACTIVATED": "отключено",
    "BLOCKED": "заблокировано",
}

MONTHS = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)
MOSCOW_TIMEZONE = ZoneInfo("Europe/Moscow")

STAGING_PAYMENT_DISABLED_TEXT = (
    "Оплата в тестовой версии пока отключена.\n\n"
    "Для участия в закрытом тестировании используйте тестовый доступ "
    "или приглашение администратора."
)

DOWNLOAD_WARNING_TEXT = (
    "⚠️ Тестовая сборка пока не имеет цифровой подписи.\n"
    "Windows SmartScreen может показать предупреждение."
)

PUBLIC_HELP_TEXT = (
    "1. Скачайте Creator Assistant.\n2. Установите программу.\n3. Получите код в боте.\n"
    "4. Введите код в приложении.\n5. Настройте Ollama и AI-модель.\n"
    "6. Создайте проект.\n7. Найдите и отрендерите Shorts.\n\n"
    "Поддержка доступна только через кнопку «🛟 Поддержка» в этом боте."
)

def support_category_text(value: object) -> str:
    return support_category_label(value)


def support_status_text(value: dict, *, audience: str = "user") -> str:
    status = str(value.get("status") or "").upper()
    if audience == "admin":
        labels = {
            "NEW": "Новое", "WAITING_ADMIN": "Ждёт моего ответа",
            "WAITING_USER": "Ждёт пользователя", "ANSWERED": "Ждёт пользователя",
            "CLOSED": "Закрыто", "BLOCKED": "Заблокировано",
            "OPEN": "Ждёт моего ответа",
        }
    else:
        labels = {
            "NEW": "ожидает ответа поддержки", "WAITING_ADMIN": "ожидает ответа поддержки",
            "WAITING_USER": "ожидает вашего ответа", "ANSWERED": "ожидает вашего ответа",
            "CLOSED": "закрыто", "BLOCKED": "заблокировано",
            "OPEN": "ожидает ответа поддержки",
        }
    return labels.get(status, "статус неизвестен")


def safe_attachment_text(value: object) -> str:
    attachment = value if isinstance(value, dict) else {}
    if not attachment:
        return "нет"
    name = str(attachment.get("name") or "вложение").replace("\n", " ").strip()
    kind = str(attachment.get("type") or "файл").replace("\n", " ").strip()
    try:
        size = max(0, int(attachment.get("size") or 0))
    except (TypeError, ValueError):
        size = 0
    return f"{name} · {kind} · {size / 1024:.1f} КБ"


def localized_status(value: object) -> str:
    return STATUS_TEXT.get(str(value or "").strip().upper(), "⚪ Статус подписки неизвестен")


def _parse_datetime(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def format_telegram_datetime(
    value: object, *, now: datetime | None = None, full: bool = False,
) -> str:
    parsed = _parse_datetime(value)
    if parsed is None:
        return "дата не указана"
    local = parsed.astimezone(MOSCOW_TIMEZONE)
    reference = now or datetime.now(MOSCOW_TIMEZONE)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=MOSCOW_TIMEZONE)
    else:
        reference = reference.astimezone(MOSCOW_TIMEZONE)
    clock = local.strftime("%H:%M")
    if not full and local.date() == reference.date():
        return f"Сегодня, {clock}"
    if not full and local.date() == (reference - timedelta(days=1)).date():
        return f"Вчера, {clock}"
    if not full and local.year == reference.year:
        return f"{local.day} {MONTHS[local.month - 1]}, {clock}"
    return f"{local.day} {MONTHS[local.month - 1]} {local.year}, {clock}"


def russian_date(value: object) -> str:
    parsed = _parse_datetime(value)
    if parsed is None:
        return ""
    return f"{parsed.day} {MONTHS[parsed.month - 1]} {parsed.year} года"


def russian_days(days: int) -> str:
    value = abs(int(days))
    tail = value % 100
    if 11 <= tail <= 14:
        word = "дней"
    elif value % 10 == 1:
        word = "день"
    elif value % 10 in {2, 3, 4}:
        word = "дня"
    else:
        word = "дней"
    return f"{days} {word}"


def subscription_text(value: dict, *, now: datetime | None = None) -> str:
    lines = [localized_status(value.get("status"))]
    expires = _parse_datetime(value.get("expires_at"))
    if not value.get("expires_at"):
        lines.append("Срок: бессрочно")
    elif expires is None:
        lines.append("Дата окончания: не указана")
    else:
        lines.append(f"Действует до: {russian_date(value.get('expires_at'))}")
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        seconds = (expires - current.astimezone(timezone.utc)).total_seconds()
        if seconds <= 0:
            lines.append("Срок истёк")
        else:
            lines.append(f"Осталось: {russian_days(max(1, math.ceil(seconds / 86400)))}")
    return "\n".join(lines)


def devices_text(value: dict) -> str:
    devices = list(value.get("devices") or [])
    active = [item for item in devices if str(item.get("status", "")).upper() == "ACTIVE"]
    limit = value.get("device_limit")
    try:
        limit_value = int(limit) if limit is not None else None
    except (TypeError, ValueError):
        limit_value = None
    counter = f"Использовано устройств: {len(active)}"
    if limit_value is not None and limit_value >= 0:
        counter += f" из {limit_value}"
    lines = [counter]
    for item in devices:
        name = str(item.get("name") or "Без названия").strip() or "Без названия"
        state = DEVICE_STATUS_TEXT.get(str(item.get("status") or "").upper(), "состояние неизвестно")
        lines.append(f"• {name} — {state}")
    return "\n".join(lines)


def main_menu(*, free_enabled: bool = False, is_admin: bool = False) -> InlineKeyboardMarkup:
    labels = [
        ("🎟 Тестовый доступ", "beta_access"), ("📅 Моя подписка", "subscription"),
        ("🔑 Код активации", "activation"), ("💻 Мои устройства", "devices"),
        ("⬇️ Скачать Creator Assistant", "download"), ("📖 Инструкция", "help"),
        ("🐞 Сообщить об ошибке", "feedback"), ("🛟 Поддержка", "support"),
    ]
    if free_enabled:
        labels.insert(0, ("🎁 Попробовать бесплатно", "free_offer"))
    if is_admin:
        labels.append(("🛠 Админ-панель", "admin_panel"))
    buttons = [InlineKeyboardButton(text=text, callback_data=data) for text, data in labels]
    return InlineKeyboardMarkup(inline_keyboard=[buttons[index:index + 2] for index in range(0, len(buttons), 2)])


def free_status_text(value: dict) -> str:
    projects = value.get("projects") or {"used": 0, "limit": 2}
    shorts = value.get("shorts_sources") or {"used": 0, "limit": 2}
    devices = value.get("devices") or {"used": 0, "limit": 1}
    membership = value.get("membership") or {}
    state = str(value.get("state") or "ELIGIBLE")
    state_text = {
        "ELIGIBLE": "доступ ещё не выдан", "ACTIVE": "активен",
        "PAUSED_UNSUBSCRIBED": "приостановлен: подпишитесь снова",
        "EXHAUSTED": "лимиты исчерпаны", "BLOCKED": "заблокирован",
        "CONVERTED_TO_PAID": "используется платная подписка",
    }.get(state, "состояние неизвестно")
    subscribed = "подтверждена" if membership.get("status") in {"creator", "administrator", "member", "restricted"} else "не подтверждена"
    return (
        "🎁 Бесплатный тариф\n\n"
        f"Состояние: {state_text}\n"
        f"Подготовка проектов: {projects.get('used', 0)} из {projects.get('limit', 0)}\n"
        f"Исходные видео для Shorts: {shorts.get('used', 0)} из {shorts.get('limit', 0)}\n"
        f"Устройства: {devices.get('used', 0)} из {devices.get('limit', 0)}\n"
        f"Подписка на канал: {subscribed}"
    )


def checkout_keyboard(url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Перейти к тестовой оплате", url=url)]])


def plans_text(plans: list[dict]) -> str:
    if not plans: return "Сейчас нет доступных тарифов."
    return "\n\n".join(
        f"{p['name']}\n{p['amount_minor'] / 100:.2f} {p['currency']} / {p['duration_days']} дней\n"
        f"Устройств: {p['device_limit']}\nФункции: {', '.join(p.get('features', []))}"
        for p in plans
    )
