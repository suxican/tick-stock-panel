"""enriched 涨跌停计算读取按日交易所参考数据: 优先级、按日 ST、无限制日与回退。"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import polars as pl
import pytest

from app.data_providers.schemas import PRICE_LIMIT_SCHEMA, ST_STATUS_SCHEMA
from app.indicators import pipeline
from app.services import market_reference as svc

D1, D2 = date(2025, 3, 3), date(2025, 3, 4)  # 2026-07 前主板 ST 为 5%


def _bars(symbol: str, prev: float, close: float) -> pl.DataFrame:
    return pl.DataFrame({
        "symbol": [symbol, symbol], "date": [D1, D2],
        "open": [prev, close], "high": [prev, close], "low": [prev, close], "close": [prev, close],
        "raw_close": [prev, close], "raw_high": [prev, close], "raw_low": [prev, close],
    })


def _write(root: Path, dataset: str, day: date, rows: list[dict], schema: dict) -> None:
    path = svc.path_of(root, dataset, day)
    path.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame([{"date": day, "source": "meoz", **row} for row in rows], schema=schema).write_parquet(path)


def _limits(root: Path, day: date, symbol: str, up: float | None, down: float | None, no_limit=False) -> None:
    _write(root, "price_limits", day, [{"symbol": symbol, "limit_up": up, "limit_down": down,
                                        "no_limit": no_limit}], PRICE_LIMIT_SCHEMA)


def _st(root: Path, day: date, symbols: list[str]) -> None:
    rows = [{"symbol": s, "name": "ST", "st_type": "ST"} for s in symbols] or [
        {"symbol": "000000.SZ", "name": "占位", "st_type": "ST"}]
    _write(root, "st_status", day, rows, ST_STATUS_SCHEMA)


@pytest.fixture
def reader(tmp_path):
    svc.enable_reader(tmp_path)
    yield tmp_path
    svc.enable_reader(None)


def _run(bars: pl.DataFrame, name: str = "普通股") -> dict:
    instruments = pl.DataFrame({"symbol": [bars["symbol"][0]], "name": [name]})
    out = pipeline.compute_limit_signals(bars, instruments, needed={"signal_limit_up", "consecutive_limit_ups"})
    assert not any(c.startswith("_ref_") for c in out.columns)
    return out.row(-1, named=True)


def test_exchange_price_overrides_theoretical_for_s_share(reader):
    # S 股 600182 交易所 5%; 本地规则按 10% 推算会漏判
    bars = _bars("600182.SH", 10.86, 11.40)
    assert _run(bars)["consecutive_limit_ups"] == 0
    _limits(reader, D2, "600182.SH", 11.40, 10.32)
    row = _run(bars)
    assert row["limit_up_price"] == pytest.approx(11.395)
    assert row["consecutive_limit_ups"] == 1


def test_reader_disabled_keeps_local_rules(reader):
    _limits(reader, D2, "600182.SH", 11.40, 10.32)
    svc.enable_reader(None)
    assert _run(_bars("600182.SH", 10.86, 11.40))["limit_up_price"] == pytest.approx(11.945)


def test_daily_st_list_overrides_current_name(reader):
    # 当前名称是 ST, 但当日不在名单 → 10%; 当前名称正常, 当日在名单 → 5%
    _st(reader, D2, [])
    assert _run(_bars("600001.SH", 10.0, 10.50), name="*ST样本")["limit_up_price"] == pytest.approx(10.995)
    svc.path_of(reader, "st_status", D2).unlink()
    _st(reader, D2, ["600002.SH"])
    row = _run(_bars("600002.SH", 10.0, 10.50))
    assert row["limit_up_price"] == pytest.approx(10.495)
    assert row["consecutive_limit_ups"] == 1


def test_day_without_st_list_falls_back_to_name(reader):
    _st(reader, D1, ["600003.SH"])  # 只有 D1 有名单, D2 回退当前名称
    assert _run(_bars("600003.SH", 10.0, 10.50), name="*ST样本")["limit_up_price"] == pytest.approx(10.495)


def test_exchange_no_limit_day_clears_limit_price(reader):
    _limits(reader, D2, "301001.SZ", None, None, no_limit=True)
    row = _run(_bars("301001.SZ", 10.0, 20.0))
    assert row["limit_up_price"] is None and row["consecutive_limit_ups"] == 0


def test_realtime_path_uses_today_reference(reader, monkeypatch):
    monkeypatch.setattr(pipeline, "cn_today", lambda: D2)
    _limits(reader, D2, "600182.SH", 11.40, 10.32)
    today = pl.DataFrame({
        "symbol": ["600182.SH"], "open": [11.4], "high": [11.4], "low": [11.4], "close": [11.4],
        "raw_close": [11.4], "raw_high": [11.4], "raw_low": [11.4], "_prev_close_raw": [10.86],
        "volume": [1000.0],
    })
    out = pipeline._compute_limit_signals_today(today, pl.DataFrame({"symbol": ["600182.SH"], "name": ["S佳通"]}))
    assert out["limit_up_price"][0] == pytest.approx(11.395)
    assert out["consecutive_limit_ups"][0] == 1
    assert "date" not in out.columns and not any(c.startswith("_ref_") for c in out.columns)


def test_risk_warning_exclusion_uses_daily_list_where_available(reader):
    rows = pl.DataFrame({"symbol": ["600001.SH", "600002.SH", "600001.SH", "600002.SH"],
                         "date": [D1, D1, D2, D2]})
    current = {"600002.SH"}  # 当前名单: 600002 戴帽
    _st(reader, D1, ["600001.SH"])  # D1 实际是 600001 戴帽; D2 无名单
    kept = svc.exclude_risk_warning(rows, current).sort("date", "symbol")
    assert kept.rows() == [("600002.SH", D1), ("600001.SH", D2)]
    svc.enable_reader(None)
    assert svc.exclude_risk_warning(rows, current)["symbol"].unique().to_list() == ["600001.SH"]
