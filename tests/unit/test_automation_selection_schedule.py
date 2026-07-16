from datetime import datetime

from creator_assistant.domain.shorts.models import Candidate
from creator_assistant.services.automation.schedule import SchedulePlanner
from creator_assistant.services.automation.selection import AutomaticCandidateSelector


def candidate(identifier, start, duration, score):
    return Candidate(identifier, start, start + duration, score, identifier, final_score=score)


def test_auto_count_changes_with_quality_and_does_not_fill_with_weak_candidates():
    selector = AutomaticCandidateSelector()
    settings = {"minimum_score": 80, "maximum_per_source": 10, "minimum_temporal_distance": 30, "minimum_duration": 25, "maximum_duration": 75}
    weak, _ = selector.select([candidate("weak", 0, 40, 79)], settings)
    rich, summary = selector.select([
        candidate("a", 0, 40, 95), candidate("duplicate", 10, 40, 94),
        candidate("b", 100, 45, 91), candidate("c", 200, 50, 88),
    ], settings)
    assert weak == []
    assert [item.id for item in rich] == ["a", "b", "c"]
    assert "Найдено 4 окон, отобрано 3 Shorts" in summary


def test_schedule_planner_distributes_one_two_and_three_per_day_with_timezone():
    planner = SchedulePlanner()
    for per_day in (1, 2, 3):
        slots = ["09:00", "13:00", "19:00"][:per_day]
        plan = planner.build(
            [f"s{i}" for i in range(6)],
            {"start_date": "2026-07-20", "timezone": "Europe/Moscow", "publications_per_day": per_day, "preferred_time_slots": slots},
            ["youtube", "tiktok"],
        )
        assert len(plan.slots) == 6
        assert all(datetime.fromisoformat(item.scheduled_at).utcoffset().total_seconds() == 3 * 3600 for item in plan.slots)
        first_day = plan.slots[0].scheduled_at[:10]
        assert sum(item.scheduled_at.startswith(first_day) for item in plan.slots) == per_day


def test_schedule_planner_skips_inactive_days_and_occupied_slots():
    planner = SchedulePlanner()
    occupied = {"2026-07-20T13:00:00+03:00"}
    plan = planner.build(
        ["one", "two"],
        {"start_date": "2026-07-20", "timezone": "Europe/Moscow", "publications_per_day": 2,
         "preferred_time_slots": ["13:00", "19:00"], "active_weekdays": [0]},
        ["youtube"], occupied,
    )
    assert plan.slots[0].scheduled_at == "2026-07-20T19:00:00+03:00"
    assert plan.slots[1].scheduled_at == "2026-07-27T13:00:00+03:00"
