"""猫爪异动订阅: 推送解析、seq 缺口、首次触板时间、留档恢复、重连与门禁。"""
from __future__ import annotations

import json
from datetime import date, datetime

import httpx
import pytest

from app.market_time import CN_TZ
from app.plugins.meoz import client as mc
from app.plugins.meoz.client import MeozAuthError, MeozError
from app.services import meoz_limit_stream as ls

NOW = datetime(2026, 11, 19, 10, 0, 5, tzinfo=CN_TZ)
FIELDS = ["symbol", "name", "time", "event", "tradedate", "servertime", "last_price", "limit_price",
          "amount", "turnover_rate", "fd_amount", "trade_amount_3s", "fd_decrease_12s",
          "fd_decrease_rate_12s", "limit_times", "open_times"]
SUB = json.dumps({"type": "ack", "op": "sub", "fields": FIELDS})


def _ms(hh, mm, ss) -> int:
    return int(datetime(2026, 11, 19, hh, mm, ss, tzinfo=CN_TZ).timestamp() * 1000)


def row(symbol, event, ms, fd=None) -> list:
    return [symbol, "名", ms, event, "20261119", "10:00:00", 10.0, 10.0, 1e8, 3.0, fd, None, None, None, 1, 0]


def push(seq, *rows) -> str:
    return json.dumps({"t": "stream_limit_event_v2", "sid": "limit_event_v2", "seq": seq, "i": list(rows)})


class FakeWs:
    """依次吐出脚本里的消息; 吐完后设置停止并抛 TimeoutError。"""

    def __init__(self, messages, on_done):
        self.messages, self.on_done, self.closed = list(messages), on_done, False

    def recv(self, timeout=None):
        if self.messages:
            item = self.messages.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        self.on_done()
        raise TimeoutError

    def close(self):
        self.closed = True


def make(tmp_path, scripts, **kw):
    """scripts: 每次连接的消息列表; 用完后停止。"""
    stream_holder = {}
    tickets, urls = [], []
    pending = list(scripts)

    def ticket(body):
        tickets.append(body)
        return {"url": "wss://example.invalid/ws?ticket=SECRET"}

    def connect(url):
        urls.append(url)
        return FakeWs(pending.pop(0), lambda: None if pending else stream_holder["s"]._stop.set())

    stream = ls.LimitStream(tmp_path, now=lambda: NOW, is_trading_day=lambda _n: True, ticket=ticket,
                            connect=connect, sleep=lambda s: stream_holder["s"]._stop.is_set(), **kw)
    stream_holder["s"] = stream
    return stream, tickets, urls


def test_parse_push_follows_ack_field_order_and_skips_bad_rows():
    msg = json.loads(push(1, row("002343.SZ", 1, _ms(9, 56, 36)), ["short"], row(None, 1, 1)))
    out = ls.parse_push(FIELDS, msg, 123)
    assert [(e["symbol"], e["event"], e["recv_ms"]) for e in out] == [("002343.SZ", 1, 123)]
    assert "turnover_rate" not in out[0]


def test_events_touch_times_and_archive(tmp_path):
    stream, tickets, _ = make(tmp_path, [[SUB,
        push(1, row("000001.SZ", 2, _ms(9, 40, 0), 2e7)),          # 封死涨停: 晚于触板
        push(2, row("000001.SZ", 1, _ms(9, 38, 10)), row("000002.SZ", 7, _ms(9, 41, 0))),  # 冲击涨停 + 冲击跌停
        push(3, row("000001.SZ", 3, _ms(9, 50, 0)))]])
    stream._run()
    touch = stream.touch_times()
    assert set(touch) == {"000001.SZ"}  # 跌停方向事件不计入触板
    assert (touch["000001.SZ"].at.strftime("%H:%M:%S"), touch["000001.SZ"].source) == ("09:38:10", "touch")
    assert len(stream.events()) == 4 and len(stream.events("000002.SZ")) == 1
    assert tickets[0]["ttl_seconds"] == 600 and tickets[0]["symbols"] == []
    assert stream.status()["seq_gaps"] == 0 and stream.status()["events_today"] == 4
    path = tmp_path / "meoz_limit_events" / "v1" / "date=2026-11-19.jsonl"
    assert len(path.read_text(encoding="utf-8").splitlines()) == 4


def test_restart_restores_today_from_archive(tmp_path):
    first, _, _ = make(tmp_path, [[SUB, push(1, row("000001.SZ", 2, _ms(9, 40, 0), 2e7))]])
    first._run()
    second = ls.LimitStream(tmp_path, now=lambda: NOW)
    second._ingest([{"symbol": "000009.SZ", "name": "x", "time": _ms(9, 45, 0), "event": 1}], NOW)
    assert set(second.touch_times()) == {"000001.SZ", "000009.SZ"}
    assert second.touch_times()["000001.SZ"].source == "sealed"
    other_day = datetime(2026, 11, 20, 10, 0, tzinfo=CN_TZ)
    second._ingest([{"symbol": "000008.SZ", "name": "x", "time": 1, "event": 1}], other_day)
    assert second.touch_times(other_day.date()).keys() == {"000008.SZ"} and second.events(day=NOW.date()) == []


def test_seq_gap_counted_and_non_stream_messages_ignored(tmp_path):
    stream, _, _ = make(tmp_path, [[SUB, "not json", json.dumps({"type": "ack", "op": "connect"}),
                                    push(1, row("000001.SZ", 1, _ms(9, 38, 0))),
                                    push(4, row("000002.SZ", 1, _ms(9, 39, 0)))]])
    stream._run()
    assert stream.status()["seq_gaps"] == 1 and len(stream.events()) == 2


def test_seq_gaps_reset_on_day_change(tmp_path):
    stream, _, _ = make(tmp_path, [[SUB, push(1, row("000001.SZ", 1, _ms(9, 38, 0))),
                                    push(4, row("000002.SZ", 1, _ms(9, 39, 0)))]])
    stream._run()
    assert stream.status()["seq_gaps"] == 1
    next_day = datetime(2026, 11, 20, 10, 0, tzinfo=CN_TZ)
    stream._ingest([{"symbol": "000003.SZ", "name": "x", "time": 1, "event": 1}], next_day)
    assert stream.status()["seq_gaps"] == 0


def test_reconnects_with_fresh_ticket_after_disconnect(tmp_path):
    stream, tickets, urls = make(tmp_path, [
        [SUB, push(1, row("000001.SZ", 1, _ms(9, 38, 0))), ConnectionError("wss://x?ticket=SECRET")],
        [SUB, push(1, row("000002.SZ", 1, _ms(9, 39, 0)))]])
    stream._run()
    assert len(tickets) == 2 and len(urls) == 2
    assert set(stream.touch_times()) == {"000001.SZ", "000002.SZ"}
    status = stream.status()
    assert (status["reconnects"], status["last_error"]) == (1, None)  # 第二次连接成功后清除错误
    assert "SECRET" not in json.dumps(status)


def test_auth_error_waits_long_and_exposes_no_secret(tmp_path):
    waits = []
    stream = ls.LimitStream(tmp_path, now=lambda: NOW, is_trading_day=lambda _n: True,
                            ticket=lambda b: (_ for _ in ()).throw(MeozAuthError("ws-ticket 拒绝访问 (403)")),
                            sleep=lambda s: waits.append(s) or True)
    stream._run()
    assert waits == [300] and stream.status()["state"] == "error" and "403" in stream.status()["last_error"]


@pytest.mark.parametrize("enabled, trading, hour, connects", [
    (True, True, 10, 1), (False, True, 10, 0), (True, False, 10, 0), (True, None, 10, 0), (True, True, 8, 0), (True, True, 16, 0)])
def test_connects_only_in_window_and_when_enabled(tmp_path, enabled, trading, hour, connects):
    clock = datetime(2026, 11, 19, hour, 30, tzinfo=CN_TZ)
    holder = {}
    calls = []

    def connect(url):
        calls.append(url)
        return FakeWs([SUB], lambda: holder["s"]._stop.set())

    stream = ls.LimitStream(tmp_path, enabled=lambda: enabled, now=lambda: clock, is_trading_day=lambda _n: trading,
                            ticket=lambda b: {"url": "wss://x"}, connect=connect,
                            sleep=lambda s: holder["s"]._stop.set() or True)
    holder["s"] = stream
    stream._run()
    assert len(calls) == connects
    assert stream.status()["state"] in ("idle", "disabled")


# ---- 猫爪客户端取票据 ----

def _client(monkeypatch, handler) -> mc.MeozClient:
    monkeypatch.setattr(mc.time, "sleep", lambda _s: None)
    client = mc.MeozClient("test-key")
    client._http = httpx.Client(transport=httpx.MockTransport(handler))
    return client


def test_client_ws_ticket_posts_documented_body_and_returns_data(monkeypatch):
    seen = {}

    def handler(request):
        seen["url"], seen["body"] = str(request.url), json.loads(request.content)
        return httpx.Response(200, json={"code": 200, "data": {"url": "wss://h/x?ticket=T", "tick_sub_limit": 6000}})

    data = _client(monkeypatch, handler).ws_ticket({"channel": "tick_stream"})
    assert seen["url"] == "https://sz.meoz.cn:6688/api/v2/realtime/ws-ticket"
    assert seen["body"] == {"apikey": "test-key", "channel": "tick_stream"} and data["url"].endswith("T")


@pytest.mark.parametrize("response, error", [
    (httpx.Response(403, json={"code": 403, "message": "无权"}), MeozAuthError),
    (httpx.Response(200, json={"code": 200, "data": {}}), MeozError),
    (httpx.Response(502, text="bad gateway"), MeozError)])
def test_client_ws_ticket_errors(monkeypatch, response, error):
    with pytest.raises(error):
        _client(monkeypatch, lambda request: response).ws_ticket({})


# ---- 涨停池兜底 ----

def test_pool_poll_fills_missing_and_keeps_earlier_event(tmp_path):
    stream = ls.LimitStream(tmp_path, now=lambda: NOW,
                            pool=lambda day: [("000001.SZ", "09:50:00"), ("000002.SZ", "09:31:07"), ("000003.SZ", "bad"), ("000004.SZ", None)])
    stream._ingest([{"symbol": "000001.SZ", "name": "x", "time": _ms(9, 38, 10), "event": 1}], NOW)
    stream.poll_pool()
    touch = stream.touch_times()
    assert (touch["000001.SZ"].at.strftime("%H:%M:%S"), touch["000001.SZ"].source) == ("09:38:10", "touch")
    assert (touch["000002.SZ"].at.strftime("%H:%M:%S"), touch["000002.SZ"].source) == ("09:31:07", "pool")
    assert set(touch) == {"000001.SZ", "000002.SZ"} and stream.status()["pool_error"] is None


def test_pool_earlier_than_sealed_event_wins_and_failures_are_contained(tmp_path):
    stream = ls.LimitStream(tmp_path, now=lambda: NOW, pool=lambda day: [("000001.SZ", "09:35:00")])
    stream._ingest([{"symbol": "000001.SZ", "name": "x", "time": _ms(9, 40, 0), "event": 2}], NOW)
    stream.poll_pool()
    assert stream.touch_times()["000001.SZ"].source == "pool"
    stream._pool = lambda day: (_ for _ in ()).throw(RuntimeError("503"))
    stream.poll_pool()
    assert stream.status()["pool_error"] == "RuntimeError" and "000001.SZ" in stream.touch_times()


def test_default_pool_ignores_limit_down_rows(monkeypatch):
    import polars as pl

    frame = pl.DataFrame({"symbol": ["a", "b", "c"], "type": ["u", "d", "ub"], "first_time": ["09:31:00", "09:32:00", "09:33:00"]})
    provider = type("P", (), {"get_limit_events": lambda self, day: frame})()
    monkeypatch.setattr(ls.custom_sources, "get_provider", lambda name: provider)
    assert ls._default_pool(NOW.date()) == [("a", "09:31:00", None), ("c", "09:33:00", None)]

# ---- limit_state: 事件流正向证据 ----

def _state_stream(tmp_path, *events, now=NOW):
    stream = ls.LimitStream(tmp_path, now=lambda: now)
    stream._ingest([{"symbol": sym, "name": "x", "time": ms, "event": ev} for sym, ev, ms in events], NOW)
    return stream


def test_limit_state_confirmed_seal(tmp_path):
    stream = _state_stream(tmp_path, ("000001.SZ", 1, _ms(9, 40, 0)), ("000001.SZ", 3, _ms(9, 41, 0)))
    st = stream.limit_state()["000001.SZ"]
    assert st.status == "sealed_confirmed" and st.at.strftime("%H:%M:%S") == "09:41:00"
    assert st.touch_at.strftime("%H:%M:%S") == "09:40:00" and st.confirmed_opens == 0 and st.pool_open_times is None


def test_limit_state_touching_expires(tmp_path):
    stream = _state_stream(tmp_path, ("000001.SZ", 1, _ms(9, 59, 30)))
    assert stream.limit_state()["000001.SZ"].status == "touching"
    later = datetime(2026, 11, 19, 10, 1, 0, tzinfo=CN_TZ)
    assert stream.limit_state(now=later)["000001.SZ"].status == "none"


def test_limit_state_opened_counts_confirmed_opens(tmp_path):
    seq = [(1, (9, 31, 0)), (3, (9, 32, 1)), (4, (9, 35, 2)), (1, (9, 40, 7)), (3, (9, 41, 9)), (4, (9, 50, 3))]
    stream = _state_stream(tmp_path, *[("600360.SH", ev, _ms(*t)) for ev, t in seq])
    st = stream.limit_state()["600360.SH"]
    assert st.status == "opened" and st.confirmed_opens == 2 and st.at.strftime("%H:%M:%S") == "09:50:03"


def test_limit_state_same_millisecond_prefers_open(tmp_path):
    stream = _state_stream(tmp_path, ("000001.SZ", 4, _ms(9, 50, 0)), ("000001.SZ", 1, _ms(9, 50, 0)))
    assert stream.limit_state()["000001.SZ"].status == "opened"


def test_limit_state_wrong_day_empty(tmp_path):
    stream = _state_stream(tmp_path, ("000001.SZ", 1, _ms(9, 59, 30)))
    assert stream.limit_state(day=date(2026, 11, 18)) == {}


def test_limit_state_restored_from_archive(tmp_path):
    first = _state_stream(tmp_path, ("000001.SZ", 1, _ms(9, 40, 0)), ("000001.SZ", 3, _ms(9, 41, 0)))
    assert first.limit_state()
    second = ls.LimitStream(tmp_path, now=lambda: NOW)
    assert second.limit_state()["000001.SZ"].status == "sealed_confirmed"


def test_limit_state_ignores_unverified_events(tmp_path):
    stream = _state_stream(tmp_path, *[("000001.SZ", ev, _ms(9, 40, ev)) for ev in (2, 5, 6, 7)])
    assert stream.limit_state() == {}


def test_poll_pool_records_open_times(tmp_path):
    stream = ls.LimitStream(tmp_path, now=lambda: NOW,
                            pool=lambda day: [("600360.SH", "09:32:36", 4), ("X", "09:30:00")])
    stream._ingest([{"symbol": "600360.SH", "name": "x", "time": _ms(9, 31, 0), "event": 1}], NOW)
    stream.poll_pool()
    state = stream.limit_state()
    assert state["600360.SH"].pool_open_times == 4
    assert "X" not in state and stream._pool_opens == {"600360.SH": 4}

