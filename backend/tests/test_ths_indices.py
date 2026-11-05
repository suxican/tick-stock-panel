from datetime import date, datetime, timedelta
from unittest.mock import Mock

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import indices
from app.data_providers import capabilities
from app.data_providers.custom.config import config_from_dict
from app.market_time import CN_TZ
from app.services import ths_indices

SYMBOL = "883421.TI"
NOW = datetime(2026, 11, 5, 14, 30, tzinfo=CN_TZ)


@pytest.fixture
def source(monkeypatch):
    provider = Mock()
    provider.get_ths_index_quotes.return_value = [{
        "symbol": SYMBOL, "last_price": 102.5, "prev_close": 100,
        "change_pct": .025, "snapshot_time": NOW.timestamp() * 1000,
    }]
    monkeypatch.setattr(ths_indices, "cn_now", lambda: NOW)
    monkeypatch.setattr(ths_indices.preferences, "load", lambda: {})
    monkeypatch.setattr(ths_indices.custom, "names", lambda: {"fuyao", "alternate"})
    monkeypatch.setattr(ths_indices.custom, "provider_has_dataset",
                        lambda name, dataset: name in {"fuyao", "alternate"} and dataset == "ths_index")
    monkeypatch.setattr(ths_indices.custom, "get_provider", lambda _: provider)
    ths_indices._work.clear()
    yield provider
    ths_indices._work.clear()


def _client():
    app = FastAPI()
    app.include_router(indices.router)
    return TestClient(app)


def test_quote_api_preserves_snapshot_time_without_inventing_trading_date(source):
    for _ in range(2):
        response = _client().get("/api/index/ths-quotes")
        assert response.status_code == 200
        row = response.json()["rows"][0]
        assert row["change_pct"] == pytest.approx(2.5)
        assert row["snapshot_time"] == NOW.timestamp() * 1000
        assert row["date"] is None
        assert row["source"] == "fuyao"
    source.get_ths_index_quotes.assert_called_once_with()


@pytest.mark.parametrize("field,value", [
    ("snapshot_time", 0), ("snapshot_time", (NOW + timedelta(minutes=1)).timestamp() * 1000),
    ("last_price", float("nan")), ("prev_close", 0), ("symbol", "000985.SH"),
])
def test_invalid_or_other_index_quote_is_not_displayed(source, field, value):
    source.get_ths_index_quotes.return_value[0][field] = value
    body = _client().get("/api/index/ths-quotes").json()
    assert body["rows"] == []
    assert body["reason"]


def test_unavailable_source_has_explicit_failure_and_minute_never_calls_domestic(source, monkeypatch):
    monkeypatch.setattr(ths_indices.custom, "provider_has_dataset", lambda *_: False)
    assert _client().get("/api/index/ths-quotes").json()["reason"]
    source.get_ths_index_quotes.assert_not_called()
    response = _client().get("/api/index/minute", params={"symbol": SYMBOL})
    assert response.json()["source"] == "unsupported"


def test_daily_uses_index_provider_and_warms_indicators_before_window(source):
    source.get_ths_index_daily.return_value = pl.DataFrame({
        "symbol": [SYMBOL] * 80,
        "date": [date(2026, 8, 1) + timedelta(days=i) for i in range(80)],
        "open": [100.] * 80, "high": [104.] * 80, "low": [99.] * 80,
        "close": [102.] * 80, "volume": [10000.] * 80, "amount": [1020000.] * 80,
    })
    start, end = date(2026, 10, 10), date(2026, 10, 15)
    body = _client().get("/api/index/daily", params={
        "symbol": SYMBOL, "start_date": start.isoformat(), "end_date": end.isoformat(),
    }).json()
    assert body["index_info"]["capability"] == "ths_index"
    assert body["rows"][0]["date"] == start.isoformat()
    assert body["rows"][-1]["date"] == end.isoformat()
    assert body["rows"][0]["ma60"] == 102
    source.get_ths_index_daily.assert_called_once_with(SYMBOL, start - timedelta(days=300), end)


def test_source_reload_and_failure_are_isolated(source, monkeypatch):
    assert ths_indices.get_quotes()["rows"]
    replacement = Mock(get_ths_index_quotes=Mock(side_effect=TimeoutError))
    monkeypatch.setattr(ths_indices.custom, "get_provider", lambda _: replacement)
    assert ths_indices.get_quotes()["reason"]
    replacement.get_ths_index_quotes.assert_called_once()


def test_daily_warmup_respects_five_calendar_year_boundary(source):
    source.get_ths_index_daily.return_value = pl.DataFrame()
    ths_indices.get_daily(SYMBOL, date(2022, 1, 1), date(2022, 6, 1))
    source.get_ths_index_daily.assert_called_once_with(SYMBOL, date(2021, 11, 5), date(2022, 6, 1))


def test_daily_distinguishes_temporary_failure_from_empty_window(source):
    source.get_ths_index_daily.side_effect = TimeoutError
    assert ths_indices.get_daily(SYMBOL, date(2026, 11, 1), date(2026, 11, 5))["unavailable"] is True
    source.get_ths_index_daily.side_effect = None
    source.get_ths_index_daily.return_value = pl.DataFrame()
    body = ths_indices.get_daily(SYMBOL, date(2026, 11, 2), date(2026, 11, 5))
    assert body["rows"] == []
    assert "unavailable" not in body


def test_independent_route_defaults_and_matrix_missing_key(source, monkeypatch):
    monkeypatch.setattr(ths_indices.custom, "list_sources", lambda: [])
    monkeypatch.setattr(ths_indices.custom, "list_plugins", lambda: [{
        "name": "fuyao", "display_name": "扶摇", "datasets": ["ths_index"],
        "available": False, "status": "缺少 API Key",
    }])
    matrix = capabilities.build_capability_matrix({}, "expert")
    cap = next(c for c in matrix["capabilities"] if c["id"] == "ths_index")
    assert cap["effective"] == "fuyao"
    assert cap["usable"] is False
    assert cap["tf_available"] is False
    monkeypatch.setattr(ths_indices.preferences, "load", lambda: {"ths_index_data_provider": "alternate"})
    assert ths_indices.preferences.get_ths_index_data_provider() == "alternate"


def test_yaml_rejects_unsupported_mapping():
    with pytest.raises(ValueError, match="Python 插件"):
        config_from_dict({"name": "test", "datasets": {"ths_index": {"url": "https://example.test"}}})
