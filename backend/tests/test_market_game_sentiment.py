"""zzshare sentiment archive, frozen context, loss-effect gate and cross-check."""
import time as clock
from datetime import date, datetime, timedelta

import pytest

from app.market_time import CN_TZ
from app.services import market_game_sentiment as ms

STAMP = "2026-01-01T17:00:00+08:00"


def _day(index):
    return (date(2026, 1, 1) + timedelta(days=index)).isoformat()


def _entry(level, *, limit_up=50, limit_down=5, stamp=STAMP):
    return {
        "sentiment": {"up_count": 60, "down_count": 40, "limit_up": limit_up, "limit_down": limit_down,
                      "broken_limit": 10, "gt5_count": 20, "lt5_count": level, "max_boards": 5,
                      "lb2_count": 3, "lb3_count": 1, "mian_count": level, "damian_count": 0,
                      "tiandi_count": None, "ditian_count": 0, "bigleg_count": 1, "fetched_at": stamp},
        "effect": {"earn_effect": 50, "lose_effect": level, "max_boards": 5, "limit_up": limit_up,
                   "limit_down": limit_down, "fetched_at": stamp},
        "index": {"open": 100, "high": 101, "low": 99, "close": 100 + level, "previous_close": 100,
                  "fetched_at": stamp},
    }


def _rows(current_level, *, prior=100, **current):
    rows = {_day(i): _entry(i % 10) for i in range(prior)}
    rows[_day(prior)] = _entry(current_level, **current)
    return rows, _day(prior)


def _context(rows, as_of, *, cutoff="2027-01-01T00:00:00+08:00", latest=True, local=None):
    return ms.build_sentiment_context(rows, as_of=as_of, cutoff=cutoff, latest_mode=latest,
                                      local_metrics=local or {"limit_up": 50, "limit_down": 5})


@pytest.mark.parametrize(("level", "expected", "score"), [(20, "veto", 100), (7, "caution", 75), (5, "none", 55)])
def test_gate_levels_use_only_earlier_sessions(level, expected, score):
    rows, as_of = _rows(level)
    context = _context(rows, as_of)
    assert context["status"] == "ready" and context["point_in_time"]
    assert context["loss_gate"]["level"] == expected
    assert context["loss_gate"]["score"] == pytest.approx(score)
    assert context["loss_gate"]["components"] == ["loss_count", "lose_effect", "drop5_share"]
    # Later sessions never enter an earlier day's reference distribution.
    rows[_day(101)] = _entry(0)
    assert _context(rows, as_of)["loss_gate"] == context["loss_gate"]
    assert len(context["history"]) == 60 and context["history"][-1]["loss_gate"] == context["loss_gate"]["score"]


def test_fewer_than_sixty_prior_sessions_do_not_score():
    rows, as_of = _rows(20, prior=59)
    context = _context(rows, as_of)
    assert context["loss_gate"]["level"] == "unavailable" and context["loss_gate"]["score"] is None
    assert context["status"] == "limited"
    assert all(metric["percentile"] is None for metric in context["metrics"])


def test_missing_session_or_late_fetch_is_not_known_at_cutoff():
    rows, as_of = _rows(20)
    missing = _context(rows, _day(150))
    assert missing["status"] == "unavailable" and missing["loss_gate"]["level"] == "unavailable"
    late = _context(rows, as_of, cutoff="2025-12-31T15:00:00+08:00")
    assert late["status"] == "unavailable" and "晚于" in late["reason"]
    research = _context(rows, as_of, cutoff="2025-12-31T15:00:00+08:00", latest=False)
    assert research["status"] == "limited" and research["point_in_time"] is False
    assert research["loss_gate"]["level"] == "veto"
    assert ms.sentiment_evidence(research)[0]["available_at"] is None


@pytest.mark.parametrize(("local", "status"), [
    ({"limit_up": 55, "limit_down": 7}, "matched"),
    ({"limit_up": 30, "limit_down": 5}, "mismatched"),
    ({"limit_up": None, "limit_down": 5}, "unavailable"),
])
def test_cross_check_tolerance(local, status):
    rows, as_of = _rows(5)
    assert _context(rows, as_of, local=local)["cross_check"]["status"] == status


def test_cross_check_upgrades_only_a_sole_completion_gap():
    rows, as_of = _rows(5)
    matched = _context(rows, as_of)
    snapshot = {"quality": "limited", "quality_reasons": ["completion_unverified"],
                "limitations": ["部分日线没有收盘时间证据; 保留研究画像并降级。", "其他限制"]}
    upgraded = ms.apply_cross_check(snapshot, matched)
    assert upgraded["quality"] == "ready" and upgraded["quality_reasons"] == []
    assert upgraded["quality_corroboration"] == "completion_corroborated"
    assert upgraded["limitations"][0] == "其他限制" and "双源" not in snapshot["limitations"][-1]
    assert snapshot["quality"] == "limited"  # input is not mutated
    for other in (
        {**snapshot, "quality_reasons": ["completion_unverified", "coverage_insufficient"]},
        {**snapshot, "quality": "unavailable"},
    ):
        assert ms.apply_cross_check(other, matched) is other
    mismatched = _context(rows, as_of, local={"limit_up": 10, "limit_down": 5})
    assert ms.apply_cross_check(snapshot, mismatched) is snapshot
    historical = _context(rows, as_of, cutoff="2025-12-31T15:00:00+08:00", latest=False)
    assert ms.apply_cross_check(snapshot, historical) is snapshot


def test_archive_appends_records_revisions_and_skips_intraday_rows(tmp_path):
    archive = ms.SentimentArchive(tmp_path)
    evening = datetime(2026, 11, 6, 17, tzinfo=CN_TZ)
    record = {"trade_date": "2026-11-06", "limit_up": 32, "mian_count": 4}
    assert archive.merge([("sentiment", [record])], fetched_at=evening) == {"added": 1, "revised": 0}
    later = datetime(2026, 11, 7, 9, tzinfo=CN_TZ)
    result = archive.merge([("sentiment", [{**record, "limit_up": 33}])], fetched_at=later)
    assert result == {"added": 0, "revised": 1}
    stored = archive.load()
    assert stored["rows"]["2026-11-06"]["sentiment"]["limit_up"] == 32  # first-seen value kept
    assert stored["revisions"] == [{"date": "2026-11-06", "part": "sentiment", "fields": ["limit_up"],
                                    "detected_at": later.isoformat()}]
    intraday = datetime(2026, 11, 9, 11, tzinfo=CN_TZ)
    assert archive.merge([("effect", [{"trade_date": "2026-11-09", "lose_effect": 3}])],
                         fetched_at=intraday) == {"added": 0, "revised": 0}
    assert "2026-11-09" not in archive.rows()


def test_sync_backfills_then_fetches_only_the_overlap(tmp_path):
    archive = ms.SentimentArchive(tmp_path)
    calls = []

    def fetch(kind, start, end):
        calls.append((kind, start, end))
        return [{"trade_date": "2026-11-05", "close": 1, "previous_close": 1, "lose_effect": 1}]

    now = datetime(2026, 11, 6, 17, tzinfo=CN_TZ)
    assert ms.sync(archive, date(2026, 11, 6), fetch=fetch, now=now)["ok"]
    assert [call[1] for call in calls] == [ms.BACKFILL_START] * 3
    calls.clear()
    assert ms.sync(archive, date(2026, 11, 6), fetch=fetch, now=now)["added"] == 0
    assert {call[1] for call in calls} == {date(2026, 11, 5) - timedelta(days=ms.OVERLAP_DAYS)}
    calls.clear()
    assert ms.sync(archive, date(2026, 11, 5), fetch=fetch, now=now)["reason"].startswith("本地情绪归档已覆盖")
    assert calls == []


def test_sync_failures_and_timeouts_degrade_without_writing(tmp_path):
    archive = ms.SentimentArchive(tmp_path)

    def broken(*args):
        raise RuntimeError("upstream down")

    result = ms.sync_with_budget(archive, date(2026, 11, 6), fetch=broken)
    assert result["ok"] is False and "暂不可用" in result["reason"]
    assert not archive.path.exists()

    def slow(*args):
        clock.sleep(0.5)
        return []

    result = ms.sync_with_budget(archive, date(2026, 11, 6), fetch=slow, budget=0.05)
    assert result["ok"] is False and "超时" in result["reason"]


def test_corrupt_archive_is_reported_not_repaired(tmp_path):
    archive = ms.SentimentArchive(tmp_path)
    archive.path.parent.mkdir(parents=True)
    archive.path.write_text("{}", encoding="utf-8")
    assert ms.sync(archive, date(2026, 11, 6), fetch=lambda *a: [])["ok"] is False
    assert archive.path.read_text(encoding="utf-8") == "{}"


def test_gate_series_matches_frozen_context():
    rows, as_of = _rows(20)
    assert ms.gate_series(rows)[as_of] == (100.0, "veto")
    assert ms.gate_series(rows)[_day(10)] == (None, "unavailable")
