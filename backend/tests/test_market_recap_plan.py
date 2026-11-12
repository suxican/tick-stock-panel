"""明日剧本结构化 + 次日系统核验 + 开盘半小时成交额。"""
import json
from datetime import date, datetime
from types import SimpleNamespace

import polars as pl
import pytest

from app.services import market_recap
from app.services import market_recap_plan as mp

PLAN_JSON = {
    "scenarios": [
        {"name": "强于预期", "primary": False, "conditions": [
            {"metric": "amount_30m", "op": ">=", "value": 6000},
            {"metric": "limit_up", "op": ">=", "value": 90},
        ]},
        {"name": "符合预期", "primary": True, "conditions": [
            {"metric": "boards", "symbol": "603366.sh", "name": "日出东方", "op": ">=", "value": 5},
        ]},
        {"name": "弱于预期", "primary": True, "conditions": [
            {"metric": "amount_30m", "op": "<", "value": 4500},
        ]},
    ],
}

CONTENT = (
    "## 🗺️ 明日剧本\n| 情景 | 触发条件 | 应对 |\n\n"
    "> ⚠️ 本内容由 AI 生成\n\n```plan\n" + json.dumps(PLAN_JSON, ensure_ascii=False) + "\n```\n"
)


# ---------------------------------------------------------------- 解析
def test_split_plan_block_strips_block_and_validates():
    clean, plan = mp.split_plan_block(CONTENT)
    assert "```plan" not in clean and "免" not in clean.split("⚠️")[0]
    assert clean.rstrip().endswith("本内容由 AI 生成")
    names = [s["name"] for s in plan["scenarios"]]
    assert names == ["强于预期", "符合预期", "弱于预期"]
    # 只保留第一个 primary; symbol 统一大写
    assert [s["primary"] for s in plan["scenarios"]] == [False, True, False]
    assert plan["scenarios"][1]["conditions"][0]["symbol"] == "603366.SH"


def test_split_plan_block_handles_missing_bad_and_unclosed_blocks():
    assert mp.split_plan_block("正文") == ("正文", None)
    clean, plan = mp.split_plan_block("正文\n```plan\n{坏的 json\n```")
    assert plan is None and "```plan" not in clean
    # 流被截断、代码块没闭合: 仍剥离
    clean, plan = mp.split_plan_block("正文\n```plan\n{\"scenarios\": [")
    assert clean.strip() == "正文" and plan is None


def test_validate_plan_drops_unknown_metrics_ops_and_bad_symbols():
    plan = mp.validate_plan({"scenarios": [
        {"name": "A", "conditions": [
            {"metric": "vol_ratio", "op": ">", "value": 1},       # 不在词表
            {"metric": "limit_up", "op": "==", "value": 80},      # 非法运算符
            {"metric": "limit_up", "op": ">", "value": "80"},     # 非数字
            {"metric": "boards", "op": ">=", "value": 5},         # 缺 symbol
        ]},
        {"name": "B", "conditions": [{"metric": "limit_down", "op": "<=", "value": 5}]},
    ]})
    assert [s["name"] for s in plan["scenarios"]] == ["B"]
    assert mp.validate_plan({"scenarios": []}) is None
    assert mp.validate_plan("x") is None


# ---------------------------------------------------------------- 核验
def _snapshot(intraday=False, amount_30m=5341.0, limit_up=93.0):
    return {
        "as_of": "2026-11-12",
        "intraday": intraday,
        "dates": ["2026-11-11", "2026-11-12"],
        "metrics": [
            {"key": "amount_30m", "today": amount_30m, "avg5": 6000.0},
            {"key": "limit_up", "today": limit_up, "avg5": 77.0},
            {"key": "amount_yi", "today": 17000.0, "avg5": 16000.0},
        ],
        "ladder": [{"boards": 5, "count": 1, "stocks": [{"symbol": "603366.SH", "name": "日出东方"}]}],
    }


def _repo(boards: dict | None):
    def get_enriched_range(start, end, symbols=None, columns=None):
        if boards is None:
            return None
        return pl.DataFrame({"symbol": list(boards), "date": [start] * len(boards),
                             "consecutive_limit_ups": list(boards.values())})
    return SimpleNamespace(get_enriched_range=get_enriched_range)


def test_evaluate_plan_marks_hit_miss_and_pending():
    _, plan = mp.split_plan_block(CONTENT)
    chk = mp.evaluate_plan(plan, _snapshot(), _repo({"603366.SH": 5}), from_as_of="2026-11-11")
    by = {s["name"]: s for s in chk["scenarios"]}
    # 强于预期: 30m 5341 < 6000 → 落空 (即便涨停满足)
    assert by["强于预期"]["status"] == "miss"
    assert [c["status"] for c in by["强于预期"]["conditions"]] == ["miss", "hit"]
    assert by["强于预期"]["conditions"][0]["label"] == "开盘半小时成交额 ≥ 6000亿"
    assert by["强于预期"]["conditions"][0]["actual_text"] == "5341亿"
    assert by["符合预期"]["status"] == "hit"
    assert by["符合预期"]["conditions"][0]["label"] == "日出东方连板数 ≥ 5板"
    assert by["弱于预期"]["status"] == "miss"


def test_match_any_scenario_hits_when_one_condition_holds():
    plan = mp.validate_plan({"scenarios": [{"name": "弱于预期", "primary": True, "match": "any", "conditions": [
        {"metric": "amount_30m", "op": "<", "value": 4800},
        {"metric": "limit_up", "op": ">=", "value": 90},
    ]}]})
    assert plan["scenarios"][0]["match"] == "any"
    chk = mp.evaluate_plan(plan, _snapshot(), _repo(None), from_as_of="2026-11-11")
    assert chk["scenarios"][0]["status"] == "hit"  # 30m 不满足, 涨停 93 满足
    assert " 或 " in mp.plan_check_prompt_block(chk, None)

    # 全部不成立 → 落空; 有待定且无成立 → 待定
    none_hit = mp.evaluate_plan(plan, _snapshot(limit_up=50.0), _repo(None), from_as_of="2026-11-11")
    assert none_hit["scenarios"][0]["status"] == "miss"
    pending = mp.evaluate_plan(plan, _snapshot(amount_30m=None, limit_up=50.0), _repo(None), from_as_of="2026-11-11")
    assert pending["scenarios"][0]["status"] == "pending"
    # 未声明 match 时默认 all
    assert mp.validate_plan({"scenarios": [{"conditions": [{"metric": "limit_up", "op": ">", "value": 1}]}]})["scenarios"][0]["match"] == "all"


def test_evaluate_plan_missing_data_is_pending_not_miss():
    _, plan = mp.split_plan_block(CONTENT)
    chk = mp.evaluate_plan(plan, _snapshot(amount_30m=None), _repo(None), from_as_of="2026-11-11")
    by = {s["name"]: s for s in chk["scenarios"]}
    # 30m 缺失 → 待定; 但涨停 93 ≥ 90 成立, 仍为待定而非命中
    assert by["强于预期"]["status"] == "pending"
    assert by["符合预期"]["status"] == "pending"  # enriched 不覆盖
    assert by["弱于预期"]["status"] == "pending"


def test_evaluate_plan_intraday_keeps_full_day_metrics_pending():
    _, plan = mp.split_plan_block(CONTENT)
    chk = mp.evaluate_plan(plan, _snapshot(intraday=True), _repo({"603366.SH": 5}), from_as_of="2026-11-11")
    strong = chk["scenarios"][0]
    assert [c["status"] for c in strong["conditions"]] == ["miss", "pending"]
    assert chk["scenarios"][1]["status"] == "pending"


def test_plan_stats_counts_only_settled_checks():
    checks = [
        {"scenarios": [{"primary": True, "status": "hit"}, {"primary": False, "status": "miss"}]},
        {"scenarios": [{"primary": True, "status": "miss"}, {"primary": False, "status": "hit"}]},
        {"scenarios": [{"primary": True, "status": "miss"}, {"primary": False, "status": "miss"}]},
        {"scenarios": [{"primary": True, "status": "pending"}, {"primary": False, "status": "miss"}]},
    ]
    assert mp.plan_stats(checks) == {
        "primary_total": 3, "primary_hits": 1, "covered_total": 3, "covered": 2, "ambiguous": 0,
    }


def test_archived_checks_uses_latest_report_per_day_before_as_of():
    reports = [
        {"as_of": "2026-11-10", "created_at": "2026-11-10T11:00:00", "snapshot": {"plan_check": {"id": "盘中"}}},
        {"as_of": "2026-11-10", "created_at": "2026-11-10T20:00:00", "snapshot": {"plan_check": {"id": "盘后"}}},
        {"as_of": "2026-11-09", "created_at": "2026-11-09T20:00:00", "snapshot": None},
        {"as_of": "2026-11-12", "created_at": "2026-11-12T20:00:00", "snapshot": {"plan_check": {"id": "未来"}}},
    ]
    assert mp.archived_checks(reports, "2026-11-12") == [{"id": "盘后"}]


# ---------------------------------------------------------------- 复盘流程接入
def test_attach_plan_check_only_for_adjacent_previous_day():
    _, plan = mp.split_plan_block(CONTENT)
    prev = {"as_of": "2026-11-11", "created_at": "x", "content": "正文", "plan": plan,
            "snapshot": {"plan_check": {"scenarios": [{"primary": True, "status": "hit"}]}}}
    snap = _snapshot()
    market_recap.attach_plan_check(snap, _repo({"603366.SH": 5}), [prev])
    assert snap["plan_check"]["from_as_of"] == "2026-11-11"
    # 历史 1 次(命中) + 本次(主判断"符合预期"命中)
    assert snap["plan_stats"]["primary_total"] == 2 and snap["plan_stats"]["primary_hits"] == 2

    gap = {**prev, "as_of": "2026-11-10"}  # 中间缺一天复盘: 剧本不针对复盘日
    snap2 = _snapshot()
    market_recap.attach_plan_check(snap2, _repo({"603366.SH": 5}), [gap])
    assert snap2["plan_check"] is None


def test_prompt_uses_system_check_instead_of_plan_text_and_lists_vocabulary():
    _, plan = mp.split_plan_block(CONTENT)
    snap = _snapshot()
    chk = mp.evaluate_plan(plan, snap, _repo({"603366.SH": 5}), from_as_of="2026-11-11")
    check_text = mp.plan_check_prompt_block(chk, {"primary_total": 2, "primary_hits": 1})
    vocab = mp.plan_vocabulary_block(snap)
    prompt = market_recap._build_user_prompt(
        {"as_of": "2026-11-12"}, [], "", prev_plan={"as_of": "2026-11-11", "plan": "旧剧本文本"},
        plan_check_context=check_text, plan_vocab_context=vocab,
    )
    assert "不得改判" in prompt and "符合预期(主判断): ✅命中" in prompt
    assert "历史主判断命中 1/2" in prompt
    assert "旧剧本文本" not in prompt
    assert "| amount_30m | 开盘半小时成交额(亿) | 5341亿 | 6000亿 |" in prompt
    assert "日出东方=603366.SH(5板)" in prompt


# ---------------------------------------------------------------- 开盘半小时成交额
def _write_minutes(data_dir, day: str, symbols: int, minutes: int = 31, amount: float = 1e8):
    rows = []
    start = datetime.fromisoformat(f"{day}T09:30:00")
    for m in range(minutes):
        ts = start.replace(minute=30 + m) if m < 30 else start.replace(hour=10, minute=m - 30)
        for s in range(symbols):
            rows.append({"symbol": f"{s:06d}.SZ", "datetime": ts, "amount": amount})
    # 10:00 之后的分钟不计入
    rows.append({"symbol": "000000.SZ", "datetime": start.replace(hour=10, minute=1), "amount": 9e12})
    p = data_dir / "kline_minute" / f"date={day}" / "part.parquet"
    p.parent.mkdir(parents=True)
    pl.DataFrame(rows).write_parquet(p)


def test_opening_amount_sums_0930_to_1000(tmp_path):
    _write_minutes(tmp_path, "2026-11-10", symbols=100)
    assert mp.opening_amount_yi(tmp_path, "2026-11-10", 100) == pytest.approx(31 * 100 * 1e8 / 1e8)


def test_opening_amount_rejects_partial_universe_and_missing_minutes(tmp_path):
    # 分区只同步了部分标的(实测 1995/5573): 相对当日成交个股数不足 90%
    _write_minutes(tmp_path, "2026-11-03", symbols=50)
    assert mp.opening_amount_yi(tmp_path, "2026-11-03", 100) is None
    # 分钟不全(盘中同步到 9:48)
    _write_minutes(tmp_path, "2026-11-11", symbols=100, minutes=19)
    assert mp.opening_amount_yi(tmp_path, "2026-11-11", 100) is None
    assert mp.opening_amount_yi(tmp_path, "2026-11-12", 100) is None  # 无分区
    assert mp.opening_amount_yi(tmp_path, "2026-11-10", 0) is None
