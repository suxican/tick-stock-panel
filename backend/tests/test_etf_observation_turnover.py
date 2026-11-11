import os
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.custom import etf_observation
from app.custom.etf_observation import router
from app.custom.etf_observation_data import read_model, turnover
from app.custom.etf_observation_data.ledger import BASKET
from app.market_time import CN_TZ
from app.services.daily_health import DailyIntegrityError

SESSIONS = [date(2026, 10, 1) + timedelta(days=i) for i in range(40) if (date(2026, 10, 1) + timedelta(days=i)).weekday() < 5]
TODAY = SESSIONS[21]  # 21 earlier sessions, so the baseline is the 20 just before it


def _daily(today_amount=300.0, *, history=100.0, skip=()):
    rows = []
    for day in SESSIONS[:22]:
        for symbol in BASKET:
            if (symbol, day) in skip:
                continue
            amount = today_amount if day == TODAY else history
            close = 1.1 if day == TODAY else 1.0
            rows.append({"symbol": symbol, "date": day, "close": close, "amount": amount})
    return pl.DataFrame(rows)


def _ledger(units):
    return {s: {"listed_units": units, "listed_change_units": 2.0} for s in BASKET}


def test_settled_session_compares_full_day_with_twenty_session_average():
    result = turnover.summarize(_daily(190.0), elapsed=None, profiles={}, ledger=_ledger(10.0))
    assert result["as_of"] == TODAY.isoformat()
    assert result["baseline_start"] == SESSIONS[1].isoformat()
    assert result["baseline_end"] == SESSIONS[20].isoformat()
    assert result["live"] is False
    assert result["funds"][0]["ratio"] == pytest.approx(1.9)
    assert result["funds"][0]["change_pct"] == pytest.approx(0.1)  # decimal ratio, not percent
    assert result["totals"]["amount_cny"] == pytest.approx(1900.0)
    assert result["totals"]["avg_amount_cny"] == pytest.approx(1000.0)
    assert result["totals"]["ratio"] == pytest.approx(1.9)
    assert result["totals"]["weighted_change_pct"] == pytest.approx(0.1)
    assert result["conclusion"] == "strong"
    assert turnover.summarize(_daily(140.0), elapsed=None, profiles={}, ledger=None)["conclusion"] == "mild"
    assert turnover.summarize(_daily(129.0), elapsed=None, profiles={}, ledger=None)["conclusion"] == "calm"


def test_missing_history_or_weights_fail_closed():
    gap = _daily(skip={(BASKET[3], SESSIONS[5])})
    result = turnover.summarize(gap, elapsed=None, profiles={}, ledger=None)
    fund = next(f for f in result["funds"] if f["symbol"] == BASKET[3])
    assert fund["avg_amount_cny"] is None and fund["ratio"] is None
    assert result["funds"][0]["ratio"] == pytest.approx(3.0)
    assert result["totals"]["amount_cny"] == pytest.approx(3000.0)  # today's turnover is complete
    assert result["totals"]["avg_amount_cny"] is None and result["totals"]["ratio"] is None
    assert result["conclusion"] is None
    assert result["totals"]["weighted_change_pct"] is None  # no verified units

    partial_units = {s: {"listed_units": 10.0} for s in BASKET[1:]}
    assert turnover.summarize(_daily(), elapsed=None, profiles={}, ledger=partial_units)["totals"]["weighted_change_pct"] is None

    short = _daily().filter(pl.col("date") >= SESSIONS[5])
    assert turnover.summarize(short, elapsed=None, profiles={}, ledger=None)["totals"]["avg_amount_cny"] is None
    assert turnover.summarize(pl.DataFrame(), elapsed=None, profiles={}, ledger=None)["as_of"] is None


def test_intraday_multiple_uses_same_time_profile_or_withholds_it():
    profile = [min(m / 240, 1.0) for m in range(241)]
    half = turnover.summarize(_daily(50.0), elapsed=120, profiles=dict.fromkeys(BASKET, profile), ledger=None)
    assert half["live"] is True and half["elapsed_minutes"] == 120
    assert half["totals"]["progress"] == pytest.approx(0.5)
    assert half["totals"]["ratio"] == pytest.approx(1.0)  # half the average by midday is normal pace

    without = turnover.summarize(_daily(50.0), elapsed=120, profiles={}, ledger=None)
    assert without["totals"]["progress"] == pytest.approx(0.5)
    assert without["totals"]["ratio"] is None and without["conclusion"] is None

    opening = turnover.summarize(_daily(5.0), elapsed=0, profiles=dict.fromkeys(BASKET, profile), ledger=None)
    assert opening["totals"]["ratio"] is None


def _minute_day(day, symbol, morning, afternoon):
    rows = []
    for hour, minute, amount in ((9, 31, morning), (11, 30, 0.0), (13, 1, afternoon), (15, 0, 0.0)):
        rows.append({"symbol": symbol, "datetime": datetime(day.year, day.month, day.day, hour, minute), "amount": amount})
    return rows


def test_profile_averages_complete_days_and_needs_enough_of_them():
    rows = []
    for i, day in enumerate(SESSIONS[:turnover.MIN_PROFILE_DAYS]):
        morning = 60.0 if i % 2 else 40.0
        rows += _minute_day(day, BASKET[0], morning, 100.0 - morning)
    for day in SESSIONS[:turnover.MIN_PROFILE_DAYS - 1]:
        rows += _minute_day(day, BASKET[1], 50.0, 50.0)
    # A day cut off before the close is not a usable intraday shape.
    rows.append({"symbol": BASKET[2], "datetime": datetime(2026, 10, 1, 9, 31), "amount": 10.0})
    profiles = turnover.build_profiles(pl.DataFrame(rows))
    curve = profiles[BASKET[0]]
    assert len(curve) == 241
    assert curve[0] == 0 and curve[1] == pytest.approx(0.5)
    assert curve[120] == pytest.approx(0.5)  # 11:30 to 13:00 all map to minute 120
    assert curve[121] == pytest.approx(1.0) and curve[240] == pytest.approx(1.0)
    assert profiles[BASKET[1]] is None and profiles[BASKET[2]] is None


def _window(rows_by_day):
    return SimpleNamespace(days=list(rows_by_day), by_day=rows_by_day)


def test_share_index_holds_base_navs_so_prices_do_not_move_it():
    def day(units, nav):
        return {s: {"listed_units": units, "unit_nav_cny": nav, "listed_change_value_cny": 1.0} for s in BASKET}

    partial = {s: v for s, v in day(130.0, 9.0).items() if s != BASKET[0]}
    series = turnover.share_series(_window({"d1": day(100.0, 2.0), "d2": day(110.0, 5.0), "d3": partial}))
    assert [p["index"] for p in series["series"]] == [pytest.approx(100.0), pytest.approx(110.0), None]
    assert series["series"][1]["listed_change_value_cny"] == pytest.approx(len(BASKET))
    assert series["series"][2]["listed_change_value_cny"] is None
    assert series["units_as_of"] == "d2" and series["ledger"][BASKET[0]]["listed_units"] == 110.0
    assert turnover.share_series(_window({}))["ledger"] is None


@pytest.fixture
def client(tmp_path, monkeypatch):
    frame = _daily(190.0)
    calls = {}

    def get_etf_daily(symbol, start, end, columns):
        calls["end"] = end
        return frame.filter((pl.col("symbol") == symbol) & (pl.col("date") <= end)).select(columns)

    repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        get_etf_daily=get_etf_daily,
        get_minute_by_dates=lambda *a, **k: pl.DataFrame(),
        get_minute_batch=lambda *a, **k: pl.DataFrame(),
    )
    monkeypatch.setattr(read_model, "cn_today", lambda: TODAY)
    app = FastAPI()
    app.include_router(router)
    app.state.repo = repo
    return TestClient(app), repo, calls


def _at(monkeypatch, hour, minute):
    monkeypatch.setattr(etf_observation, "cn_now", lambda: datetime(TODAY.year, TODAY.month, TODAY.day, hour, minute, tzinfo=CN_TZ))


def test_endpoint_settled_session_without_share_ledger(client, monkeypatch):
    page, _, calls = client
    _at(monkeypatch, 16, 0)
    body = page.get("/api/etf-observation/turnover").json()
    assert calls["end"] == TODAY
    assert body["live"] is False and body["data_time"] is None and body["stale"] is False
    assert body["totals"]["ratio"] == pytest.approx(1.9) and body["conclusion"] == "strong"
    assert body["thresholds"] == {"strong": 1.8, "mild": 1.3}
    assert body["shares"] == {"series": [], "units_as_of": None}
    assert body["totals"]["weighted_change_pct"] is None
    assert body["huijin_period_end"] == "2025-12-31"
    hs300 = next(f for f in body["funds"] if f["symbol"] == "510300.SH")
    assert hs300["huijin_units"] == 37858474974 + 35654598859
    assert next(f for f in body["funds"] if f["symbol"] == "588000.SH")["huijin_units"] == 0


def test_endpoint_uses_partition_write_time_for_todays_row(client, monkeypatch, tmp_path):
    page, _, _ = client
    part = tmp_path / "kline_etf_enriched" / f"date={TODAY.isoformat()}" / "part.parquet"
    part.parent.mkdir(parents=True)
    part.write_bytes(b"x")
    written = datetime(TODAY.year, TODAY.month, TODAY.day, 9, 47, tzinfo=CN_TZ).timestamp()
    os.utime(part, (written, written))

    _at(monkeypatch, 9, 50)
    fresh = page.get("/api/etf-observation/turnover").json()
    assert fresh["live"] is True and fresh["elapsed_minutes"] == 17
    assert fresh["data_time"] == "09:47" and fresh["stale"] is False
    assert fresh["totals"]["ratio"] is None and fresh["conclusion"] is None  # no minute profile

    # Polling stopped in the morning: after the close the row is still a partial one.
    _at(monkeypatch, 15, 30)
    stopped = page.get("/api/etf-observation/turnover").json()
    assert stopped["live"] is True and stopped["stale"] is True and stopped["conclusion"] is None

    closed = datetime(TODAY.year, TODAY.month, TODAY.day, 15, 2, tzinfo=CN_TZ).timestamp()
    os.utime(part, (closed, closed))
    assert page.get("/api/etf-observation/turnover").json()["conclusion"] == "strong"


def test_endpoint_reports_unusable_daily_data(client, monkeypatch):
    page, repo, _ = client
    _at(monkeypatch, 16, 0)

    def broken(*args, **kwargs):
        raise DailyIntegrityError("etf")

    repo.get_etf_daily = broken
    response = page.get("/api/etf-observation/turnover")
    assert response.status_code == 503
    assert "ETF 日 K" in response.json()["detail"]

    repo.get_etf_daily = lambda *a, **k: pl.DataFrame()
    assert page.get("/api/etf-observation/turnover").json()["as_of"] is None


def test_rows_carry_scale_units_recent_turnover_and_holdings():
    huijin = {BASKET[0]: 500, BASKET[1]: 0}
    result = turnover.summarize(_daily(190.0), elapsed=None, profiles={}, ledger=_ledger(10.0), huijin=huijin)
    fund = result["funds"][0]
    assert fund["size_cny"] == pytest.approx(11.0)  # verified units x today's close
    assert fund["listed_units"] == 10.0 and fund["listed_change_units"] == 2.0
    assert fund["huijin_units"] == 500 and result["funds"][1]["huijin_units"] == 0
    assert result["funds"][2]["huijin_units"] is None
    assert len(fund["recent_amounts_cny"]) == len(result["recent_dates"]) == 22
    assert result["recent_dates"][-1] == TODAY.isoformat()
    assert fund["recent_amounts_cny"][-1] == 190.0 and fund["recent_amounts_cny"][0] == 100.0
    no_ledger = turnover.summarize(_daily(), elapsed=None, profiles={}, ledger=None)["funds"][0]
    assert no_ledger["size_cny"] is None and no_ledger["listed_units"] is None


def test_closing_half_hour_share_needs_both_curves_and_a_settled_session():
    usual = [0.0] * 241
    usual[turnover.TAIL_START] = 0.9
    today = [0.0] * 241
    today[turnover.TAIL_START] = 0.7
    profiles, curves = dict.fromkeys(BASKET, usual), dict.fromkeys(BASKET, today)
    settled = turnover.summarize(_daily(), elapsed=None, profiles=profiles, ledger=None, today_curves=curves)
    assert settled["totals"]["tail_share"] == pytest.approx(0.3)
    assert settled["totals"]["tail_share_avg"] == pytest.approx(0.1)
    missing = dict(curves, **{BASKET[0]: None})
    assert turnover.summarize(_daily(), elapsed=None, profiles=profiles, ledger=None, today_curves=missing)["totals"]["tail_share"] is None
    live = turnover.summarize(_daily(), elapsed=100, profiles=profiles, ledger=None, today_curves=curves)
    assert live["totals"]["tail_share"] is None and live["totals"]["tail_share_avg"] is None


def test_huijin_units_count_only_huijin_entities():
    disclosures = {"funds": [
        {"symbol": "510310.SH", "holders": [
            {"kind": "direct", "units": 30}, {"kind": "direct", "units": 20}, {"kind": "named_plan", "units": 7},
        ]},
        {"symbol": "588000.SH", "holders": []},
    ]}
    assert turnover.huijin_units(disclosures) == {"510310.SH": 50, "588000.SH": 0}
    assert turnover.huijin_units({"funds": []}) == {}
