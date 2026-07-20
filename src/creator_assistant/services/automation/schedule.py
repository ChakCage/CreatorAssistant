from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone as fixed_timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from creator_assistant.domain.automation.models import PublishingPlan, PublishingSlot
from creator_assistant.services.shorts.manifest import utc_now


class ScheduleValidationError(ValueError):
    """A user-correctable schedule configuration error."""


class SchedulePlanner:
    @staticmethod
    def normalize_time(value: str) -> str:
        parts = str(value).strip().split(":")
        if len(parts) != 2 or not all(part.isdigit() for part in parts):
            raise ScheduleValidationError(f"Некорректное время «{value}». Используйте формат HH:MM.")
        hour, minute = map(int, parts)
        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            raise ScheduleValidationError(f"Некорректное время «{value}». Часы 0–23, минуты 0–59.")
        return f"{hour:02d}:{minute:02d}"

    def validate_settings(self, settings: dict) -> list[str]:
        per_day = max(1, int(settings.get("publications_per_day", 2)))
        raw = settings.get("preferred_time_slots") or []
        normalized = [self.normalize_time(str(value)) for value in raw]
        if len(set(normalized)) != len(normalized):
            raise ScheduleValidationError("В расписании есть повторяющиеся слоты.")
        if len(normalized) != per_day:
            raise ScheduleValidationError(
                f"Указано публикаций в день: {per_day}, а временных слотов: {len(normalized)}. "
                f"Добавьте ещё {per_day - len(normalized)} слот(а)." if len(normalized) < per_day else
                f"Указано публикаций в день: {per_day}, а временных слотов: {len(normalized)}. Удалите лишние слоты."
            )
        return sorted(normalized)

    def build(self, short_ids: list[str], settings: dict, platforms: list[str], occupied: set[str] | None = None) -> PublishingPlan:
        timezone_name = str(settings.get("timezone", "Europe/Moscow"))
        try:
            timezone = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError:
            # Windows does not ship IANA tzdata. Moscow has had a fixed UTC+3
            # offset since 2014, so preserve correct scheduling without adding
            # an online/runtime dependency.
            if timezone_name != "Europe/Moscow":
                raise
            timezone = fixed_timezone(timedelta(hours=3), timezone_name)
        start_value = settings.get("start_date") or date.today().isoformat()
        day = date.fromisoformat(str(start_value))
        per_day = max(1, int(settings.get("publications_per_day", 2)))
        raw_slots = settings.get("preferred_time_slots") or ["13:00", "19:00"]
        times = [time.fromisoformat(value) for value in self.validate_settings({**settings, "preferred_time_slots": raw_slots})]
        active_days = {int(value) for value in settings.get("active_weekdays", range(7))}
        minimum_interval = float(settings.get("minimum_interval_hours", 0.0))
        occupied = occupied or set()
        slots: list[PublishingSlot] = []
        last: datetime | None = None
        index = 0
        while index < len(short_ids):
            if day.weekday() not in active_days:
                day += timedelta(days=1)
                continue
            for slot_time in times:
                moment = datetime.combine(day, slot_time, timezone)
                key = moment.isoformat()
                if key in occupied or (last and (moment - last).total_seconds() < minimum_interval * 3600):
                    continue
                slots.append(PublishingSlot(short_ids[index], key, list(platforms)))
                last = moment
                index += 1
                if index >= len(short_ids):
                    break
            day += timedelta(days=1)
        return PublishingPlan(timezone_name, utc_now(), slots)
