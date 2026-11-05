from copy import deepcopy
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import polars as pl
import pytest

from app.market_time import CN_TZ
from app.services import data_integrity
from app.services import first_board as fb
from app.services.first_board_data_status import radar_clocks

NOW = datetime(2026, 11, 5, 10, 2, tzinfo=CN_TZ)
DAY = NOW.date()
S, T = "600000.SH", "000001.SZ"


def row(symbol, score="partial", buy=True):
    prior = "2026-11-04"
    return {
        "symbol": symbol, "pattern": "platform", "price": 10., "can_buy": buy, "quote_time": NOW.isoformat(),
        "blocked_reasons": [], "sector_shadow_eligible": score == "shadow",
        "attention_context": {"status": "partial", "reasons": [], "intraday_rising": False,
                              "intraday": {"as_of": (NOW.replace(second=0) - timedelta(minutes=1)).isoformat()},
                              "groups": [{"kind": "industry", "peer_rank": 4, "peer_total": 6, "peer_strong": False}]},
        "priority_score": {"status": score, "score": 64., "can_buy": buy},
        "evidence": {"reference_date": prior, "technical_shadow": {
            "version": "ma-macd-shadow-v1", "status": "partial", "reference_date": prior, "pattern": "platform",
            "ma_support": False, "macd_support": None}},
    }


def radar():
    clock = {"source_at": (NOW - timedelta(minutes=9, seconds=55)).isoformat(), "fetched_at": NOW.isoformat()}
    official = {"status": "ready", "source": "official_core", "market_at": NOW.isoformat(), **clock}
    plate = {"status": "ready", "source": "vendor_plate", **clock}
    return {
        S: {"popularity": {"status": "ready", "rank": 4, **clock}, "theme": official, "plate": plate,
            "plates": [plate]},
        T: {"popularity": {"status": "unranked", **clock}},
    }


@pytest.fixture
def rig(tmp_path, monkeypatch):
    clock = {"value": NOW}
    monkeypatch.setattr(fb, "_now", lambda: clock["value"])
    monkeypatch.setattr(data_integrity, "_CAL", (0., {date(2026, 11, 4), DAY, date(2026, 11, 6)}))
    switch = {"enabled": True, "realtime_allowed": True, "paused": False, "mode": "poll"}
    svc = fb.FirstBoardService(SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)),
                               history_loader=lambda *_: pl.DataFrame(),
                               quote_service=SimpleNamespace(status=lambda: dict(switch)))
    svc._config = svc._config.model_copy(update={"enabled": True})
    a = row(S)
    a2 = row(S, "shadow", False)
    b = row(T, "shadow", False)
    b["attention_context"]["groups"] = [{"kind": "concept", "peer_rank": 2, "peer_total": 3}]
    b["attention_context"]["intraday_rising"] = None
    b["evidence"] = {}
    svc._snapshot = {**svc._empty_snapshot(), "status": "ready", "as_of": str(DAY),
                     "observed_at": NOW.isoformat(), "rows": [a, a2, b],
                     "attention_sampling": {"status": "not_due", "last_sample_at": NOW.replace(minute=0).isoformat(),
                                            "sample_count": 3, "trade_date": str(DAY), "revision": svc._config.revision},
                     "_data_status_inputs": {"clocks": radar_clocks(radar()), "minute": {"minute_price_basis": "raw"}}}
    svc._latest_quote_times = dict.fromkeys((S, T), NOW.isoformat())
    return svc, clock, switch


def test_summary_counts_stocks_and_verified_negative_results(rig, monkeypatch):
    svc, _, _ = rig
    monkeypatch.setattr(svc._radar, "snapshot", lambda *_a, **_k: pytest.fail("extra radar call"))
    original = deepcopy(svc._snapshot)
    result = svc.snapshot()
    assert "_data_status_inputs" not in result
    assert svc._snapshot == original
    report = result["data_status"]
    assert report["phase"] == "live" and report["symbol_count"] == 2 and report["in_session"] and report["trading_day"]
    assert report["trade_date"] == "2026-11-05" and report["checked_at"] == NOW.isoformat()
    assert report["realtime_enabled"] and report["monitor_enabled"]
    m = report["metrics"]
    assert all(item["total"] == 2 for item in m.values())
    assert {k: v["known"] for k, v in m.items()} == {
        "quotes": 2, "minute": 1, "concept": 1, "industry": 1, "popularity": 2, "theme": 1,
        "ma": 1, "macd": 0, "priority": 2}
    assert m["industry"]["status"] == "partial" and m["macd"]["status"] == "missing"
    assert "可提示 1，影子 1" in m["priority"]["detail"]
    assert "上榜 1，已确认榜外 1" in m["popularity"]["detail"]
    assert "官网核心 1，供应商板块成员 1" in m["theme"]["detail"]
    assert "2026-11-04" in m["ma"]["detail"] and report["sampling"]["sample_count"] == 3


def test_sources_expire_separately_without_fetching_or_touching_events(rig):
    svc, clock, _ = rig
    clock["value"] += timedelta(seconds=6)  # Stock quotes fresh, rank source now older than ten minutes.
    m = svc.snapshot()["data_status"]["metrics"]
    assert m["quotes"]["known"] == 2 and m["popularity"]["status"] == "missing"
    assert m["theme"]["known"] == 1 and "供应商板块成员 0" in m["theme"]["detail"]
    svc._snapshot["rows"][2]["quote_time"] = (NOW - timedelta(seconds=30)).isoformat()
    partial = svc.snapshot()["data_status"]["metrics"]
    assert partial["concept"]["known"] == 0 and partial["priority"]["known"] == 1
    assert "影子 0" in partial["priority"]["detail"] and partial["quotes"]["status"] == "partial"
    clock["value"] += timedelta(seconds=30)
    expired = svc.snapshot()["data_status"]
    assert expired["phase"] == "stale" and expired["metrics"]["minute"]["status"] == "inactive"
    assert expired["metrics"]["industry"]["known"] == 0 and expired["metrics"]["ma"]["known"] == 1


def test_switches_postclose_and_prior_day_sampling_are_separate(rig):
    svc, clock, switch = rig
    switch["enabled"] = False
    off = svc.snapshot()["data_status"]
    assert off["in_session"] and off["monitor_enabled"] and off["realtime_enabled"] is False
    assert off["realtime_status"] == "blocked" and off["phase"] == "waiting"
    switch.update(enabled=True, paused=True)
    paused = svc.snapshot()["data_status"]
    assert paused["realtime_enabled"] and "暂停" in paused["realtime_reason"]
    switch["paused"] = False
    clock["value"] = NOW.replace(hour=15, minute=5)
    closed = svc.snapshot()["data_status"]
    assert closed["phase"] == "closed" and not closed["in_session"] and closed["trading_day"]
    assert closed["same_day"] and closed["metrics"]["priority"]["known"] == 0
    assert closed["sampling"]["sample_count"] == 3 and closed["sampling"]["status"] == "paused"
    clock["value"] = NOW + timedelta(days=1)
    next_day = svc.snapshot()["data_status"]
    assert not next_day["same_day"] and next_day["trade_date"] == "2026-11-05" and next_day["phase"] == "stale"
    assert next_day["metrics"]["ma"]["status"] == "inactive" and "当时覆盖 1/2" in next_day["metrics"]["ma"]["detail"]
    assert next_day["sampling"]["sample_count"] == 0 and next_day["sampling"]["trade_date"] == "2026-11-06"
    svc._snapshot["attention_sampling"]["revision"] = 100
    assert svc.snapshot()["data_status"]["sampling"]["sample_count"] == 0
    svc._config = svc._config.model_copy(update={"enabled": False})
    assert svc.snapshot()["data_status"]["phase"] == "paused"


def test_legacy_empty_snapshot_and_faulted_rows_are_not_actionable(rig):
    svc, _, _ = rig
    svc._snapshot.pop("_data_status_inputs")
    svc._snapshot.update(status="error", rows=[])
    result = svc.snapshot()["data_status"]
    assert result["phase"] == "error" and result["symbol_count"] == 0
    assert result["metrics"]["ma"]["status"] == "empty" and result["metrics"]["priority"]["status"] == "inactive"


def test_technical_readiness_must_match_seed_pattern_and_reference(rig):
    svc, _, _ = rig
    svc._snapshot["rows"] = [row(S)]
    svc._snapshot["environment"] = {"history_validation": {"allowed": True, "reference_date": "2026-11-03"}}
    assert svc.snapshot()["data_status"]["metrics"]["ma"]["known"] == 0
    svc._snapshot["environment"]["history_validation"]["reference_date"] = "2026-11-04"
    svc._snapshot["rows"][0]["pattern"] = "trend"
    assert svc.snapshot()["data_status"]["metrics"]["ma"]["known"] == 0


def test_new_minute_requires_new_complete_endpoint_and_keeps_reason(rig):
    svc, clock, _ = rig
    before = NOW.replace(second=57)
    for r in svc._snapshot["rows"]:
        r["quote_time"] = before.isoformat()
    svc._latest_quote_times = dict.fromkeys((S, T), before.isoformat())
    clock["value"] = before
    assert svc.snapshot()["data_status"]["metrics"]["minute"]["known"] == 1
    clock["value"] += timedelta(seconds=4)
    missing = svc.snapshot()["data_status"]["metrics"]["minute"]
    assert missing["known"] == 0 and "当前已完成分钟" in missing["detail"]
    for r in svc._snapshot["rows"]:
        r["attention_context"].update(status="unavailable", intraday_rising=None, intraday={"reason": "缺少开盘分钟"})
    missing = svc.snapshot()["data_status"]["metrics"]["minute"]
    assert missing["status"] == "missing" and "缺少开盘分钟" in missing["detail"]


def test_malformed_optional_radar_provenance_does_not_break_status():
    assert radar_clocks(None) == {}
    assert radar_clocks({S: "bad", T: {"popularity": {"status": []}, "theme": 1, "plates": 1}}) == {}
    assert radar_clocks({S: {"popularity": {"status": "ready", "source_at": {}, "fetched_at": NOW.isoformat()}}}) == {}


def test_publishing_new_snapshot_keeps_sampling_until_research_write(rig, monkeypatch):
    svc, _, _ = rig
    observed = []
    monkeypatch.setattr(svc, "_prepare", lambda *_: None)
    monkeypatch.setattr(svc._radar, "snapshot", lambda *_a, **_k: {})
    monkeypatch.setattr(svc, "_record_attention", lambda *_a, **_k: observed.append(svc.snapshot()["data_status"]["sampling"]))
    svc.process_snapshot(pl.DataFrame({"symbol": [S], "date": [DAY], "raw_close": [10.],
                                       "quote_ts": [int(NOW.timestamp() * 1000)]}), DAY, now=NOW)
    assert observed[-1]["sample_count"] == 3 and observed[-1]["trade_date"] == str(DAY)
    svc._config = svc._config.model_copy(update={"revision": svc._config.revision + 1})
    svc.process_snapshot(pl.DataFrame(), DAY, now=NOW)
    assert observed[-1]["sample_count"] == 0 and observed[-1]["revision"] == svc._config.revision
