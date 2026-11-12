"""剧本闭环: 情景互斥校验、持仓条件次日核验、10:00 盘中核验。"""
from datetime import date, datetime
from types import SimpleNamespace

import polars as pl
import pytest

from app.market_time import CN_TZ
from app.services import market_recap
from app.services import market_recap_holdings as mh
from app.services import market_recap_intraday as mi
from app.services import market_recap_plan as mp


def _sc(name, conds, *, primary=False, match="all", action=""):
    return {"name": name, "primary": primary, "match": match, "action": action,
            "conditions": [{"metric": m, "op": op, "value": v, **({"symbol": s} if s else {})}
                           for m, op, v, s in [(*c, None)[:4] for c in conds]]}


# ---------------------------------------------------------------- 情景互斥
def test_disjoint_bands_on_main_axis_have_no_overlap():
    plan = mp.validate_plan({"scenarios": [
        _sc("强", [("amount_30m", ">=", 6000), ("limit_up", ">=", 90)]),
        _sc("符合", [("amount_30m", ">=", 4800), ("amount_30m", "<", 6000)], primary=True),
        _sc("弱", [("amount_30m", "<", 4800)]),
    ]})
    assert plan["overlaps"] == []


def test_overlaps_from_real_plan_shape():
    # 11-11 实测剧本: 强(≥6000且涨停≥90) / 符合(≥5000且最高板≥5) / 弱(<4800 或 涨停<70)
    plan = mp.validate_plan({"scenarios": [
        _sc("强", [("amount_30m", ">=", 6000), ("limit_up", ">=", 90)]),
        _sc("符合", [("amount_30m", ">=", 5000), ("max_boards", ">=", 5)], primary=True),
        _sc("弱", [("amount_30m", "<", 4800), ("limit_up", "<", 70)], match="any"),
    ]})
    assert plan["overlaps"] == [["强", "符合"], ["符合", "弱"]]


@pytest.mark.parametrize("a, b, overlap", [
    ((">=", 6000), ("<", 6000), False),   # 首尾相接: 边界只属于一方
    ((">=", 6000), ("<=", 6000), True),   # 边界两方都含
    ((">", 6000), ("<=", 6000), False),
    ((">", 6000), (">", 7000), True),
])
def test_boundary_semantics(a, b, overlap):
    plan = mp.validate_plan({"scenarios": [_sc("A", [("amount_30m", *a)]), _sc("B", [("amount_30m", *b)])]})
    assert bool(plan["overlaps"]) is overlap


def test_boards_are_keyed_by_symbol_and_contradictory_scenario_never_overlaps():
    plan = mp.validate_plan({"scenarios": [
        {"name": "A", "conditions": [{"metric": "boards", "symbol": "600001.SH", "op": ">=", "value": 5}]},
        {"name": "B", "conditions": [{"metric": "boards", "symbol": "600002.SH", "op": "<", "value": 5}]},
        {"name": "C", "conditions": [{"metric": "limit_up", "op": ">", "value": 90},
                                     {"metric": "limit_up", "op": "<", "value": 50}]},  # 自相矛盾
    ]})
    assert plan["overlaps"] == [["A", "B"]]  # 不同股票可同时成立; C 永不命中


def test_ambiguous_day_is_excluded_from_primary_hit_rate():
    plan = mp.validate_plan({"scenarios": [
        _sc("强", [("amount_30m", ">=", 5000)], action="加大参与"),
        _sc("符合", [("amount_30m", ">=", 4000)], primary=True),
    ]})
    snap = {"as_of": "2026-11-12", "metrics": [{"key": "amount_30m", "today": 5300.0}]}
    chk = mp.evaluate_plan(plan, snap, SimpleNamespace(), from_as_of="2026-11-11")
    assert chk["ambiguous"] is True and chk["overlaps"] == [["强", "符合"]]
    assert chk["scenarios"][0]["action"] == "加大参与"
    stats = mp.plan_stats([chk])
    assert stats["ambiguous"] == 1 and stats["primary_total"] == 0
    assert stats["covered"] == 1  # 覆盖率不受影响
    assert "不计入主判断命中率" in mp.plan_check_prompt_block(chk, stats)


# ---------------------------------------------------------------- 持仓条件次日核验
PREV_HOLDINGS = {"rows": [
    {"symbol": "600522.SH", "name": "中天科技", "conditions": [
        {"op": ">=", "value": 26.8, "label": "收盘 ≥ 26.80(压力)", "meaning": "站上压力, 联动走强"},
        {"op": "<=", "value": 22.1, "label": "收盘 ≤ 22.10(支撑)", "meaning": "跌破支撑, 联动失效"},
    ]},
    {"symbol": "000657.SZ", "name": "中钨高新", "conditions": []},  # 无条件不核验
    {"symbol": "600498.SH", "name": "烽火通信", "conditions": [
        {"op": "<=", "value": 28.0, "label": "收盘 ≤ 28.00(支撑)", "meaning": "跌破支撑"},
    ]},
]}


def _repo_closes(closes: dict | None):
    def getter(start, end, symbols=None, columns=None):
        if closes is None:
            return None
        return pl.DataFrame({"symbol": list(closes), "date": [start] * len(closes),
                             "close": list(closes.values()), "raw_close": list(closes.values())})
    return SimpleNamespace(get_enriched_range=getter)


def test_holding_conditions_triggered_not_and_missing_symbol_pending():
    snap = {"as_of": "2026-11-12", "intraday": False}
    chk = mh.check_holding_conditions(PREV_HOLDINGS, snap, _repo_closes({"600522.SH": 27.1}), from_as_of="2026-11-11")
    by = {r["symbol"]: r for r in chk["rows"]}
    assert set(by) == {"600522.SH", "600498.SH"}
    assert [c["status"] for c in by["600522.SH"]["conditions"]] == ["triggered", "not"]
    assert [c["status"] for c in by["600498.SH"]["conditions"]] == ["pending"]  # 当日无行情
    assert chk["triggered"] == 1
    text = mh.holdings_check_prompt_block(chk)
    assert "中天科技 收盘 ≥ 26.80(压力) → 站上压力, 联动走强(收盘 27.10)" in text
    assert "暂不能判定" in text


def test_holding_conditions_intraday_or_no_conditions():
    chk = mh.check_holding_conditions(PREV_HOLDINGS, {"as_of": "2026-11-12", "intraday": True},
                                      _repo_closes({"600522.SH": 27.1}), from_as_of="2026-11-11")
    assert chk["triggered"] == 0
    assert all(c["status"] == "pending" for r in chk["rows"] for c in r["conditions"])
    assert mh.check_holding_conditions({"rows": [{"symbol": "x", "conditions": []}]},
                                       {"as_of": "2026-11-12"}, None, from_as_of="d") is None
    assert mh.holdings_check_prompt_block(None) == ""


def test_attach_holdings_check_only_for_adjacent_previous_report():
    reports = [{"as_of": "2026-11-11", "created_at": "x", "content": "正文",
                "snapshot": {"holdings": PREV_HOLDINGS}}]
    snap = {"as_of": "2026-11-12", "intraday": False, "dates": ["2026-11-11", "2026-11-12"]}
    market_recap.attach_holdings_check(snap, _repo_closes({"600522.SH": 21.0}), reports)
    assert snap["holdings_check"]["triggered"] == 1

    gap = {"as_of": "2026-11-13", "intraday": False, "dates": ["2026-11-12", "2026-11-13"]}
    market_recap.attach_holdings_check(gap, _repo_closes({"600522.SH": 21.0}), reports)
    assert gap["holdings_check"] is None


# ---------------------------------------------------------------- 10:00 盘中核验
SESSIONS = [date(2026, 11, 10), date(2026, 11, 11), date(2026, 11, 12)]
NOW = datetime(2026, 11, 12, 10, 2, tzinfo=CN_TZ)
PLAN = mp.validate_plan({"scenarios": [
    _sc("强于预期", [("amount_30m", ">=", 6000), ("limit_up", ">=", 90)], action="主线核心加大参与"),
    _sc("符合预期", [("amount_30m", ">=", 4800), ("amount_30m", "<", 6000)], primary=True, action="只做分歧转一致"),
    _sc("弱于预期", [("amount_30m", "<", 4800)], action="降低参与度"),
]})
REPORTS = [
    {"as_of": "2026-11-11", "created_at": "2026-11-11T20:00:00", "plan": PLAN,
     "snapshot": {"metrics": [{"key": "amount_30m", "today": 5000.0}]}},
    {"as_of": "2026-11-11", "created_at": "2026-11-11T11:00:00", "plan": None},
]


@pytest.fixture
def intraday_env(monkeypatch, tmp_path):
    monkeypatch.setattr(mi, "_sessions", lambda today: SESSIONS)
    monkeypatch.setattr(mi, "_traded_count", lambda data_dir, day: 5000)
    state = {"amount": 5341.0}
    monkeypatch.setattr(mp, "opening_amount_yi", lambda data_dir, day, n: state["amount"])
    return SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)), state


def test_intraday_check_leans_to_scenario_matching_opening_turnover(intraday_env):
    repo, _ = intraday_env
    r = mi.run_intraday_check(repo, now=NOW, reports=REPORTS)
    assert r["status"] == "ok" and r["from_as_of"] == "2026-11-11"
    assert r["leaning"] == [{"name": "符合预期", "primary": True, "action": "只做分歧转一致"}]
    # 全天条件(涨停家数)盘中保持待定, 不把"强于预期"判为落空以外的结论
    strong = r["check"]["scenarios"][0]
    assert strong["status"] == "miss"  # 成交额已不满足
    text = mi.format_push(r)
    assert "开盘半小时成交额 5,341 亿(较上一交易日同时段 +7%)" in text
    assert "→ 倾向「符合预期(主判断)」: 只做分歧转一致" in text


def test_intraday_check_pending_full_day_condition_still_leans(intraday_env):
    repo, state = intraday_env
    state["amount"] = 6500.0
    r = mi.run_intraday_check(repo, now=NOW, reports=REPORTS)
    # 强于预期: 成交额成立, 涨停家数待收盘 → 仍列为倾向
    assert [s["name"] for s in r["leaning"]] == ["强于预期"]


def test_intraday_check_statuses(intraday_env, monkeypatch):
    repo, state = intraday_env
    assert mi.run_intraday_check(repo, now=NOW.replace(hour=9, minute=55), reports=REPORTS)["status"] == "too_early"
    assert mi.run_intraday_check(repo, now=NOW, reports=[])["status"] == "no_plan"
    # 上一交易日之前的剧本不针对今天
    old = [{**REPORTS[0], "as_of": "2026-11-10"}]
    assert mi.run_intraday_check(repo, now=NOW, reports=old)["status"] == "no_plan"
    state["amount"] = None
    r = mi.run_intraday_check(repo, now=NOW, reports=REPORTS)
    assert r["status"] == "data_incomplete" and r["leaning"] == []
    assert "分钟数据不完整" in mi.format_push(r)
    monkeypatch.setattr(mi, "_sessions", lambda today: SESSIONS[:-1])
    assert mi.run_intraday_check(repo, now=NOW, reports=REPORTS)["status"] == "not_trading"


def test_run_and_record_pushes_only_when_asked(intraday_env, monkeypatch):
    repo, _ = intraday_env
    saved, pushed = [], []
    monkeypatch.setattr("app.services.market_recap_reports.list_reports", lambda: REPORTS)
    monkeypatch.setattr(mi._store, "save_report", lambda r: saved.append(r) or {**r, "id": "mri_1"})
    monkeypatch.setattr("app.jobs.daily_pipeline._maybe_push_review",
                        lambda content, meta, **kw: pushed.append((content, kw)))
    mi.run_and_record(repo, push=False, now=NOW)
    assert len(saved) == 1 and pushed == []
    mi.run_and_record(repo, push=True, now=NOW)
    assert len(pushed) == 1 and pushed[0][1] == {"title": "盘中剧本核验", "event": "market_review_intraday"}

    monkeypatch.setattr("app.services.market_recap_reports.list_reports", lambda: [])
    mi.run_and_record(repo, push=True, now=NOW)  # 无剧本: 归档但不推送
    assert len(pushed) == 1 and saved[-1]["status"] == "no_plan"


def test_settings_toggle_registers_and_removes_job(monkeypatch, tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.settings import router
    from app.services import preferences

    monkeypatch.setattr(preferences, "_path", lambda: tmp_path / "preferences.json")
    preferences._invalidate_cache()
    jobs: dict = {}
    scheduler = SimpleNamespace(add_job=lambda fn, **kw: jobs.__setitem__(kw["id"], kw),
                                remove_job=lambda job_id: jobs.pop(job_id))
    app = FastAPI()
    app.include_router(router)
    app.state.scheduler = scheduler
    app.state.repo = object()
    client = TestClient(app)

    assert client.put("/api/settings/preferences/intraday-plan-check", json={"enabled": True}).json() == {"enabled": True}
    assert jobs[mi.JOB_ID]["trigger"].fields[5].expressions[0].first == 10  # hour
    assert preferences.get_intraday_plan_check_enabled() is True
    client.put("/api/settings/preferences/intraday-plan-check", json={"enabled": False})
    assert mi.JOB_ID not in jobs and preferences.get_intraday_plan_check_enabled() is False
    preferences._invalidate_cache()
