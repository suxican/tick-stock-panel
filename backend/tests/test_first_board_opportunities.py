from datetime import date, datetime

from app.market_time import CN_TZ
from app.services.first_board_opportunities import aggregate_opportunities

DAY = date(2026, 10, 16)


def ev(id, minute, symbol, event_type, pattern="platform", revision=4, **extra):
    at = datetime(2026, 10, 16, 10, minute, tzinfo=CN_TZ)
    return {"id": id, "ts": int(at.timestamp() * 1000), "date": "2026-10-16", "symbol": symbol,
            "name": f"名{symbol}", "pattern": pattern, "pattern_label": f"形态{pattern}", "event_type": event_type,
            "revision": revision, "price": 10.0, "blocked_reasons": [], **extra}


def at(minute):
    return datetime(2026, 10, 16, 10, minute, tzinfo=CN_TZ).isoformat()


def run(events):
    return aggregate_opportunities(events, day=DAY, revision=4)


def test_merges_patterns_of_one_stock_into_one_row():
    report = run([ev("a", 1, "A", "buy_candidate", "platform"), ev("b", 2, "A", "buy_candidate", "trend")])
    assert len(report["rows"]) == 1
    row = report["rows"][0]
    assert [item["pattern"] for item in row["patterns"]] == ["platform", "trend"]
    assert row["first_alert"]["pattern"] == "platform" and row["first_alert"]["at"] == at(1)
    assert row["event_count"] == 2


def test_same_millisecond_alert_picks_smallest_pattern_then_id():
    report = run([ev("z", 1, "A", "buy_candidate", "trend"), ev("y", 1, "A", "buy_candidate", "platform")])
    assert report["rows"][0]["first_alert"]["pattern"] == "platform"
    assert report["rows"][0]["first_alert"]["event_id"] == "y"


def test_full_lifecycle_alert_touch_break_reseal():
    report = run([ev("1", 1, "A", "buy_candidate"), ev("2", 2, "A", "sealed", price=11.0), ev("3", 3, "A", "broken"),
                  ev("4", 4, "A", "sealed"), ev("5", 5, "A", "broken")])
    row = report["rows"][0]
    assert row["first_touch"] == {"at": at(2), "price": 11.0}
    assert row["broken"] == at(3) and row["resealed"] == at(4)
    assert row["last_state"] == {"event_type": "broken", "at": at(5)}
    assert row["alerted"] is True and row["sealed_unalerted"] is False


def test_sealed_without_alert_is_flagged_and_keeps_first_record():
    report = run([ev("1", 1, "A", "approaching", blocked_reasons=["行业强度不足"]), ev("2", 3, "A", "sealed")])
    row = report["rows"][0]
    assert row["sealed_unalerted"] is True and row["first_alert"] is None
    assert row["first_record"] == {"at": at(1), "event_type": "approaching", "blocked_reasons": ["行业强度不足"]}
    assert report["summary"] == {"stock_count": 1, "alerted_count": 0, "touched_count": 1,
                                 "sealed_unalerted_count": 1, "skipped_count": 0}


def test_broken_without_prior_touch_is_not_inferred():
    row = run([ev("1", 1, "A", "broken")])["rows"][0]
    assert row["first_touch"] is None and row["broken"] is None and row["resealed"] is None


def test_result_is_independent_of_input_order():
    events = [ev("1", 1, "A", "buy_candidate"), ev("2", 2, "A", "sealed"), ev("3", 3, "B", "approaching"),
              ev("4", 4, "A", "broken"), ev("5", 4, "B", "sealed", "trend")]
    assert run(events) == run(events[::-1])


def test_ignores_other_revisions_and_exit_events():
    report = run([ev("1", 1, "A", "buy_candidate"), ev("2", 2, "A", "sealed", revision=5),
                  ev("3", 3, "A", "exit_candidate", "exit"), ev("4", 4, "X", "buy_candidate", revision=3)])
    assert [row["symbol"] for row in report["rows"]] == ["A"]
    assert report["rows"][0]["event_count"] == 1 and report["rows"][0]["first_touch"] is None


def test_skips_events_without_valid_ts_and_counts_them():
    missing = ev("1", 1, "A", "buy_candidate")
    del missing["ts"]
    report = run([missing, ev("2", 2, "A", "buy_candidate", ts="x"), ev("3", 3, "A", "buy_candidate", ts=True),
                  ev("4", 4, "A", "sealed")])
    assert report["summary"]["skipped_count"] == 3
    assert report["rows"][0]["event_count"] == 1 and report["rows"][0]["first_alert"] is None


def test_empty_input_returns_empty_report():
    assert run([]) == {"day": "2026-10-16", "revision": 4, "rows": [],
                       "summary": {"stock_count": 0, "alerted_count": 0, "touched_count": 0,
                                   "sealed_unalerted_count": 0, "skipped_count": 0}}
