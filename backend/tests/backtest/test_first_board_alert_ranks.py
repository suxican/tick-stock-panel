from copy import deepcopy
from datetime import date, datetime

from app.backtest import first_board_execution as execution
from app.market_time import CN_TZ

DAY = date(2026, 10, 19)
PREVIOUS = date(2026, 10, 16)


def alert(symbol, clock="10:00:00", *, score=None, change=None, hot=None, tech=None,
          revision=7, kind="buy_candidate", pattern="trend"):
    stamp = datetime.fromisoformat(f"{DAY}T{clock}").replace(tzinfo=CN_TZ)
    evidence = {}
    if score is not None:
        evidence["priority_score"] = {
            "version": "priority-v1", "status": "partial", "score": score, "known_max": 90,
            "parts": {"popularity": {"points": hot, "known_max": 10 if hot is not None else 0}},
        }
    if tech:
        evidence["technical_shadow"] = {
            "version": "ma-macd-shadow-v1", "pattern": pattern, "reference_date": str(PREVIOUS),
            "status": "ready", "ma_support": tech[0], "macd_support": tech[1],
        }
    return {"id": f"{symbol}-{clock}-{revision}-{pattern}-{kind}", "event_type": kind,
            "revision": revision, "date": str(DAY), "symbol": symbol, "name": symbol,
            "ts": int(stamp.timestamp() * 1000), "pattern": pattern, "change_pct": change, "evidence": evidence}


def ranks(events, ref=PREVIOUS):
    return execution.rank_first_alerts(events, day=DAY, revision=7, reference_session=ref)


def test_first_alert_ranks_show_actual_event_and_scheme_specific_coverage():
    first = alert("OLD", "09:50:00", change=.05)
    later = alert("OLD", "10:00:00", score=89, change=.10, tech=(True, True))
    full = alert("A", score=70, hot=8, change=.089, tech=(False, False))
    full["evidence"]["priority_score"]["radar"] = {"popularity": {"status": "ready", "rank": 3, "rank_gain": 8}}
    both = alert("B", score=69, change=.095, tech=(True, True))
    other_pattern = alert("B", score=60, change=.095, tech=(False, False), pattern="platform")
    unknown = alert("U", "10:00:00.001", score=80)
    events = [later, unknown, both, other_pattern, full, first, alert("SKIP", revision=8), alert("SKIP2", kind="sealed")]
    saved = deepcopy(events)
    report = ranks(events)
    assert events == saved and report["source"] == "first_buy_candidate"
    assert report["total"] == 4 and report["coverage"] == {
        "batches": 3, "priority_known": 3, "change_known": 3, "technical_known": 2, "popularity_known": 1,
    }
    assert [r["symbol"] for r in report["rows"]] == ["OLD", "A", "B", "U"]
    first, a, b, u = report["rows"]
    assert first["ranks"] == {"change": {"day": 1, "batch": 1, "batch_total": 1}}
    assert first["priority_score"] is None and first["ma_support"] is None
    assert (a["base_score"], a["priority_score"], a["ma_support"]) == (62, 70, False)
    assert a["popularity"] == {"points": 8, "known_max": 10, "rank": 3, "rank_gain": 8, "status": "ready"}
    assert (a["ranks"]["base"]["day"], a["ranks"]["priority"]["day"]) == (2, 1)
    assert b["ranks"]["change"] == {"day": 2, "batch": 1, "batch_total": 2}
    assert b["patterns"] == ["platform", "trend"] and b["pattern"] == "trend"
    assert b["ranks"]["technical_baseline"]["batch"] == 2 and b["ranks"]["combined"]["day"] == 1
    assert u["ranks"] == {k: {"day": 3, "batch": 1, "batch_total": 1} for k in ("base", "priority")}
    assert not ranks(events, ref=None)["coverage"]["technical_known"]


def test_append_later_scores_or_other_versions_cannot_displace_first_seats_and_ties_use_code():
    early = [alert(s, score=71, change=.09, tech=(True, True)) for s in "DCBA"]
    before = ranks(early)
    later = alert("L", "10:00:00.001", score=90, change=.19, tech=(True, True))
    assert ranks([later, *reversed(early), alert("Z", "09:40:00", revision=8)])["rows"][:4] == before["rows"]
    assert all(row["ranks"]["combined"]["day"] == i for i, row in enumerate(before["rows"], 1))


def test_reference_day_and_legacy_real_events_with_no_fields_are_not_synthesized():
    old = alert("O")
    del old["change_pct"], old["pattern"], old["name"]
    report = ranks([old])
    assert report["rows"][0]["ranks"] == {} and report["rows"][0]["patterns"] == []
    assert report["rows"][0]["name"] is None
    same_day = alert("S", score=70, tech=(True, True))
    same_day["evidence"]["technical_shadow"]["reference_date"] = str(DAY)
    row = ranks([same_day])["rows"][0]
    assert set(row["ranks"]) == {"base", "priority"}
    assert row["ma_support"] is row["macd_support"] is None


def test_multi_shape_uses_best_base_and_best_full_independently_and_preserves_zero_popularity():
    # One stock can win the original and no-popularity comparison on different shapes.
    paid = alert("A", score=75, hot=8, pattern="trend")
    other = alert("A", score=70, hot=0, pattern="platform", change=.085)
    unknown = alert("B", score=72, hot=0, change=.09)
    unknown["evidence"]["priority_score"]["radar"] = {"popularity": {"status": "unranked"}}
    a, b = ranks([other, unknown, paid])["rows"]
    assert (a["base_score"], a["priority_score"], a["pattern"]) == (70, 75, "trend")
    assert a["popularity"]["points"] == 8 and a["popularity"]["rank"] is None
    assert a["change_pct"] == .085 and a["ranks"]["base"]["day"] == 2
    assert b["popularity"]["points"] == 0 and b["popularity"]["status"] == "unranked"
    assert b["ranks"]["priority"]["day"] == 2
    paid["change_pct"] = .09
    a, _ = ranks([paid, unknown, other])["rows"]
    assert a["change_pct"] is None and "change" not in a["ranks"]
