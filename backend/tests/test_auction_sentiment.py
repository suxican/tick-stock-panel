"""竞价情绪 (情绪周期新维度): 集合竞价按日归档 + 日线昨日涨停 → 每日指标。只展示, 不改阶段规则。"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import polars as pl
import pytest

from app.data_providers.schemas import AUCTION_SCHEMA, PRICE_LIMIT_SCHEMA, ST_STATUS_SCHEMA
from app.services import auction_sentiment as au
from app.services import market_reference

D0, D1 = date(2020, 3, 2), date(2020, 3, 3)


def _enriched(data_dir: Path, day: date, sealed: tuple[str, ...], others: tuple[str, ...] = ()):
    rows = [{"symbol": s, "date": day, "consecutive_limit_ups": 1} for s in sealed]
    rows += [{"symbol": s, "date": day, "consecutive_limit_ups": 0} for s in others]
    out = data_dir / "kline_daily_enriched" / f"date={day.isoformat()}" / "part.parquet"
    out.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(rows, schema={"symbol": pl.String, "date": pl.Date, "consecutive_limit_ups": pl.Int64}).write_parquet(out)


def _auction(data_dir: Path, day: date, rows: dict[str, tuple[float, float, float]]):
    """rows: {symbol: (price, change_pct, amount)}"""
    out = market_reference.path_of(data_dir, "auction", day)
    out.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame([{"date": day, "symbol": s, "price": p, "change_pct": c, "amount": a, "volume": a / p,
                   "unmatched_volume": 0.0, "source": "meoz"} for s, (p, c, a) in rows.items()],
                 schema=AUCTION_SCHEMA).write_parquet(out)


def _limits(data_dir: Path, day: date, rows: dict[str, tuple[float | None, float | None]]):
    out = market_reference.path_of(data_dir, "price_limits", day)
    out.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame([{"date": day, "symbol": s, "limit_up": u, "limit_down": d, "no_limit": u is None, "source": "meoz"}
                  for s, (u, d) in rows.items()], schema=PRICE_LIMIT_SCHEMA).write_parquet(out)


def _st(data_dir: Path, day: date, symbols: list[str]):
    out = market_reference.path_of(data_dir, "st_status", day)
    out.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame([{"date": day, "symbol": s, "name": "ST", "st_type": "ST", "source": "meoz"} for s in symbols],
                 schema=ST_STATUS_SCHEMA).write_parquet(out)


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    market_reference.enable_reader(tmp_path)
    monkeypatch.setattr(au, "_current_st_symbols", lambda data_dir: frozenset())
    au.clear_cache()
    yield tmp_path
    market_reference.enable_reader(None)
    au.clear_cache()


def _day(frame: pl.DataFrame, day: date) -> dict:
    return frame.filter(pl.col("date") == day).row(0, named=True)


def test_open_ratios_limit_counts_and_amount(data_dir):
    _enriched(data_dir, D0, ())
    _enriched(data_dir, D1, ())
    _auction(data_dir, D1, {"600001.SH": (11.0, 0.10, 1e6), "600002.SH": (9.0, -0.10, 2e6),
                            "600003.SH": (10.1, 0.01, 3e6), "600004.SH": (9.9, -0.01, 4e6)})
    _limits(data_dir, D1, {"600001.SH": (11.0, 9.0), "600002.SH": (11.0, 9.0),
                           "600003.SH": (11.11, 9.09), "600004.SH": (10.89, 8.91)})
    row = _day(au.compute_auction_sentiment(data_dir, exclude_st=True), D1)
    assert (row["count"], row["high_open_ratio"], row["low_open_ratio"]) == (4, 0.5, 0.5)
    assert row["median_pct"] == pytest.approx(0.0)
    assert (row["limit_up_open"], row["limit_down_open"]) == (1, 1)
    assert row["amount"] == pytest.approx(1e7)


def test_previous_sealed_cohort_money_effect(data_dir):
    _enriched(data_dir, D0, ("600001.SH", "600002.SH", "600003.SH"), others=("600009.SH",))
    _enriched(data_dir, D1, ())
    _auction(data_dir, D1, {"600001.SH": (11.0, 0.05, 1e6), "600002.SH": (9.0, -0.04, 1e6),
                            "600009.SH": (10.0, 0.08, 1e6)})  # 600003 停牌无竞价, 不进均值
    row = _day(au.compute_auction_sentiment(data_dir, exclude_st=True), D1)
    assert row["prev_sealed"] == 2
    assert row["prev_mean_pct"] == pytest.approx(0.005)
    assert row["prev_high_open_ratio"] == pytest.approx(0.5)
    assert row["prev_weak_count"] == 1  # 竞价 <= -3% 的「核按钮」


def test_st_and_no_limit_new_stocks_are_excluded(data_dir):
    _enriched(data_dir, D0, ("600001.SH",))
    _enriched(data_dir, D1, ())
    _auction(data_dir, D1, {"600001.SH": (11.0, 0.10, 1e6), "600002.SH": (12.0, 0.44, 1e6),
                            "600003.SH": (9.5, -0.05, 1e6)})
    _limits(data_dir, D1, {"600001.SH": (11.0, 9.0), "600002.SH": (None, None), "600003.SH": (10.5, 9.5)})
    _st(data_dir, D1, ["600001.SH"])
    row = _day(au.compute_auction_sentiment(data_dir, exclude_st=True), D1)
    assert row["count"] == 1 and row["low_open_ratio"] == 1.0  # ST 与无涨跌幅限制的新股都剔除
    assert row["prev_sealed"] == 0 and row["prev_mean_pct"] is None
    kept = _day(au.compute_auction_sentiment(data_dir, exclude_st=False), D1)
    assert kept["count"] == 2  # 关闭 ST 剔除后 ST 保留, 新股仍剔除


def test_missing_limits_or_auction_are_null_not_zero(data_dir):
    _enriched(data_dir, D0, ())
    _enriched(data_dir, D1, ())
    _auction(data_dir, D1, {"600001.SH": (10.0, 0.0, 1e6)})
    frame = au.compute_auction_sentiment(data_dir, exclude_st=True)
    assert frame["date"].to_list() == [D1]  # D0 没有竞价分区: 不出行
    row = frame.row(0, named=True)
    assert row["limit_up_open"] is None and row["limit_down_open"] is None  # 无交易所涨跌停价
    assert row["high_open_ratio"] == 0.0 and row["low_open_ratio"] == 0.0


def test_amount_ratio_to_previous_five_sessions(data_dir):
    days = [date(2020, 3, d) for d in (2, 3, 4, 5, 6, 9)]
    for i, day in enumerate(days):
        _enriched(data_dir, day, ())
        _auction(data_dir, day, {"600001.SH": (10.0, 0.0, 1e6 * (2 if i == 5 else 1))})
    frame = au.compute_auction_sentiment(data_dir, exclude_st=True)
    assert _day(frame, days[5])["amount_ratio_5d"] == pytest.approx(2.0)
    assert _day(frame, days[0])["amount_ratio_5d"] is None  # 不足 5 个前序交易日


def test_payload_latest_with_percentiles(data_dir):
    _enriched(data_dir, D0, ())
    _enriched(data_dir, D1, ())
    _auction(data_dir, D1, {"600001.SH": (10.1, 0.01, 1e6)})
    payload = au.auction_sentiment_payload(data_dir, None, None)
    assert payload["latest"]["date"] == D1.isoformat()
    assert payload["latest"]["pct"]["high_open_ratio"] is None  # 样本不足 60 天
    assert payload["first_date"] == D1.isoformat() and len(payload["rows"]) == 1


def test_api_route_passes_range_and_validates_dates(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import regime as regime_api

    calls = []
    monkeypatch.setattr(au, "auction_sentiment_payload",
                        lambda data_dir, start, end: calls.append((data_dir, start, end)) or {"rows": []})
    app = FastAPI()
    app.include_router(regime_api.router)
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    http = TestClient(app)
    assert http.get("/api/regime/auction-sentiment", params={"start": "2026-01-02", "end": "2026-11-20"}).json() == {"rows": []}
    assert calls == [(tmp_path, date(2026, 1, 2), date(2026, 11, 20))]
    assert http.get("/api/regime/auction-sentiment", params={"start": "2026-13-01"}).status_code == 422
