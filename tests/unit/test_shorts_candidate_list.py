import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from creator_assistant.domain.shorts.models import Candidate
from creator_assistant.ui.shorts.candidate_list import CandidateList


def app():
    return QApplication.instance() or QApplication([])


def test_candidate_list_hides_empty_warning_column_and_shows_details():
    application = app()
    widget = CandidateList()
    candidate = Candidate(
        "short_003",
        10,
        58,
        91,
        "Полная длинная расшифровка кандидата, которую нельзя нормально читать в узкой ячейке таблицы.",
        reasons=["Сильное начало", "Есть развязка"],
        heuristic_score=88,
        semantic_score=93,
        final_score=91,
        ai_model="qwen3:14b",
        ai_mode="balanced",
        candidate_rank=1,
    )
    widget.set_candidates([candidate])
    assert widget.table.isColumnHidden(10)
    assert widget.table.item(0, 0).text() == "1"
    assert widget.table.verticalHeader().isVisible() is False
    widget.table.selectRow(0)
    application.processEvents()
    assert "Полная длинная расшифровка" in widget.details.toPlainText()
    assert "short_003" in widget.details.toPlainText()
    widget.close()


def test_candidate_list_keeps_final_rank_after_sort_and_stable_id():
    application = app()
    widget = CandidateList()
    first = Candidate("short_001", 5, 30, 60, "first", heuristic_score=60, final_score=60, candidate_rank=2)
    second = Candidate("short_002", 10, 40, 90, "second", heuristic_score=90, final_score=90, warnings=["warn"], candidate_rank=1)
    widget.set_candidates([first, second])
    assert widget.table.item(0, 0).text() == "1"
    assert widget.table.item(0, 0).data(256).id == "short_002"
    assert not widget.table.isColumnHidden(10)
    widget.sort.setCurrentIndex(widget.sort.findData("time"))
    assert widget.table.item(0, 0).text() == "2"
    assert widget.table.item(0, 0).data(256).id == "short_001"
    assert widget.table.item(1, 0).text() == "1"
    assert widget.table.item(1, 0).data(256).id == "short_002"
    widget.close()
    application.processEvents()


def test_candidate_list_displays_dash_when_rank_is_missing():
    application = app()
    widget = CandidateList()
    widget.set_candidates([Candidate("legacy", 0, 10, 50, "legacy")])
    assert widget.table.item(0, 0).text() == "—"
    widget.close()
    application.processEvents()
