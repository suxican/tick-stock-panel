from datetime import datetime

import pytest

from app.services.market_game_reports import MarketGameReportStore


def saved(store, day, cutoff):
    return store.save({"as_of": day, "cutoff": cutoff}, {
        "as_of": day, "cutoff": cutoff, "stocks": [], "input_version": day,
    })


def test_ecology_archives_are_original_prior_observations_only(tmp_path):
    store = MarketGameReportStore(tmp_path)
    saved(store, "2026-09-28", "2026-09-28T16:00:00+08:00")
    saved(store, "2026-09-29", "2026-10-02T16:00:00+08:00")
    saved(store, "2026-09-30", "2026-09-30T16:00:00+08:00")
    rows = store.ecology_snapshots(as_of="2026-09-30", before=datetime.fromisoformat("2026-09-30T16:00:00+08:00"))
    assert [row["as_of"] for row in rows] == ["2026-09-28"]
    with pytest.raises(ValueError):
        store.ecology_snapshots(as_of="2026-09-30", before=datetime(2026, 9, 30))


def test_model_journal_is_immutable_separate_and_survives_restart(tmp_path):
    store = MarketGameReportStore(tmp_path)
    report = saved(store, "2026-09-30", "2026-09-30T16:00:00+08:00")
    path = store.root / f"{report['id']}.json"
    before = path.read_bytes()
    for day in ("2026-10-08", "2026-10-09"):
        store.save_model_evaluation(report["id"], {
            "report_id": report["id"], "evaluated_at": f"{day}T16:00:00+08:00",
            "execution": {"report_id": report["id"], "evaluated_at": f"{day}T16:00:00+08:00", "rows": []},
        })
    restored = MarketGameReportStore(tmp_path)
    assert path.read_bytes() == before
    assert len(restored.model_evaluations(report["id"])) == 2
    assert len(restored.list_reports()) == 1
    cutoff = datetime.fromisoformat("2026-10-08T17:00:00+08:00")
    history = restored.execution_history(before=cutoff)
    assert len(history) == 1
    assert history[0]["evaluated_at"] == "2026-10-08T16:00:00+08:00"
    assert restored.execution_history(before=cutoff, exclude_report_id=report["id"]) == []
    later = restored.execution_history(before=datetime.fromisoformat("2026-10-10T16:00:00+08:00"))
    assert len(later) == 2  # retain first-known label times across refreshes


def test_model_journal_rejects_mismatched_report_or_naive_time(tmp_path):
    store = MarketGameReportStore(tmp_path)
    report = saved(store, "2026-09-30", "2026-09-30T16:00:00+08:00")
    with pytest.raises(ValueError):
        store.save_model_evaluation(report["id"], {"report_id": "other", "evaluated_at": "2026-10-08T16:00:00+08:00"})
    with pytest.raises(ValueError):
        store.save_model_evaluation(report["id"], {"report_id": report["id"], "evaluated_at": "2026-10-08T16:00:00"})
    assert store.model_evaluations(report["id"]) == []


def test_bad_optional_ecology_archive_does_not_block_a_new_plan(tmp_path):
    store = MarketGameReportStore(tmp_path)
    old = saved(store, "2026-09-28", "2026-09-28T16:00:00+08:00")
    (store.root / f"{old['id']}.json").write_text("bad", encoding="utf-8")
    warnings = []
    assert store.ecology_snapshots(as_of="2026-09-30", before=datetime.fromisoformat("2026-09-30T16:00:00+08:00"), warnings=warnings) == []
    assert len(warnings) == 1


def test_later_file_write_does_not_replace_newer_evaluation(tmp_path):
    store = MarketGameReportStore(tmp_path)
    report = saved(store, "2026-09-30", "2026-09-30T16:00:00+08:00")
    for day in ("2026-10-09", "2026-10-08"):
        store.save_model_evaluation(report["id"], {"report_id": report["id"], "evaluated_at": f"{day}T16:00:00+08:00"})
    assert store.model_evaluations(report["id"], limit=1)[0]["evaluated_at"] == "2026-10-09T16:00:00+08:00"


def test_execution_history_includes_constraints_older_than_display_window(tmp_path):
    store = MarketGameReportStore(tmp_path)
    report = saved(store, "2026-09-30", "2026-09-30T16:00:00+08:00")
    for index in range(65):
        store.save_capital_observation(report["id"], {"report_id": report["id"], "index": index})
    assert len(store.capital_observations(report["id"])) == 60
    archived = store.execution_capital_observations(report["id"])
    assert len(archived) == 65 and archived[0]["index"] == 0
