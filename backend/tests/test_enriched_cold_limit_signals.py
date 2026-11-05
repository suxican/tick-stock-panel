"""扩展数据清缓存后懒加载仍须产出涨跌停信号与连板状态。"""
from datetime import date, timedelta
from types import SimpleNamespace

import polars as pl
import pytest

from app.api.ext_data import _refresh_views
from app.api.screener import limit_ladder
from app.indicators import pipeline
from app.strategy.custom_signals import build_expressions
from app.strategy.signal_seeds import SEED_SIGNALS
from app.tickflow.repository import DataStore, KlineRepository

DAY = date(2026, 10, 29)


class NoDepth:
    def get_sealed_map(self, _day, is_down=False):
        return {}

    def is_sealed_ready(self, _day):
        return False


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline.settings, "data_dir", tmp_path)
    monkeypatch.setattr(pipeline, "_get_custom_signal_exprs", lambda: build_expressions(SEED_SIGNALS))
    result = KlineRepository(DataStore(tmp_path))
    symbols = ["600000.SH", "000001.SZ", "600001.SH"]
    instrument_path = tmp_path / "instruments" / "part.parquet"
    instrument_path.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({
        "symbol": symbols, "name": ["涨停样本", "跌停样本", "炸板样本"],
        "listing_date": [date(2000, 1, 1)] * 3,
    }).write_parquet(instrument_path)
    for offset, closes in enumerate(([10.0, 10.0, 10.0], [11.0, 9.0, 11.0], [12.1, 8.1, 11.5])):
        day = DAY - timedelta(days=2 - offset)
        highs = [12.1, 8.1, 12.1] if day == DAY else closes
        part = tmp_path / "kline_daily_enriched" / f"date={day}" / "part.parquet"
        part.parent.mkdir(parents=True, exist_ok=True)
        pl.DataFrame({
            "symbol": symbols, "date": [day] * 3,
            "open": closes, "high": highs, "low": closes, "close": closes,
            "raw_high": highs, "raw_low": closes, "raw_close": closes,
            "volume": [1000.0] * 3, "amount": [10000.0] * 3,
            "consecutive_limit_ups": [offset, 0, int(offset == 1)],
            "consecutive_limit_downs": [0, offset, 0],
        }).write_parquet(part)
    assert result.refresh_view("kline_enriched", result._enriched_glob)
    yield result
    result.db.close()


@pytest.mark.parametrize("after_extension_refresh", [False, True])
def test_lazy_enriched_keeps_ladder_counts_and_states(repo, after_extension_refresh):
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=repo, depth_service=NoDepth())))
    if after_extension_refresh:
        repo.refresh_cache()
        assert repo._instruments_cache is not None
        _refresh_views(request)
    assert repo._instruments_cache is None

    payload = limit_ladder(request, as_of=DAY, direction="up", ext_columns=None)

    assert payload["counts_raw"] == {"up": 1, "down": 1}
    assert payload["counts"] == {"up": 1, "down": 1}
    stocks = {s["symbol"]: s for tier in payload["tiers"] for s in tier["stocks"]}
    assert stocks["600000.SH"]["status"] == "limit_up"
    assert stocks["600000.SH"]["boards"] == 2
    assert stocks["600001.SH"]["status"] == "broken"
    assert stocks["600001.SH"]["boards"] == 2
    down = limit_ladder(request, as_of=DAY, direction="down", ext_columns=None)
    assert down["tiers"][0]["stocks"][0]["status"] == "limit_down"
    assert down["tiers"][0]["boards"] == 2
    latest, latest_day = repo.get_enriched_latest()
    assert latest_day == DAY
    assert latest.filter(pl.col("symbol") == "600000.SH")["limit_up_price"][0] == pytest.approx(12.095)

