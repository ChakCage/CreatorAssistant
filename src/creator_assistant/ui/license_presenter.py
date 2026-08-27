from __future__ import annotations

from datetime import datetime, timedelta, timezone, tzinfo


MONTHS = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)

PLAN_NAMES = {
    "free_channel": "Бесплатный",
    "free": "Бесплатный",
    "paid": "Платный",
    "commercial": "Платный",
    "promo": "Промо",
    "beta": "Тестовый доступ",
}

STATE_NAMES = {
    "ACTIVE": "активна",
    "OFFLINE_GRACE": "временный офлайн-доступ",
    "GRACE": "временный офлайн-доступ",
    "EXPIRED": "истекла",
    "CANCELLED": "отменена",
    "NOT_ACTIVATED": "не активирована",
    "SERVER_UNAVAILABLE": "сервер временно недоступен",
    "UPDATE_REQUIRED": "требуется обновление приложения",
}

DEVICE_STATE_NAMES = {
    "ACTIVE": "активно",
    "DEACTIVATED": "отключено",
    "BLOCKED": "заблокировано",
}


def plan_name(value: object) -> str:
    text = str(value or "").strip()
    return PLAN_NAMES.get(text.casefold(), text or "—")


def state_name(value: object) -> str:
    text = str(value or "").strip().upper()
    return STATE_NAMES.get(text, "состояние неизвестно")


def device_state_name(value: object) -> str:
    text = str(value or "").strip().upper()
    return DEVICE_STATE_NAMES.get(text, "состояние неизвестно")


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
    return parsed


def format_desktop_datetime(
    value: object,
    *,
    now: datetime | None = None,
    local_timezone: tzinfo | None = None,
    relative: bool = False,
    always_show_year: bool = False,
) -> str:
    parsed = _parse_datetime(value)
    if parsed is None:
        return "—"
    zone = local_timezone or datetime.now().astimezone().tzinfo or timezone.utc
    local = parsed.astimezone(zone)
    reference = now or datetime.now(zone)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=zone)
    else:
        reference = reference.astimezone(zone)
    clock = local.strftime("%H:%M")
    if relative and local.date() == reference.date():
        return f"Сегодня, {clock}"
    if relative and local.date() == (reference - timedelta(days=1)).date():
        return f"Вчера, {clock}"
    if always_show_year or local.year != reference.year:
        return f"{local.day} {MONTHS[local.month - 1]} {local.year}, {clock}"
    return f"{local.day} {MONTHS[local.month - 1]}, {clock}"


def free_quota_text(value: dict) -> str:
    projects = value.get("projects") or {}
    shorts = value.get("shorts_sources") or {}
    devices = value.get("devices") or {}
    membership = value.get("membership") or {}
    membership_status = str(membership.get("status") or "").casefold()
    confirmed = membership_status in {"creator", "administrator", "member", "restricted"}
    return (
        "\n\nБесплатный тариф\n"
        f"Подготовка проектов: {projects.get('used', 0)} из {projects.get('limit', 0)}\n"
        f"Исходные видео для Shorts: {shorts.get('used', 0)} из {shorts.get('limit', 0)}\n"
        f"Устройства: {devices.get('used', 0)} из {devices.get('limit', 0)}\n"
        f"Подписка на канал: {'подтверждена' if confirmed else 'не подтверждена'}"
    )
