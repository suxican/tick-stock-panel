"""离线修复复用扶摇标准化, 缺缓存或需要接口时必须明确失败。"""

from datetime import date, datetime
from unittest.mock import Mock

import polars as pl
import pytest

from app.config import settings
from app.plugins.fuyao import provider as fp
from app.plugins.fuyao.client import FuyaoError


@pytest.fixture
def cache(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    path = tmp_path / "cache" / "fuyao"
    path.mkdir(parents=True)
    return path


@pytest.fixture
def no_network(monkeypatch):
    client = Mock(side_effect=AssertionError("offline provider constructed a network client"))
    monkeypatch.setattr(fp.fuyao_client, "FuyaoClient", client)
    monkeypatch.setattr(fp, "get_api_key", Mock(side_effect=AssertionError("read API key")))
    return client


def _daily(rows):
    return pl.DataFrame(
        [
            {
                "thscode": symbol,
                "adjusted": "none",
                "date_ms": fp._ms_of_date(day),
                "open_price": close,
                "high_price": close + 0.5,
                "low_price": close - 0.5,
                "close_price": close,
                "volume": 12345.0,
                "turnover": 123450.0,
            }
            for symbol, day, close in rows
        ]
    )


def _events(cache, symbol="001222.SZ"):
    pl.DataFrame(
        {
            "thscode": [symbol],
            "ex_date_ms": [fp._ms_of_date(date(2023, 6, 2))],
            "dividend_per_share": [1.0],
            "per_share_bonus": [0.0],
            "allotment_ratio": [0.0],
            "allotment_price": [0.0],
        }
    ).write_parquet(cache / "adj_factors__20230602.parquet")


def test_default_provider_still_resolves_latest_release_online(cache, monkeypatch):
    path = cache / "daily_k_10d__20230602.parquet"
    _daily([("001222.SZ", date(2023, 6, 2), 9.0)]).write_parquet(path)
    client = Mock()
    client.dump_download_url.return_value = {"presigned_url": "https://example/releases/20230602/x"}
    monkeypatch.setattr(fp.fuyao_client, "FuyaoClient", Mock(return_value=client))
    monkeypatch.setattr(fp, "get_api_key", lambda: "test-key")

    provider = fp.FuyaoProvider()
    assert provider._ensure_dump_path("daily-k-10d", "daily_k_10d") == path
    client.dump_download_url.assert_called_once_with("daily-k-10d")
    client.download_dump.assert_not_called()


def test_cache_only_uses_exact_prefix_latest_release_and_pins_instance(cache, no_network):
    for name in (
        "daily_k__20230601.parquet",
        "daily_k__20230602.parquet",
        "daily_k_10d__20990101.parquet",
        "daily_k__unknown.parquet",
    ):
        (cache / name).touch()
    provider = fp.FuyaoProvider(cache_only=True)
    selected = cache / "daily_k__20230602.parquet"
    assert provider._ensure_dump_path("daily-k", "daily_k") == selected
    (cache / "daily_k__20230603.parquet").touch()
    assert provider._ensure_dump_path("daily-k", "daily_k") == selected
    no_network.assert_not_called()


def test_cache_only_missing_dump_fails_without_creating_cache_directory(
    tmp_path, monkeypatch, no_network
):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    provider = fp.FuyaoProvider(cache_only=True)
    with pytest.raises(FuyaoError, match="daily-k"):
        provider._ensure_dump_path("daily-k", "daily_k")
    assert not (tmp_path / "cache").exists()
    no_network.assert_not_called()


def test_cache_only_client_guard_rejects_even_preexisting_client(no_network):
    provider = fp.FuyaoProvider(cache_only=True)
    provider._client = Mock()
    with pytest.raises(FuyaoError, match="离线"):
        provider._get_client()
    provider._client.historical_kline.assert_not_called()
    no_network.assert_not_called()


def test_cache_only_daily_reuses_raw_price_and_share_to_hand_mapping(cache, no_network):
    _daily(
        [
            ("001222.SZ", date(2023, 5, 1), 10.0),
            ("001222.SZ", date(2023, 6, 2), 9.0),
            ("603291.SH", date(2023, 6, 2), 8.0),
        ]
    ).write_parquet(cache / "daily_k__20230602.parquet")
    provider = fp.FuyaoProvider(cache_only=True)
    result = provider.get_daily(["001222.SZ"], datetime(2023, 5, 1), datetime(2023, 6, 2))
    assert result["date"].to_list() == [date(2023, 5, 1), date(2023, 6, 2)]
    assert result["close"].to_list() == [10.0, 9.0]
    assert result["volume"].to_list() == [123.0, 123.0]
    assert result["amount"].to_list() == [123450.0, 123450.0]
    no_network.assert_not_called()


def test_cache_only_recent_daily_reads_ten_day_cache(cache, no_network):
    _daily([("001222.SZ", date(2023, 6, 2), 9.0)]).write_parquet(
        cache / "daily_k_10d__20230602.parquet"
    )
    result = fp.FuyaoProvider(cache_only=True).get_daily(
        ["001222.SZ"], datetime(2023, 6, 2), datetime(2023, 6, 2)
    )
    assert result["volume"].to_list() == [123.0]
    no_network.assert_not_called()


def test_cache_only_daily_missing_history_raises_instead_of_empty_success(cache, no_network):
    with pytest.raises(FuyaoError, match="离线"):
        fp.FuyaoProvider(cache_only=True).get_daily(
            ["001222.SZ"], datetime(2023, 5, 1), datetime(2023, 6, 2)
        )
    no_network.assert_not_called()


def test_cache_only_daily_window_outside_dump_fails_instead_of_partial_success(cache, no_network):
    _daily([("001222.SZ", date(2023, 6, 2), 9.0)]).write_parquet(
        cache / "daily_k__20230602.parquet"
    )
    with pytest.raises(FuyaoError, match=r"001222\.SZ"):
        fp.FuyaoProvider(cache_only=True).get_daily(
            ["001222.SZ"], datetime(2023, 5, 1), datetime(2023, 6, 2)
        )
    no_network.assert_not_called()


def test_cache_only_adj_factors_reuses_existing_event_formula(cache, no_network):
    _events(cache)
    _daily(
        [
            ("001222.SZ", date(2023, 5, 1), 10.0),
            ("001222.SZ", date(2023, 6, 1), 10.0),
            ("001222.SZ", date(2023, 6, 2), 9.0),
        ]
    ).write_parquet(cache / "daily_k__20230602.parquet")
    result = fp.FuyaoProvider(cache_only=True).get_adj_factors(
        ["001222.SZ"], datetime(2023, 6, 1), datetime(2023, 6, 2)
    )
    assert result.select("symbol", "trade_date").rows() == [("001222.SZ", date(2023, 6, 2))]
    assert result["ex_factor"][0] == pytest.approx(10.0 / 9.0)
    no_network.assert_not_called()


def test_cache_only_adj_missing_event_dump_raises(cache, no_network):
    with pytest.raises(FuyaoError, match="adjustment-factors"):
        fp.FuyaoProvider(cache_only=True).get_adj_factors(
            ["001222.SZ"], datetime(2023, 6, 1), datetime(2023, 6, 2)
        )
    no_network.assert_not_called()


def test_cache_only_adj_missing_symbol_prices_raises(cache, no_network):
    _events(cache)
    _daily(
        [
            ("603291.SH", date(2023, 5, 1), 10.0),
            ("603291.SH", date(2023, 6, 2), 9.0),
        ]
    ).write_parquet(cache / "daily_k__20230602.parquet")
    with pytest.raises(FuyaoError, match="离线"):
        fp.FuyaoProvider(cache_only=True).get_adj_factors(
            ["001222.SZ"], datetime(2023, 6, 1), datetime(2023, 6, 2)
        )
    no_network.assert_not_called()


@pytest.mark.parametrize("cache_only", [False, True])
def test_adj_known_event_without_previous_close_only_fails_in_cache_only_mode(
    cache_only, monkeypatch, no_network
):
    provider = fp.FuyaoProvider(cache_only=cache_only)
    events = pl.DataFrame(
        {
            "symbol": ["001222.SZ", "001222.SZ"],
            "ex_date": [date(2023, 6, 1), date(2023, 6, 2)],
            "dividend": [1.0, 1.0],
            "bonus": [0.0, 0.0],
            "allot": [0.0, 0.0],
            "allot_price": [0.0, 0.0],
        }
    )
    monkeypatch.setattr(provider, "_load_adj_events", lambda *args: events)
    monkeypatch.setattr(
        provider,
        "_closes_from_dumps",
        lambda *args: {"001222.SZ": {date(2023, 6, 1): 10.0, date(2023, 6, 2): 9.0}},
    )
    if cache_only:
        with pytest.raises(FuyaoError, match=r"001222\.SZ.*2023-06-01"):
            provider.get_adj_factors(["001222.SZ"], datetime(2023, 6, 1), datetime(2023, 6, 2))
    else:
        result = provider.get_adj_factors(["001222.SZ"], datetime(2023, 6, 1), datetime(2023, 6, 2))
        assert result["trade_date"].to_list() == [date(2023, 6, 2)]
        assert result["ex_factor"][0] == pytest.approx(10.0 / 9.0)
    no_network.assert_not_called()
