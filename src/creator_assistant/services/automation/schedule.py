from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone as fixed_timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from creator_assistant.domain.automation.models import PublishingPlan, PublishingSlot
from creator_assistant.services.shorts.manifest import utc_now


class SchedulePlanner:
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
        times = [time.fromisoformat(str(value)) for value in raw_slots][:per_day]
        while len(times) < per_day:
            times.append(time(hour=min(23, 9 + len(times) * 4)))
        active_days = {int(value) for value in settings.get("active_weekdays", range(7))}
        minimum_interval = float(settings.get("minimum_interval_hours", 1.0))
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
