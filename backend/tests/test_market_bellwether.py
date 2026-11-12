"""盘中风向标: 盘前(T-1)选股、盘中状态归类与综合结论。"""
from datetime import date, datetime
from types import SimpleNamespace

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.market_time import CN_TZ
from app.services import market_bellwether as bw

REFERENCE = date(2026, 11, 10)
TODAY = date(2026, 11, 11)
NOON = datetime(2026, 11, 11, 10, 30, tzinfo=CN_TZ)
AFTER_CLOSE = datetime(2026, 11, 11, 20, 0, tzinfo=CN_TZ)

DRAGON, LEADER, CORE, FOLLOWER, ST_STOCK, NEWCOMER = (
    "600001.SH", "600002.SH", "600003.SH", "600004.SH", "600005.SH", "600006.SH")


def _prev() -> pl.DataFrame:
    return pl.DataFrame({
        "symbol": [DRAGON, LEADER, CORE, FOLLOWER, ST_STOCK, NEWCOMER],
        "date": [REFERENCE] * 6,
        "consecutive_limit_ups": [5, 2, 0, 1, 6, 0],
        "amount": [8e8, 6e8, 30e8, 2e8, 1e8, 1e8],
        "signal_limit_up": [True, True, False, True, True, False],
        "signal_broken_limit_up": [False, False, True, False, False, False],
    })


def _mainlines(kind="concept") -> dict:
    return {"status": "ready", "date": str(REFERENCE), "top_n": 3, "reason": "", "items": [
        {"kind": kind, "name": "机器人", "rank": 1, "leader_symbol": LEADER},
    ]}


def _members(kind="concept") -> dict:
    item = _mainlines(kind)["items"][0]
    return {symbol: [item] for symbol in (LEADER, CORE, FOLLOWER)}


def _roles(items, symbol):
    return [role["key"] for item in items if item["symbol"] == symbol for role in item["roles"]]


# ---------------------------------------------------------------- 盘前选股
def test_emotion_leader_is_highest_board_and_skips_excluded():
    items = bw.select_bellwethers(_prev(), _mainlines(), _members(), excluded=frozenset({ST_STOCK}))
    assert items[0]["symbol"] == DRAGON and items[0]["boards_prev"] == 5
    assert _roles(items, DRAGON) == ["emotion"]
    assert ST_STOCK not in [item["symbol"] for item in items]
    # 不剔除时 6 连板的 ST 才是空间最高
    assert bw.select_bellwethers(_prev(), _mainlines(), _members())[0]["symbol"] == ST_STOCK


def test_emotion_tie_breaks_on_amount_and_needs_two_boards():
    prev = pl.DataFrame({"symbol": [DRAGON, LEADER], "date": [REFERENCE] * 2,
                         "consecutive_limit_ups": [3, 3], "amount": [1e8, 5e8]})
    items = bw.select_bellwethers(prev, {"status": "unavailable", "items": []}, {})
    assert [item["symbol"] for item in items] == [LEADER]
    assert "同高度 2 只" in items[0]["roles"][0]["detail"]
    only_first_boards = prev.with_columns(pl.lit(1).alias("consecutive_limit_ups"))
    assert bw.select_bellwethers(only_first_boards, {"status": "unavailable", "items": []}, {}) == []


def test_mainline_leader_and_core_by_reference_amount():
    items = bw.select_bellwethers(_prev(), _mainlines(), _members(), excluded=frozenset({ST_STOCK}))
    assert [item["symbol"] for item in items] == [DRAGON, LEADER, CORE]
    assert _roles(items, LEADER) == ["leader"] and _roles(items, CORE) == ["core"]
    assert items[2]["roles"][0]["detail"] == "机器人 中军"


def test_same_stock_merges_roles_and_industry_is_used_without_concepts():
    members = {LEADER: _mainlines("industry")["items"]}
    items = bw.select_bellwethers(_prev(), _mainlines("industry"), members)
    assert _roles(items, LEADER) == ["leader", "core"]
    both = {"status": "ready", "items": _mainlines("industry")["items"] + [
        {"kind": "concept", "name": "算力", "rank": 1, "leader_symbol": FOLLOWER}]}
    symbols = [item["symbol"] for item in bw.select_bellwethers(_prev(), both, {})]
    assert FOLLOWER in symbols and LEADER not in symbols
    assert [item["name"] for item in bw.tracked_mainlines(both)] == ["算力"]


# ---------------------------------------------------------------- 盘中状态
@pytest.mark.parametrize("row,sealed,state", [
    ({"change_pct": 0.1, "signal_limit_up": True}, None, "sealed"),
    ({"change_pct": 0.1, "consecutive_limit_ups": 3}, {"sealed": True}, "sealed"),
    ({"change_pct": 0.1, "signal_limit_up": True}, {"sealed": False}, "broken"),
    ({"change_pct": 0.05, "signal_broken_limit_up": True}, None, "broken"),
    ({"change_pct": -0.1, "signal_limit_down": True}, None, "limit_down"),
    ({"change_pct": 0.03}, None, "strong"),
    ({"change_pct": 0.029}, None, "flat"),
    ({"change_pct": -0.03}, None, "weak"),
    ({"change_pct": None}, None, "unknown"),
    ({"change_pct": float("nan")}, None, "unknown"),
    (None, None, "unknown"),
])
def test_stock_state_uses_fraction_units(row, sealed, state):
    assert bw.stock_state(row, sealed) == state


def test_pool_performance_is_fractional_and_empty_is_none():
    today = pl.DataFrame({"symbol": [DRAGON, LEADER, CORE, FOLLOWER],
                          "change_pct": [0.1, -0.02, None, 0.5]})
    perf = bw.pool_performance(today, [DRAGON, LEADER, CORE])
    assert perf == {"count": 2, "avg_pct": pytest.approx(0.04), "up_ratio": pytest.approx(0.5)}
    assert bw.pool_performance(today, [NEWCOMER]) is None
    assert bw.pool_performance(pl.DataFrame(), [DRAGON]) is None


@pytest.mark.parametrize("signals,key", [
    ({"emotion": "strong", "leader": "strong", "core": "neutral", "feedback": "unknown"}, "attack"),
    ({"emotion": "weak", "leader": "strong", "core": "strong", "feedback": "strong"}, "divergence"),
    ({"emotion": "weak", "leader": "neutral", "core": "weak", "feedback": "strong"}, "retreat"),
    ({"emotion": "neutral", "leader": "neutral", "core": "unknown", "feedback": "unknown"}, "divergence"),
    ({"emotion": "strong", "leader": "unknown", "core": "unknown", "feedback": "unknown"}, "unknown"),
])
def test_verdict_follows_the_weakest_signals(signals, key):
    assert bw.verdict(signals)["key"] == key


def test_role_signal_weak_half_wins_and_feedback_thresholds():
    items = [{"roles": [{"key": "leader"}], "signal": "strong"},
             {"roles": [{"key": "leader"}], "signal": "weak"},
             {"roles": [{"key": "core"}], "signal": "unknown"}]
    assert bw.role_signal(items, "leader") == "weak"
    assert bw.role_signal(items, "core") == "unknown"
    assert bw.feedback_signal({"count": 50, "avg_pct": 0.025, "up_ratio": 0.7}) == "strong"
    assert bw.feedback_signal({"count": 50, "avg_pct": -0.001, "up_ratio": 0.55}) == "weak"
    assert bw.feedback_signal({"count": 50, "avg_pct": 0.01, "up_ratio": 0.5}) == "neutral"
    assert bw.feedback_signal(None) == "unknown"


def test_recap_snapshot_keeps_percent_units_on_the_shared_implementation():
    from app.services.market_recap_snapshot import _prev_limit_up_perf

    two_days = pl.DataFrame({
        "symbol": [DRAGON, LEADER, CORE] * 2,
        "date": [REFERENCE] * 3 + [TODAY] * 3,
        "change_pct": [0.1, 0.1, 0.0, 0.05, -0.01, 0.2],
        "signal_limit_up": [True, True, False, False, False, True],
    })
    repo = SimpleNamespace(get_enriched_range=lambda start, end, columns=None: two_days)
    assert _prev_limit_up_perf(repo, TODAY, str(REFERENCE)) == {
        "count": 2, "avg_pct": pytest.approx(2.0), "up_ratio": pytest.approx(50.0)}


# ---------------------------------------------------------------- 装配
def _current(quote_ms: int | None) -> pl.DataFrame:
    frame = pl.DataFrame({
        "symbol": [DRAGON, LEADER, CORE, FOLLOWER, ST_STOCK, NEWCOMER],
        "name": ["龙头", "主线龙", "中军股", "跟风", "ST某", "新面孔"],
        "change_pct": [0.1, 0.045, -0.04, 0.02, 0.05, 0.2],
        "amount": [9e8, 5e8, 40e8, 1e8, 1e8, 90e8],
        "consecutive_limit_ups": [6, 0, 0, 0, 0, 1],
        "signal_limit_up": [True, False, False, False, False, True],
        "signal_broken_limit_up": [False, False, False, False, False, False],
        "signal_limit_down": [False] * 6,
    })
    return frame if quote_ms is None else frame.with_columns(pl.lit(quote_ms, dtype=pl.Int64).alias("quote_ts"))


def _ms(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(bw, "_sessions", lambda day: [REFERENCE, TODAY])
    monkeypatch.setattr(bw, "load_mainlines", lambda data_dir, **kw: _mainlines())
    monkeypatch.setattr(bw, "load_mainline_members", lambda repo, mainlines: _members())
    monkeypatch.setattr(bw, "_excluded_symbols", lambda data_dir: frozenset({ST_STOCK}))
    state = SimpleNamespace(prev=_prev(), current=_current(_ms(NOON) - 5_000), day=TODAY)

    def get_range(start, end, columns=None):
        assert start == end == REFERENCE, "只允许读取上一交易日"
        return state.prev

    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path), get_enriched_range=get_range,
                           get_enriched_latest=lambda: (state.current, state.day),
                           get_name_map=lambda symbols: {})
    state.repo = repo
    return state


def test_snapshot_tracks_preselected_stocks_only(env):
    snap = bw.build_snapshot(env.repo, now=NOON)
    assert snap["status"] == "ready" and snap["live"] and not snap["stale"]
    assert (snap["as_of"], snap["reference_date"]) == (str(TODAY), str(REFERENCE))
    by_symbol = {item["symbol"]: item for item in snap["items"]}
    # 今日涨停放量的新面孔不在盘前名单里, 不能顶替风向标
    assert list(by_symbol) == [DRAGON, LEADER, CORE]
    assert by_symbol[DRAGON] | {"roles": None} == {
        "symbol": DRAGON, "name": "龙头", "roles": None, "boards_prev": 5, "boards_today": 6,
        "change_pct": pytest.approx(0.1), "state": "sealed", "signal": "strong", "seal_vol": None}
    assert by_symbol[LEADER]["state"] == "strong" and by_symbol[CORE]["state"] == "weak"
    assert snap["feedback"]["limit_up"] == {
        "count": 4, "avg_pct": pytest.approx((0.1 + 0.045 + 0.02 + 0.05) / 4), "up_ratio": pytest.approx(1.0)}
    assert snap["feedback"]["broken"]["count"] == 1
    assert snap["verdict"]["key"] == "divergence"
    assert snap["ladder"]["limit_up"] == 2 and snap["ladder"]["max_consecutive"] == 6
    assert snap["quote_time"] == "2026-11-11T10:29:55"


def test_snapshot_uses_depth_seal_and_reports_volume_in_lots(env):
    depth = SimpleNamespace(
        is_sealed_ready=lambda day: True,
        get_sealed_map=lambda day, is_down: {} if is_down else {
            DRAGON: {"sealed": True, "vol": 12345}, NEWCOMER: {"sealed": False, "vol": 0}})
    snap = bw.build_snapshot(env.repo, depth_service=depth, now=NOON)
    assert snap["items"][0]["seal_vol"] == 12345
    assert snap["ladder"]["limit_up"] == 1 and snap["ladder"]["sealed_corrected"] is True


def test_stale_live_quotes_withhold_the_verdict(env):
    env.current = _current(_ms(NOON) - 300_000)
    snap = bw.build_snapshot(env.repo, now=NOON)
    assert snap["stale"] and snap["verdict"]["key"] == "unknown"
    assert "未更新" in snap["verdict"]["reason"]
    # 收盘后同一份数据是定版口径, 不按过期处理
    closed = bw.build_snapshot(env.repo, now=AFTER_CLOSE)
    assert not closed["live"] and not closed["stale"] and closed["verdict"]["key"] == "divergence"


def test_missing_mainlines_keeps_emotion_leader_with_note(env, monkeypatch):
    monkeypatch.setattr(bw, "load_mainlines", lambda data_dir, **kw: {
        "status": "unavailable", "reason": "缺少参考交易日的主线排行", "items": []})
    snap = bw.build_snapshot(env.repo, now=NOON)
    assert [item["symbol"] for item in snap["items"]] == [DRAGON]
    assert any("主线" in note for note in snap["notes"])


@pytest.mark.parametrize("mutate,reason", [
    (lambda env, mp: setattr(env, "prev", None), "收盘数据"),
    (lambda env, mp: setattr(env, "prev", pl.DataFrame()), "收盘数据"),
    (lambda env, mp: mp.setattr(bw, "_sessions", lambda day: None), "交易日历"),
    (lambda env, mp: mp.setattr(bw, "_sessions", lambda day: [TODAY]), "交易日历"),
    (lambda env, mp: setattr(env, "current", pl.DataFrame()), "行情"),
    (lambda env, mp: setattr(env, "day", None), "行情"),
])
def test_snapshot_fails_closed(env, monkeypatch, mutate, reason):
    mutate(env, monkeypatch)
    snap = bw.build_snapshot(env.repo, now=NOON)
    assert snap["status"] == "unavailable" and reason in snap["reason"]
    assert snap["items"] == [] and snap["verdict"]["key"] == "unknown"


def test_duplicate_quote_rows_are_unknown_not_guessed(env):
    env.current = pl.concat([env.current, env.current.filter(pl.col("symbol") == DRAGON)])
    snap = bw.build_snapshot(env.repo, now=NOON)
    assert snap["items"][0]["state"] == "unknown" and snap["items"][0]["change_pct"] is None


# ---------------------------------------------------------------- API
def test_api_returns_snapshot_and_short_caches(env, monkeypatch):
    from app.api.market_recap import router

    calls = []
    real = bw.build_snapshot
    monkeypatch.setattr(bw, "build_snapshot", lambda *a, **kw: calls.append(1) or real(*a, **{**kw, "now": NOON}))
    bw.invalidate_cache()
    app = FastAPI()
    app.include_router(router)
    app.state.repo = env.repo
    client = TestClient(app)
    body = client.get("/api/market-recap/bellwethers").json()
    assert body["status"] == "ready" and body["items"][0]["symbol"] == DRAGON
    client.get("/api/market-recap/bellwethers")
    assert len(calls) == 1
    bw.invalidate_cache()
