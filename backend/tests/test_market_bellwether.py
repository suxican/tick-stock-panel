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
        "change_pct": [.1, .1, .02, .1, .05, -.01],
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


def test_core_must_have_risen_on_the_reference_day():
    falling = _prev().with_columns(
        pl.when(pl.col("symbol") == CORE).then(-.03).otherwise(pl.col("change_pct")).alias("change_pct"))
    items = bw.select_bellwethers(falling, _mainlines(), _members(), excluded=frozenset({ST_STOCK}))
    # 成交额最大的 CORE 昨日下跌, 中军顺延到红盘中成交额最大的 LEADER
    assert _roles(items, CORE) == [] and _roles(items, LEADER) == ["leader", "core"]
    no_change = _prev().drop("change_pct")
    items = bw.select_bellwethers(no_change, _mainlines(), _members())
    assert not any(role["key"] == "core" for item in items for role in item["roles"])


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
    ({"emotion": "neutral", "leader": "neutral", "core": "unknown", "feedback": "unknown"}, "neutral"),
    ({"emotion": "strong", "leader": "neutral", "core": "neutral", "feedback": "unknown"}, "neutral"),
    ({"emotion": "strong", "leader": "unknown", "core": "unknown", "feedback": "unknown"}, "unknown"),
])
def test_verdict_follows_the_weakest_signals(signals, key):
    assert bw.verdict(signals)["key"] == key


def test_one_stock_with_two_roles_counts_once():
    both = [{"symbol": DRAGON, "roles": [{"key": "leader"}, {"key": "core"}], "signal": "strong"}]
    assert bw.role_signals(both, ("leader", "core")) == {"leader": "strong", "core": "unknown"}
    # 只靠一只股票不能凑出"两类走强"的进攻
    assert bw.verdict(bw.role_signals(both, ("leader", "core")) | {"breadth": "unknown"})["key"] == "unknown"
    shared = [*both, {"symbol": LEADER, "roles": [{"key": "core"}], "signal": "weak"}]
    assert bw.role_signals(shared, ("leader", "core")) == {"leader": "strong", "core": "weak"}


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


# ================================================================ 持仓 / 自选所属板块
MINE, R1, R2, C1, C2, X1, NOMAP = (
    "600101.SH", "600102.SH", "600103.SH", "600104.SH", "600105.SH", "600106.SH", "600107.SH")
WIDE = [f"60020{i}.SH" for i in range(6)]
FILTER = {"min_members": 1, "max_members": 5, "blacklist": []}


def _user_prev() -> pl.DataFrame:
    rows = {MINE: (0, 1e8, .01), R1: (2, 5e8, .10), R2: (0, 20e8, .03), C1: (0, 2e8, .02),
            C2: (0, 1e8, .05), X1: (1, 3e8, .10), NOMAP: (0, 1e8, 0.)} | {w: (0, 1e8, 0.) for w in WIDE}
    return pl.DataFrame({
        "symbol": list(rows), "date": [REFERENCE] * len(rows),
        "consecutive_limit_ups": [v[0] for v in rows.values()],
        "amount": [v[1] for v in rows.values()], "change_pct": [v[2] for v in rows.values()],
        "signal_limit_up": [v[0] >= 1 for v in rows.values()],
        "signal_broken_limit_up": [False] * len(rows),
    })


def _raw_maps() -> dict[str, pl.DataFrame]:
    concept = ([(s, "机器人") for s in (MINE, R1, R2, "600101")]
               + [(s, "冷门") for s in (MINE, C1, C2)] + [(s, "第三") for s in (MINE, X1)]
               + [(s, "宽基") for s in [MINE, *WIDE]])
    industry = [(MINE, "电子-半导体-数字芯片"), (C1, "银行-国有大型银行-国有银行"),
                ("600104", "银行-国有大型银行-国有银行")]
    return {kind: pl.DataFrame({"_sym_up": [p[0] for p in pairs], kind: [p[1] for p in pairs]})
            for kind, pairs in (("concept", concept), ("industry", industry))}


def _maps() -> dict[str, pl.DataFrame]:
    return {kind: bw.sector_map(frame, kind, FILTER) for kind, frame in _raw_maps().items()}


RANKS = {("concept", "机器人"): 1}


def _by_name(sectors):
    return {sector["name"]: sector for sector in sectors}


def _roles_of(sector):
    return [(item["symbol"], [role["key"] for role in item["roles"]]) for item in sector["items"]]


def test_sector_map_filters_broad_tags_and_truncates_industry():
    maps = _maps()
    assert "宽基" not in maps["concept"]["sector"].to_list()
    assert sorted(maps["industry"]["sector"].unique().to_list()) == ["电子-半导体", "银行-国有大型银行"]


def test_pick_user_sectors_ranks_by_mainline_then_limit_ups_and_caps_per_stock():
    sectors, uncovered = bw.pick_user_sectors([MINE, C1, NOMAP, MINE.lower()], _user_prev(), _maps(), RANKS)
    assert [s["name"] for s in sectors] == ["机器人", "第三", "冷门", "银行-国有大型银行", "电子-半导体"]
    assert uncovered == [NOMAP]
    named = _by_name(sectors)
    # 代码别名不重复计成员
    assert named["机器人"]["member_count"] == 3 and named["机器人"]["mainline_rank"] == 1
    assert named["冷门"]["mine"] == [MINE, C1]
    assert _roles_of(named["机器人"]) == [(R1, ["leader"]), (R2, ["core"])]
    assert named["机器人"]["items"][0]["roles"][0]["detail"] == "昨日 2 板"
    # 没有涨停的板块只给弱龙头, 不参与结论
    assert _roles_of(named["冷门"]) == [(C2, ["weak_leader"]), (C1, ["core"])]
    assert _roles_of(named["银行-国有大型银行"]) == [(C1, ["weak_leader", "core"])]
    assert _roles_of(named["第三"]) == [(X1, ["leader", "core"])]


def test_pick_user_sectors_total_cap_reports_uncovered_and_excludes_st_from_leaders(monkeypatch):
    monkeypatch.setattr(bw, "MAX_USER_SECTORS", 1)
    sectors, uncovered = bw.pick_user_sectors([MINE, C1, NOMAP], _user_prev(), _maps(), RANKS)
    assert [s["name"] for s in sectors] == ["机器人"] and uncovered == [C1, NOMAP]
    monkeypatch.setattr(bw, "MAX_USER_SECTORS", 8)
    sectors, _ = bw.pick_user_sectors([MINE], _user_prev(), _maps(), RANKS, excluded=frozenset({R1}))
    robot = _by_name(sectors)["机器人"]
    assert robot["items"][0]["roles"][0]["key"] == "weak_leader" and robot["member_count"] == 2


@pytest.mark.parametrize("perf,signal", [
    ({"count": 9, "avg_pct": .012, "up_ratio": .65, "coverage": .9}, "strong"),
    ({"count": 9, "avg_pct": -.001, "up_ratio": .7, "coverage": .9}, "weak"),
    ({"count": 9, "avg_pct": .005, "up_ratio": .5, "coverage": .9}, "neutral"),
    ({"count": 9, "avg_pct": .05, "up_ratio": .9, "coverage": .7}, "unknown"),
    (None, "unknown"),
])
def test_breadth_signal_requires_coverage(perf, signal):
    assert bw.breadth_signal(perf) == signal


@pytest.mark.parametrize("change,avg,relative", [
    (.03, .01, "stronger"), (-.01, .01, "weaker"), (.015, .01, "inline"),
    (None, .01, "unknown"), (.01, None, "unknown"),
])
def test_relative_strength(change, avg, relative):
    assert bw.relative_strength(change, avg) == relative


@pytest.fixture
def user_env(env, monkeypatch):
    env.prev = _user_prev()
    env.current = pl.DataFrame({
        "symbol": [MINE, R1, R2, C1, C2, X1, NOMAP],
        "name": ["我的股", "机器人龙", "机器人中军", "银行股", "冷门股", "第三龙", "无归属"],
        "change_pct": [.02, .1, -.04, -.01, 0., .05, 0.],
        "consecutive_limit_ups": [0, 3, 0, 0, 0, 0, 0],
        "signal_limit_up": [False, True, False, False, False, False, False],
        "signal_broken_limit_up": [False] * 7, "signal_limit_down": [False] * 7,
    }).with_columns(pl.lit(_ms(NOON) - 5_000, dtype=pl.Int64).alias("quote_ts"))
    raw = _raw_maps()
    monkeypatch.setattr(bw, "_load_concept_map_df", lambda repo, kind: (raw[kind], 0))
    monkeypatch.setattr(bw, "_filter_config", lambda: FILTER)
    monkeypatch.setattr(bw, "_excluded_symbols", lambda data_dir: frozenset())
    monkeypatch.setattr(bw, "load_mainlines", lambda data_dir, **kw: {"status": "ready", "items": [
        {"kind": "concept", "name": "机器人", "rank": 1}]})
    return env


def test_user_snapshot_tracks_my_sectors(user_env):
    snap = bw.build_user_snapshot(user_env.repo, "holdings", [MINE, C1, NOMAP], now=NOON)
    assert snap["status"] == "ready" and snap["scope"] == "holdings" and snap["total"] == 3
    assert snap["uncovered"] == [{"symbol": NOMAP, "name": "无归属"}]
    robot = _by_name(snap["sectors"])["机器人"]
    # 活跃成员只有 R1、X1 不足 MIN_ACTIVE, 退回全体成员
    assert robot["breadth"] == {"count": 3, "avg_pct": pytest.approx(.08 / 3), "up_ratio": pytest.approx(2 / 3),
                                "coverage": pytest.approx(1.0), "limit_up": 1, "basis": "all", "size": 3}
    assert [(i["name"], i["state"]) for i in robot["items"]] == [("机器人龙", "sealed"), ("机器人中军", "weak")]
    assert robot["verdict"]["key"] == "divergence" and robot["verdict"]["signals"]["breadth"] == "strong"
    assert robot["mine"] == [{"symbol": MINE, "name": "我的股", "change_pct": pytest.approx(.02),
                              "state": "flat", "relative": "inline"}]
    cold = _by_name(snap["sectors"])["冷门"]
    # 弱龙头不计入结论
    assert cold["verdict"]["signals"]["leader"] == "unknown" and cold["verdict"]["signals"]["breadth"] == "weak"


def test_breadth_uses_recently_active_members(user_env, monkeypatch):
    monkeypatch.setattr(bw, "MIN_ACTIVE", 1)
    snap = bw.build_user_snapshot(user_env.repo, "holdings", [MINE], now=NOON)
    robot = _by_name(snap["sectors"])["机器人"]
    assert robot["breadth"]["basis"] == "active" and robot["breadth"]["size"] == 1
    assert robot["breadth"]["avg_pct"] == pytest.approx(.1)
    # 本股相对活跃成员均涨(+10%)明显跑输
    assert robot["mine"][0]["relative"] == "weaker"


def test_breadth_falls_back_when_history_is_missing():
    current = pl.DataFrame({"symbol": [MINE, R1], "change_pct": [.01, .03]})
    assert bw.sector_breadth(current, [MINE, R1], None)["basis"] == "all"
    assert bw.sector_breadth(current, [MINE, R1], set())["basis"] == "all"


def test_user_snapshot_stale_and_fail_closed(user_env):
    user_env.current = user_env.current.with_columns(
        pl.lit(_ms(NOON) - 300_000, dtype=pl.Int64).alias("quote_ts"))
    snap = bw.build_user_snapshot(user_env.repo, "watchlist", [MINE], now=NOON)
    assert snap["stale"] and all(s["verdict"]["key"] == "unknown" for s in snap["sectors"])
    none = bw.build_user_snapshot(user_env.repo, "watchlist", [NOMAP], now=NOON)
    assert none["status"] == "unavailable" and "板块归属" in none["reason"]
    user_env.prev = None
    gone = bw.build_user_snapshot(user_env.repo, "watchlist", [MINE], now=NOON)
    assert gone["status"] == "unavailable" and gone["scope"] == "watchlist" and gone["sectors"] == []


def test_load_user_symbols_from_lots_and_watchlist_groups(tmp_path, monkeypatch):
    assert bw.load_user_symbols(tmp_path, "holdings") == ([], "还没有批次提醒持仓")
    lots_dir = tmp_path / "user_data" / "lots"
    lots_dir.mkdir(parents=True)
    for lot_id, symbol in (("a1", "600101.sh"), ("a2", "600101.SH"), ("a3", "600104.SH")):
        (lots_dir / f"lot_{lot_id}.json").write_text(
            f'{{"id": "{lot_id}", "symbol": "{symbol}", "qty": 100, "cost_price": 10, "stop_pct": 5}}',
            encoding="utf-8")
    assert bw.load_user_symbols(tmp_path, "holdings") == ([MINE, C1], None)

    from app.services import watchlist
    monkeypatch.setattr(watchlist, "list_symbols", lambda: [
        {"symbol": MINE, "group_ids": ["g1"]}, {"symbol": C1, "group_ids": []}])
    monkeypatch.setattr(watchlist, "list_groups", lambda: [{"id": "g1", "name": "核心"}, {"id": "g2", "name": "空"}])
    assert bw.load_user_symbols(tmp_path, "watchlist") == ([MINE, C1], None)
    assert bw.load_user_symbols(tmp_path, "watchlist", "g1") == ([MINE], None)
    assert bw.load_user_symbols(tmp_path, "watchlist", "g2") == ([], "该自选分组没有股票")
    assert bw.load_user_symbols(tmp_path, "watchlist", "gx") == ([], "自选分组不存在")


def test_api_scope_parameter(user_env, monkeypatch):
    from app.api.market_recap import router

    monkeypatch.setattr(bw, "load_user_symbols", lambda data_dir, scope, group_id=None: ([MINE], None))
    real = bw.build_user_snapshot
    monkeypatch.setattr(bw, "build_user_snapshot", lambda *a, **kw: real(*a, **{**kw, "now": NOON}))
    bw.invalidate_cache()
    app = FastAPI()
    app.include_router(router)
    app.state.repo = user_env.repo
    client = TestClient(app)
    body = client.get("/api/market-recap/bellwethers", params={"scope": "watchlist", "group_id": "g1"}).json()
    assert body["scope"] == "watchlist" and body["sectors"][0]["name"] == "机器人"
    assert client.get("/api/market-recap/bellwethers", params={"scope": "nope"}).status_code == 422
    bw.invalidate_cache()
