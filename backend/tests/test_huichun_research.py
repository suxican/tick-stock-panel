from datetime import date, datetime, timedelta

import polars as pl
import pytest

from app.market_time import CN_TZ
from app.services.huichun_research import evaluate, near_controls, register_rows
from app.strategy.paper import buy_fee, sell_fee

DAY = date(2026, 10, 12)
DAYS = [DAY + timedelta(days=n) for n in (0, 1, 2, 3, 4, 7)]


def candidate():
    return {"id": "test", "symbol": "600001.SH", "name": "样本", "signal_date": str(DAY),
            "g_date": "2026-07-01", "d_date": "2026-09-01", "zero_distance": .015,
            "raw_close": 10., "baseline_fingerprint": "snapshot"}


def scan(rows):
    return {"rules": {"zero_threshold": .02}, "rule_revision": 0, "start_date": str(DAY),
            "end_date": str(DAY), "candidates": rows, "coverage": {"publication_status": "ready"}}


def register(hour=16, offset=0):
    records = {}
    stamp = datetime(2026, 10, 12, hour, tzinfo=CN_TZ) + timedelta(days=offset)
    register_rows(records, scan([candidate()]), [], stamp)
    return records


def run(record, *, n=6, missing=None, events=None, proof=True):
    bars = {d: {"open": p, "close": p, "high": p * 1.02, "low": p * .98}
            for d, p in zip(DAYS, (10., 10.2, 10.4, 10.3, 10., 10.5), strict=True)}
    bars.pop(missing, None)
    facts = {str(d): "普通股票" for d in DAYS} if proof else {}
    evaluate(record, DAYS[:n], bars, events or {}, facts, date(2020, 1, 1), None)


def test_forward_is_frozen_and_clock_or_rescan_cannot_relabel_history():
    records = register()
    original = records["test"]
    register_rows(records, scan([candidate()]), [], datetime(2026, 11, 3, 16, tzinfo=CN_TZ))
    assert len(records) == 1
    assert original["origin"] == "forward"
    assert register(offset=1)["test"]["origin"] == "backfill"
    assert register(hour=14) == {}
    changed = {**candidate(), "baseline_fingerprint": "different", "raw_close": 11}
    register_rows(records, scan([changed]), [], datetime(2026, 11, 3, 16, tzinfo=CN_TZ))
    run(original)
    assert original["base_close"] == 10
    assert original["execution"]["status"] == "baseline_changed"
    assert original["execution"]["net_return_pct"] is None


def test_unready_today_cannot_claim_or_revise_forward_sample_but_history_is_registered():
    rows = {}
    stamp = datetime(2026, 10, 12, 15, 10, tzinfo=CN_TZ)
    yesterday = {**candidate(), "id": "older", "signal_date": "2026-10-09"}
    partial = {**scan([candidate(), yesterday]), "start_date": yesterday["signal_date"],
               "coverage": {"publication_status": "incomplete"}}
    register_rows(rows, partial, [], stamp)
    assert list(rows) == ["older"] and rows["older"]["origin"] == "backfill"
    register_rows(rows, scan([candidate()]), [], stamp.replace(minute=35))
    assert rows["test"]["origin"] == "forward"
    assert rows["test"]["first_seen_at"] == stamp.replace(minute=35).isoformat()
    for items in ([], [{**candidate(), "baseline_fingerprint": "transient"}]):
        register_rows(rows, {**partial, "candidates": items}, [], stamp.replace(minute=40))
        assert not rows["test"]["signal_revised"]


def test_close_observation_open_proxy_costs_and_daily_limits_are_separate():
    row = register()["test"]
    run(row, n=1)
    assert row["execution"]["status"] == row["shape_t5"]["status"] == "pending"
    run(row, n=2)
    assert row["execution"]["status"] == "pending"
    assert row["execution"]["shares"] == 900 and row["execution"]["buy_fee"] == 5
    assert row["execution"]["entry_cost"] > 9000
    assert row["execution"]["sell_fee"] is None
    run(row, proof=False)
    assert row["shape_t5"]["return_pct"] == pytest.approx(.05)
    assert row["execution"]["gross_return_pct"] == pytest.approx(10.4 / 10.2 - 1)
    assert row["execution"]["status"] == "unknown_rules"
    run(row)
    trade = row["execution"]
    assert trade["status"] == "ok"
    assert trade["entry_date"] == str(DAYS[1])
    assert trade["exit_date"] == str(DAYS[2])
    entry, exit_price, qty = 10.22, 10.38, 900
    assert trade["shares"] == qty
    assert trade["entry_price"] == entry and trade["exit_price"] == exit_price
    assert trade["buy_fee"] == buy_fee(qty, entry, .0003)
    assert trade["sell_fee"] == sell_fee(qty, exit_price, .0003, .0005)
    assert trade["entry_cost"] == pytest.approx(qty * entry + trade["buy_fee"])
    assert trade["net_return_pct"] == pytest.approx(
        (qty * exit_price - sell_fee(qty, exit_price, .0003, .0005))
        / (qty * entry + buy_fee(qty, entry, .0003)) - 1)
    run(row, missing=DAYS[1])
    assert row["execution"]["status"] == row["shape_t5"]["status"] == "unknown_gap"
    run(row, events={DAYS[2]: 1.01})
    assert row["execution"]["status"] == "unverified_action"
    assert row["shape_t5"]["return_pct"] == pytest.approx(1.05 * 1.01 - 1)


def test_volatility_is_trailing_unannualized_requires_twenty_returns_in_same_segment():
    from statistics import stdev

    from app.services.huichun_research import volatility_at_cross

    days = [DAY + timedelta(days=i) for i in range(44)]
    prices = [10 + i % 3 for i in range(44)]
    series = pl.DataFrame({"symbol": ["A"] * 44, "date": days, "segment_id": [0]*22 + [1]*22,
                           "close": prices})
    picks = pl.DataFrame({"symbol": ["A"]*5, "r_date": [days[i] for i in (19, 20, 21, 41, 42)]})
    got = volatility_at_cross(series, picks)
    assert got["A", days[19]] is None and got["A", days[41]] is None
    for i in (20, 21, 42):
        assert got["A", days[i]] == pytest.approx(stdev(
            prices[j] / prices[j-1] - 1 for j in range(i-19, i+1)))
    prefix = volatility_at_cross(series.filter(pl.col("date") <= days[20]), picks)
    assert prefix["A", days[20]] == pytest.approx(got["A", days[20]])


def test_single_rejection_at_outer_boundary_keeps_control_out_of_confirmed_group():
    cycles = pl.DataFrame({
        "symbol": list("abcde"), "status": ["first_cross_rejected"] * 5,
        "r_date": [DAY, DAY, DAY, DAY - timedelta(days=1), DAY],
        "reasons": ["zero_distance_exceeded", "zero_distance_exceeded",
                    "zero_distance_exceeded;ma_not_rising", "zero_distance_exceeded",
                    "zero_distance_exceeded"],
        "zero_distance": [.021, .03, .025, .025, .031],
    })
    assert near_controls(cycles, start=DAY, threshold=.02)["symbol"].to_list() == ["a", "b"]


@pytest.mark.parametrize(("i", "price", "status"), [
    (1, 11., "entry_blocked"), (2, 9., "exit_blocked"),
])
def test_open_locked_at_limit_carries_price_observation_only(i, price, status):
    row = register()["test"]
    bars = {day: {"open": 10., "close": 10., "high": 11., "low": 9.} for day in DAYS}
    bars[DAYS[i]]["open"] = price
    names = {str(day): "普通股票" for day in DAYS}
    evaluate(row, DAYS, bars, {}, names, DAY - timedelta(days=400), None)
    assert row["execution"]["status"] == status
    assert row["execution"]["net_return_pct"] is None
    assert row["execution"]["gross_return_pct"] is not None
    if i == 2:
        assert row["execution"]["shares"] > 0 and row["execution"]["sell_fee"] is None


def test_extended_observations_use_causal_high_low_and_separate_horizon_statuses():
    days = [DAY + timedelta(days=i) for i in range(21)]
    bars = {day: {"open": 10., "close": 10., "high": 11., "low": 9.} for day in days}
    # A 2-for-1 split must not appear as a 50% loss or affect the earlier window.
    for day in days[7:]:
        bars[day] = {"open": 5., "close": 5., "high": 5.5, "low": 4.5}
    bars[days[9]]["high"] = 6.
    bars[days[12]]["low"] = 4.
    row = register()["test"]
    evaluate(row, days, bars, {days[7]: 2.}, {}, None, None, benchmarks={
        5: {"status": "ok", "return_pct": .03, "sample_count": 3, "universe_count": 4},
        10: {"status": "ok", "return_pct": -.02, "sample_count": 2, "universe_count": 4},
    })
    observations = {item["horizon"]: item for item in row["observations"]}
    assert row["horizons_mature"]
    assert row["shape_t5"]["return_pct"] == observations[5]["return_pct"] == 0.
    for horizon, high, low in ((5, .1, -.1), (10, .2, -.1), (20, .2, -.2)):
        assert observations[horizon]["status"] == "ok"
        assert observations[horizon]["mfe_pct"] == pytest.approx(high)
        assert observations[horizon]["mae_pct"] == pytest.approx(low)
    assert observations[5]["excess_return_pct"] == pytest.approx(-.03)
    assert observations[10]["excess_return_pct"] == pytest.approx(.02)
    assert observations[5]["benchmark_sample_count"] == 3
    assert observations[5]["benchmark_universe_count"] == 4
    assert observations[20]["benchmark_status"] == "unavailable"
    assert observations[20]["excess_return_pct"] is None

    evaluate(row, days[:11], bars, {days[7]: 2.}, {}, None, None)
    assert not row["horizons_mature"]
    assert [item["status"] for item in row["observations"]] == ["ok", "ok", "pending"]
    bars.pop(days[12])
    evaluate(row, days, bars, {days[7]: 2.}, {}, None, None)
    assert [item["status"] for item in row["observations"]] == ["ok", "ok", "unknown_gap"]
    assert row["observations"][2]["mfe_pct"] is None
    evaluate(row, days, bars, {}, {}, None, days[9])
    assert [item["status"] for item in row["observations"]] == ["ok", "invalid_factor", "invalid_factor"]
    evaluate(row, days, bars, {}, {}, None, None, baseline_changed=True)
    assert all(item["status"] == "baseline_changed" for item in row["observations"])
    assert all(item["return_pct"] is None for item in row["observations"])


def test_benchmark_batches_dates_and_excludes_gaps_invalid_factors_and_other_assets():
    from types import SimpleNamespace

    from app.services.huichun_mode import HuichunModeService
    from app.services.huichun_research import market_benchmarks

    days = [DAY + timedelta(days=i) for i in range(22)]
    symbols = ["600001.SH", "600002.SH", "600003.SH", "600004.SH", "510300.SH"]
    raw = pl.DataFrame([{
        "symbol": symbol, "date": day, "open": 10. + i / 10, "close": 10. + i / 10,
        "high": 11. + i / 10, "low": 9. + i / 10, "volume": 1000., "amount": 10000.,
    } for symbol in symbols for i, day in enumerate(days)
        if not (symbol == "600002.SH" and i == 7)])
    factors = pl.DataFrame({"symbol": ["600003.SH", "600004.SH"],
                            "trade_date": [days[3], None], "ex_factor": [10., 1.]})
    calls = []
    service = SimpleNamespace(
        repo=SimpleNamespace(execute_all=lambda *args: [(symbol,) for symbol in symbols]),
        _check_stop=lambda: None,
        _raw=lambda batch, end, **kwargs: (calls.append(tuple(batch)) or raw.filter(pl.col("symbol").is_in(batch))),
        _factors=lambda batch, end: factors.filter(pl.col("symbol").is_in(batch)),
        _calendar_source="official_exchange_calendar",
        _factor_failure_dates=HuichunModeService._factor_failure_dates,
    )
    service._prepare_scan_batch = lambda *args, **kwargs: HuichunModeService._prepare_scan_batch(service, *args, **kwargs)
    got = market_benchmarks(service, {days[0]: (5, 10, 20), days[1]: (5,), days[10]: (5,)}, days, {})
    assert calls == [tuple(symbols[:4])]
    assert got[days[0]][5] == {"status": "ok", "return_pct": pytest.approx(.05),
                              "sample_count": 2, "universe_count": 3}
    assert got[days[0]][10]["sample_count"] == 1
    assert got[days[0]][20]["return_pct"] == pytest.approx(.2)
    assert got[days[1]][5]["return_pct"] == pytest.approx(10.6 / 10.1 - 1)
    # An earlier anomaly remains quarantined even when outside this observation window.
    assert got[days[10]][5]["sample_count"] == got[days[10]][5]["universe_count"] == 2
    assert got[days[10]][5]["return_pct"] == pytest.approx(11.5 / 11. - 1)
    assert market_benchmarks(service, {}, days, {}) == {}
    assert len(calls) == 1


def test_refresh_upgrades_mature_legacy_rows_without_relabeling_or_per_row_market_reads(monkeypatch):
    from types import SimpleNamespace

    from app.services import huichun_research
    from app.services.huichun_mode import HuichunModeService

    days = [DAY + timedelta(days=i) for i in range(60)]
    symbol = candidate()["symbol"]
    raw = pl.DataFrame([{
        "symbol": symbol, "date": day, "open": 10. + i / 10, "close": 10. + i / 10,
        "high": 11. + i / 10, "low": 9. + i / 10, "volume": 1000., "amount": 10000.,
    } for i, day in enumerate(days[:21])])
    factors = pl.DataFrame(schema=huichun_research.FACTOR_SCHEMA)
    reads = []
    service = SimpleNamespace(
        repo=SimpleNamespace(execute_all=lambda *args: [(symbol,)]),
        _check_stop=lambda: None, _calendar=lambda: days,
        _raw=lambda batch, end, **kwargs: (reads.append(tuple(batch)) or raw),
        _factors=lambda batch, end: factors, _baseline=lambda *args: "snapshot",
        _calendar_source="official_exchange_calendar",
        _factor_failure_dates=HuichunModeService._factor_failure_dates,
    )
    service._prepare_scan_batch = lambda *args, **kwargs: HuichunModeService._prepare_scan_batch(service, *args, **kwargs)
    monkeypatch.setattr(huichun_research, "_instrument_facts", lambda repo: {})
    original = register()["test"]
    original["horizons_mature"] = True  # Old persisted records matured at T+5.
    original["shape_t5"] = {"status": "ok", "return_pct": .05}
    first_seen = original["first_seen_at"]
    previous = {"records": [original, {**original, "id": "control", "cohort": "zero_control"}]}
    now = datetime(2026, 12, 15, 16, tzinfo=CN_TZ)
    result = huichun_research.refresh(service, previous, None, [], now)
    assert len(reads) == 2  # One shared benchmark pass, one monthly candidate batch.
    for row in result["records"]:
        assert row["first_seen_at"] == first_seen and row["origin"] == "forward"
        assert row["shape_t5"]["return_pct"] == pytest.approx(.05)
        assert row["observations"][2]["return_pct"] == pytest.approx(.2)
        assert row["observations"][2]["excess_return_pct"] == pytest.approx(0.)
        assert row["horizons_mature"]
    huichun_research.refresh(service, result, None, [], now)
    assert len(reads) == 2  # Already-mature archives do not trigger another history pass.


def test_research_persistence_refresh_restart_and_failed_commit(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.huichun import router
    from app.services import huichun_mode
    from app.tickflow.repository import DataStore, KlineRepository

    store = DataStore(tmp_path / "data")
    repo = KlineRepository(store)
    app = FastAPI()
    app.state.datastore = store
    app.state.repo = repo
    app.include_router(router)
    try:
        stock = candidate()["symbol"]
        prices = [10., 10.2, 10.4, 10.3, 10., 10.5]
        raw = pl.DataFrame([{
            "symbol": stock, "date": d, "open": p, "high": p + 1, "low": p - 1, "close": p,
            "amount": p * 1000, "volume": 1000.,
        } for d, p in zip(DAYS, prices, strict=True)])
        raw.write_parquet(store.data_dir / "kline_daily/part.parquet")
        store._register_views()
        clock = [datetime(2026, 10, 12, 16, tzinfo=CN_TZ)]
        monkeypatch.setattr(huichun_mode, "_now", lambda: clock[0])
        with TestClient(app) as client:
            svc = app.state.huichun_mode_service
            assert client.get("/api/huichun/research").json()["records"] == []
            baseline = svc._baseline(raw, svc._factors([stock], DAY), stock, DAY)
            control = {**candidate(), "id": "ctrl", "zero_distance": .025,
                       "baseline_fingerprint": baseline}
            def synthetic(*args):
                return {**scan([{**candidate(), "baseline_fingerprint": baseline}]),
                        "id": args[0], "_research_controls": [control]}
            monkeypatch.setattr(svc, "_scan", synthetic)
            for _ in range(2):
                svc.start_scan(DAY, DAY)
                assert svc.wait()["job"]["status"] == "completed"
            assert svc.get_tracking()["records"] == []
            rows = client.get("/api/huichun/research").json()["records"]
            assert {row["cohort"] for row in rows} == {"confirmed", "zero_control"}
            assert len(rows) == 2 and all(row["origin"] == "forward" for row in rows)
            assert all(row["shape_t5"]["status"] == "pending" for row in rows)
            for day in DAYS[1:3]:
                clock[0] = datetime.combine(day, clock[0].timetz())
                pl.DataFrame({"symbol": [stock], "as_of": [day], "name": ["样本"],
                              "listing_date": [date(2020, 1, 1)]}).write_parquet(
                    store.data_dir / "instruments/fixture.parquet")
                store._register_views()
                svc.start_tracking_refresh()
                assert svc.wait()["job"]["status"] == "completed"
            # Both trading-day names were observed on their own dated snapshots.
            assert svc.get_research()["records"][0]["execution"]["status"] == "ok"
            clock[0] += timedelta(days=55)
            svc.start_tracking_refresh()
            assert svc.wait()["job"]["status"] == "completed"
            final = svc.get_research()
            assert final["records"][0]["shape_t5"]["return_pct"] == pytest.approx(.05)
            write = svc._write
            def fail_final(name, data):
                if name == "snapshot.json" and data["job"]["status"] == "completed":
                    raise OSError("fixture")
                write(name, data)
            monkeypatch.setattr(svc, "_write", fail_final)
            clock[0] += timedelta(days=1)
            svc.start_tracking_refresh()
            assert svc.wait()["job"]["status"] == "failed"
            assert svc.get_research() == final
        with TestClient(app) as client:
            assert client.get("/api/huichun/research").json() == final
    finally:
        store.db.close()
