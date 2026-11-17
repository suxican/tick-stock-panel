import json
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from app.plugins.kaipanla import client as kpl
from app.plugins.kaipanla.client import KaipanlaClient, KaipanlaError, to_symbol

FIXTURES = Path(__file__).parent / "fixtures" / "kaipanla"


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _params(request: httpx.Request) -> dict[str, str]:
    raw = request.url.query.decode() if request.method == "GET" else request.content.decode()
    return {k: v[0] for k, v in parse_qs(raw).items()}


def _route(request: httpx.Request) -> httpx.Response:
    p = _params(request)
    a = p["a"]
    if a == "DailyLimitIndex":
        return httpx.Response(200, text=_fixture("limit_index.json"))
    if a == "DailyLimitPerformance":
        return httpx.Response(200, text=_fixture(f"limit_up_p{p['PidType']}.json"))
    if a == "DailyLimitPerformance2":
        return httpx.Response(200, text=_fixture("broken_p1.json"))
    if a == "ChangeStatistics":
        return httpx.Response(200, text=_fixture("strong.json"))
    if a == "MarketStockZDNum":
        return httpx.Response(200, text=_fixture("zdnum.json"))
    if a == "ZhangTingExpression":
        return httpx.Response(200, text=_fixture(f"expression_{p['Day']}.json"))
    raise AssertionError(a)


def _client(handler=_route) -> KaipanlaClient:
    return KaipanlaClient("dev-1", http=httpx.Client(transport=httpx.MockTransport(handler)), min_interval=0)


def _returning(name: str) -> KaipanlaClient:
    return _client(lambda _r: httpx.Response(200, text=_fixture(name)))


def test_to_symbol_suffixes():
    codes = ("600881", "688227", "002451", "300824", "920118", "830799", "430047")
    assert [to_symbol(c) for c in codes] == [
        "600881.SH", "688227.SH", "002451.SZ", "300824.SZ", "920118.BJ", "830799.BJ", "430047.BJ",
    ]


def test_limit_up_rows_are_normalized():
    day, rows = _client().limit_up_stocks(1)
    assert day == "2026-11-17" and len(rows) == 35
    first = rows[0]
    assert first["symbol"] == "002451.SZ" and first["boards"] == 1 and first["reason"] == "电气设备"
    assert first["theme_code"] == "801562" and first["plates"] == ["电气设备", "智能电网"]
    assert first["first_limit_at"] == "2026-11-17T09:25:00+08:00"
    assert first["change_pct"] == pytest.approx(0.1003)
    assert first["label"] is None and first["reseal"] is False
    assert isinstance(first["seal_amount"], (int, float)) and isinstance(first["max_seal"], (int, float))


def test_higher_board_rows_carry_board_count():
    assert {r["boards"] for r in _client().limit_up_stocks(4)[1]} == {4}


def test_empty_board_list_is_valid():
    day, rows = _client().limit_up_stocks(5)
    assert day == "2026-11-17" and rows == []


def test_first_board_broken_rows():
    rows = _client().first_board_broken()
    assert len(rows) == 10
    assert rows[0]["symbol"] == "301421.SZ" and rows[0]["last_broken_at"].startswith("2026-11-17T")
    assert 0 < rows[0]["change_pct"] < 1


def test_counts_sentiment_and_totals():
    c = _client()
    assert c.limit_counts() == [35, 4, 1, 1, 0]
    s = c.sentiment()
    assert s == {"day": "2026-11-17", "kpl_strong": 36, "limit_up": 41, "max_boards": 4, "sharp_drawdown": 5}
    assert c.limit_totals() == {"limit_up": 41, "limit_down": 44}


def test_expression_parses_history_and_maps_1020_to_none():
    exp = _client().expression(date(2026, 11, 16))
    assert exp["limit_up"] == 21 and exp["boards_2"] == 8 and exp["max_boards"] == 1
    assert exp["kpl_promo_2"] == pytest.approx(0.266667, rel=1e-4)
    assert isinstance(exp["kpl_summary"], str)
    assert _returning("expression_1020.json").expression(date(2026, 11, 17)) is None


def test_garbage_or_error_payload_raises():
    with pytest.raises(KaipanlaError):
        _returning("expression_garbage.json").limit_counts()
    with pytest.raises(KaipanlaError):
        _client(lambda _r: httpx.Response(500, text="oops")).sentiment()
    with pytest.raises(KaipanlaError):
        _client(lambda _r: httpx.Response(200, text="not json")).limit_totals()
    with pytest.raises(KaipanlaError):
        _client(lambda _r: httpx.Response(200, json={"errcode": "1020"})).limit_up_stocks(1)


def test_network_error_raises_kaipanla_error():
    def boom(_r):
        raise httpx.ConnectError("down")
    with pytest.raises(KaipanlaError):
        _client(boom).limit_counts()


def test_requests_are_spaced_and_carry_common_params(monkeypatch):
    seen: list[dict] = []

    def record(request):
        seen.append(_params(request))
        return _route(request)

    clock = {"t": 100.0}
    sleeps: list[float] = []
    monkeypatch.setattr(kpl.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(kpl.time, "sleep", lambda s: sleeps.append(s))
    c = KaipanlaClient("dev-1", http=httpx.Client(transport=httpx.MockTransport(record)), min_interval=1.0)
    c.limit_counts()
    c.limit_counts()
    assert sleeps and sleeps[-1] == pytest.approx(1.0)
    p = seen[0]
    assert p["DeviceID"] == "dev-1" and p["PhoneOSNew"] == "2" and p["VerSion"] == "5.23.0.1" and p["apiv"] == "w44"
    assert "Token" not in p and "UserID" not in p
