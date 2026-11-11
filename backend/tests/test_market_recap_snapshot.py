"""AI 复盘数据快照 + 新版复盘 prompt 的上下文装配。"""
import json
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import polars as pl
import pytest

from app.services import market_recap, market_recap_snapshot as snap
from app.services.market_mainline import mainline_path
from app.services.regime_builder import regime_path

DAYS = [date(2026, 11, 2) + timedelta(days=i) for i in range(10) if (date(2026, 11, 2) + timedelta(days=i)).weekday() < 5]
AS_OF = DAYS[-1]  # 2026-11-11 (周三)


def _overview(as_of=AS_OF) -> dict:
    return {
        "as_of": str(as_of),
        "indices": [],
        "emotion": {"score": 71, "label": "强势"},
        "radar": [{"label": "量能", "value": 31}, {"label": "赚钱", "value": 75}],
        "breadth": {"up": 3538, "down": 1827, "flat": 20, "up_pct": 65.6},
        "amount": {"total": 1.69e12},
        "activity": {"avg_turnover": 3.1, "vol_ratio": 0.91},
        "limit": {
            "limit_up": 85, "broken": 25, "limit_down": 1, "seal_rate": 77.0, "max_boards": 4,
            "tiers": [
                {"boards": 4, "count": 2, "stocks": [{"symbol": "000001.SZ", "name": "龙一"}, {"symbol": "000002.SZ", "name": "龙二"}]},
                {"boards": 1, "count": 66, "stocks": []},
            ],
        },
        "concept_rank": {"leading": [{"name": "CPO", "avg_pct": 0.0464, "leader": {"name": "光一"}}], "lagging": []},
        "industry_rank": {"leading": [], "lagging": [{"name": "白酒", "avg_pct": -0.0308, "leader": {"name": "酒一"}}]},
    }


def _write_regime(data_dir, days, phases=None):
    n = len(days)
    df = pl.DataFrame({
        "date": days,
        "state": ["strong"] * n,
        "score": [70] * n,
        "phase": phases or ["repair"] * n,
        "total_amount": [1.5e12 + i * 1e10 for i in range(n)],
        "up_count": [3000 + i for i in range(n)],
        "down_count": [2000] * n,
        "limit_up": [60 + i for i in range(n)],
        "limit_down": [2] * n,
        "broken_limit": [20] * n,
        "max_consecutive": [5] * n,
        "seal_rate": [0.75] * n,
        "promo_rate": [0.25] * n,
    })
    p = regime_path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    df.write_parquet(p)


def _repo(data_dir, names=None):
    return SimpleNamespace(
        store=SimpleNamespace(data_dir=data_dir),
        get_name_map=lambda syms: {s: (names or {}).get(s, s) for s in syms},
    )


AFTER_CLOSE = datetime(2026, 11, 11, 20, 0)


# ---------------------------------------------------------------- 逐日序列与指标
def test_metrics_compare_today_prev_and_prior_5day_average(tmp_path):
    _write_regime(tmp_path, DAYS)
    s = snap.build_recap_snapshot(_repo(tmp_path), _overview(), now=AFTER_CLOSE)

    assert s["today_source"] == "regime"
    assert s["dates"][-1] == AS_OF.isoformat()
    amount = next(m for m in s["metrics"] if m["key"] == "amount_yi")
    n = len(DAYS)
    assert amount["today"] == pytest.approx((1.5e12 + (n - 1) * 1e10) / 1e8)
    assert amount["prev"] == pytest.approx((1.5e12 + (n - 2) * 1e10) / 1e8)
    # 前 5 日均不含复盘日
    expected_avg = sum(1.5e12 + i * 1e10 for i in range(n - 6, n - 1)) / 5 / 1e8
    assert amount["avg5"] == pytest.approx(expected_avg)
    seal = next(m for m in s["metrics"] if m["key"] == "seal_rate")
    assert seal["today"] == pytest.approx(75.0)  # 小数 → 百分数


def test_today_missing_in_regime_falls_back_to_overview_row(tmp_path):
    _write_regime(tmp_path, DAYS[:-1])
    s = snap.build_recap_snapshot(_repo(tmp_path), _overview(), now=AFTER_CLOSE)

    assert s["today_source"] == "overview"
    assert s["dates"][-1] == AS_OF.isoformat()
    limit_up = next(m for m in s["metrics"] if m["key"] == "limit_up")
    assert limit_up["today"] == 85
    assert limit_up["prev"] == 60 + len(DAYS) - 2
    promo = next(m for m in s["metrics"] if m["key"] == "promo_rate")
    assert promo["today"] is None  # 总览无法给出晋级率
    # 周期取最近一个有标签的交易日并标注日期
    assert s["phase"]["date"] == DAYS[-2].isoformat()


def test_history_after_as_of_is_ignored(tmp_path):
    _write_regime(tmp_path, DAYS)
    s = snap.build_recap_snapshot(_repo(tmp_path), _overview(DAYS[-3]), now=AFTER_CLOSE)
    assert s["dates"][-1] == DAYS[-3].isoformat()


def test_phase_streak_counts_consecutive_same_phase(tmp_path):
    phases = ["repair"] * (len(DAYS) - 3) + ["ignite"] * 3
    _write_regime(tmp_path, DAYS, phases)
    s = snap.build_recap_snapshot(_repo(tmp_path), _overview(), now=AFTER_CLOSE)
    assert s["phase"]["label"] == "启动"
    assert s["phase"]["days"] == 3
    assert s["phase"]["capped"] is False


def test_no_history_still_builds_from_overview(tmp_path):
    s = snap.build_recap_snapshot(_repo(tmp_path), _overview(), now=AFTER_CLOSE)
    assert s["today_source"] == "overview"
    assert s["phase"] is None
    assert s["mainlines"] == []
    assert s["ladder"][0]["stocks"][0]["name"] == "龙一"


def test_intraday_flag_only_for_today_before_close():
    assert snap.is_intraday_snapshot(AS_OF, datetime(2026, 11, 11, 11, 30)) is True
    assert snap.is_intraday_snapshot(AS_OF, datetime(2026, 11, 11, 15, 1)) is False
    assert snap.is_intraday_snapshot(DAYS[-2], datetime(2026, 11, 11, 11, 30)) is False


# ---------------------------------------------------------------- 主线持续天数
def test_mainline_streak_counts_consecutive_top_rank_days(tmp_path):
    rows = []
    for i, d in enumerate(DAYS[-4:]):
        # A 连续 4 日前 10; B 前两日跌出前 10 后回归
        rows.append({"date": d, "kind": "concept", "member": "A", "rank": 1, "limit_up_count": 10,
                     "max_boards": 4, "leader_symbol": "000001.SZ", "score": 90.0})
        rows.append({"date": d, "kind": "concept", "member": "B", "rank": 2 if i >= 2 else 20,
                     "limit_up_count": 6, "max_boards": 2, "leader_symbol": None, "score": 60.0})
    p = mainline_path(tmp_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(rows).write_parquet(p)

    s = snap.build_recap_snapshot(_repo(tmp_path, {"000001.SZ": "龙一"}), _overview(), now=AFTER_CLOSE)
    by = {m["name"]: m for m in s["mainlines"]}
    assert by["A"]["streak"] == 4
    assert by["A"]["leader"] == {"symbol": "000001.SZ", "name": "龙一"}
    assert by["B"]["streak"] == 2
    assert by["B"]["leader"] is None


# ---------------------------------------------------------------- prompt 文本
def test_prompt_block_contains_comparison_and_intraday_notice(tmp_path):
    _write_regime(tmp_path, DAYS)
    s = snap.build_recap_snapshot(_repo(tmp_path), _overview(), now=datetime(2026, 11, 11, 11, 0))
    text = snap.snapshot_prompt_block(s)
    assert "盘中快照" in text
    assert "| 成交额 |" in text and "较昨日" in text
    assert "4板 ×2: 龙一、龙二" in text
    assert snap.snapshot_prompt_block(None) == ""


def test_user_prompt_drops_stock_avg_volume_ratio_and_includes_prev_plan():
    prompt = market_recap._build_user_prompt(
        _overview(), [], "", snapshot_context="## 近期对比\n| 成交额 |",
        prev_plan={"as_of": "2026-11-10", "plan": "## 🗺️ 明日剧本\n| 强于预期 | 涨停>80 | 加大参与 |"},
    )
    assert "量比" not in prompt
    assert "量能" not in prompt  # 雷达量能维(来自个股量比)不下发
    assert "## 近期对比" in prompt
    assert "来自 2026-11-10 的复盘" in prompt and "涨停>80" in prompt

    no_prev = market_recap._build_user_prompt(_overview(), [], "")
    assert "(无上一交易日复盘)" in no_prev


# ---------------------------------------------------------------- 上一篇剧本
OLD_STYLE = """# 2026-11-10 A股盘后复盘

## 🎯 一句话定调
偏强

## 📌 后续观察要点
量能能否维持 1.6 万亿

## ⚠️ 风险提示
高位分歧

> ⚠️ 免责声明
"""

NEW_STYLE = """## 🎯 定调
**【周期:启动 · 状态:偏强】**

## 🗺️ 明日剧本
| 情景 | 触发条件 | 应对 |
|---|---|---|
| 强于预期 | 涨停>80 | 加大参与 |

## ⛔ 失效条件
- 龙头断板

> ⚠️ 本内容由 AI 生成
"""


def test_extract_plan_sections_supports_old_and_new_templates():
    old = market_recap._extract_plan_sections(OLD_STYLE)
    assert "量能能否维持" in old
    assert "高位分歧" not in old and "偏强" not in old

    new = market_recap._extract_plan_sections(NEW_STYLE)
    assert "涨停>80" in new and "龙头断板" in new
    assert "免责" not in new and "周期:启动" not in new


def test_find_prev_plan_picks_latest_report_before_as_of(monkeypatch):
    reports = [
        {"as_of": "2026-11-11", "created_at": "2026-11-11T20:00:00", "content": NEW_STYLE.replace("涨停>80", "今天")},
        {"as_of": "2026-11-10", "created_at": "2026-11-10T11:50:00", "content": NEW_STYLE.replace("涨停>80", "盘中")},
        {"as_of": "2026-11-10", "created_at": "2026-11-10T21:00:00", "content": NEW_STYLE},
        {"as_of": "2026-11-05", "created_at": "2026-11-05T21:00:00", "content": OLD_STYLE},
    ]
    monkeypatch.setattr("app.services.market_recap_reports.list_reports", lambda: reports)
    prev = market_recap.find_prev_plan("2026-11-11")
    assert prev["as_of"] == "2026-11-10"
    assert "涨停>80" in prev["plan"]

    monkeypatch.setattr("app.services.market_recap_reports.list_reports", lambda: [])
    assert market_recap.find_prev_plan("2026-11-11") is None


# ---------------------------------------------------------------- 流式 meta
async def test_stream_meta_carries_snapshot_and_dated_contexts(monkeypatch, tmp_path):
    _write_regime(tmp_path, DAYS)
    monkeypatch.setattr(market_recap, "build_market_overview", lambda *a, **k: _overview())
    calls: dict = {}
    monkeypatch.setattr("app.services.dragon_tiger.build_recap_context",
                        lambda data_dir, target=None: calls.setdefault("lhb", target) and "")
    monkeypatch.setattr("app.services.auction_benchmark.build_recap_context",
                        lambda data_dir, target=None: calls.setdefault("bench", target) and "")
    monkeypatch.setattr(market_recap, "find_prev_plan", lambda as_of: None)
    monkeypatch.setattr(market_recap, "effective_system_prompt", lambda: "sys")
    captured: list = []

    async def _fake_stream(messages, **kwargs):
        captured.append(messages)
        yield "正文"

    monkeypatch.setattr("app.services.ai_provider.stream_ai_text", _fake_stream)
    events = [json.loads(e) async for e in market_recap.recap_market_stream(_repo(tmp_path))]

    meta = events[0]
    assert meta["type"] == "meta"
    assert meta["snapshot"]["as_of"] == AS_OF.isoformat()
    assert calls == {"lhb": AS_OF, "bench": AS_OF}
    assert "## 近期对比" in captured[0][1]["content"]
    assert events[-1]["type"] == "done"
