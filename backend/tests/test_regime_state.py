"""情绪分单一口径(regime_state): regime 表优先、盘中同公式估算、单位边界转换、复盘只用这一套。"""
import json
from datetime import date
from types import SimpleNamespace

import polars as pl
import pytest

from app.services import regime_builder, regime_state
from app.services.fs_utils import atomic_write_parquet

AS_OF = date(2026, 11, 11)

# 总览口径(百分数) ↔ regime 口径(比例/小数) 的同一天指标
OVERVIEW_KW = dict(
    up_pct=62.0, down_pct=35.0, avg_pct=0.012, median_pct=0.009,
    strong_up_pct=14.0, strong_down_pct=3.0, limit_up=85, broken=24,
    max_boards=4, index_change_pct=0.8, above_ma20_pct=55.0,
)
REGIME_METRICS = {
    "up_pct": 62.0, "down_pct": 35.0, "avg_pct": 0.012, "median_pct": 0.009,
    "strong_up_pct": 14.0, "strong_down_pct": 3.0, "strong_diff_pct": 11.0,
    "limit_up": 85, "seal_rate": 85 / 109, "max_consecutive": 4,
    "index_pct": 0.008, "above_ma20_pct": 0.55,
}


@pytest.fixture(autouse=True)
def _clear_regime_cache(monkeypatch):
    monkeypatch.setattr(regime_builder, "_REGIME_HISTORY_CACHE", None)
    monkeypatch.setattr(regime_builder, "_REGIME_HISTORY_SIG", None)


def _write_regime(data_dir, rows):
    path = regime_builder.regime_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_parquet(pl.DataFrame(rows), path)


def test_overview_metrics_converts_units_to_regime_contract():
    m = regime_state.overview_metrics(**OVERVIEW_KW)
    assert m == pytest.approx(REGIME_METRICS)


def test_overview_metrics_neutral_seal_rate_without_limit_pool():
    m = regime_state.overview_metrics(**{**OVERVIEW_KW, "limit_up": 0, "broken": 0})
    assert m["seal_rate"] == 0.5   # 与 regime_builder._aggregate_daily 同口径, 非 0


def test_estimate_equals_classify_state_on_same_metrics(tmp_path):
    rs = regime_state.resolve_regime_state(tmp_path, AS_OF, regime_state.overview_metrics(**OVERVIEW_KW))
    state, score = regime_builder.classify_state(REGIME_METRICS)
    assert rs["source"] == "estimate"
    assert (rs["score"], rs["state"]) == (score, state)
    assert rs["label"] == regime_builder.STATE_LABELS[state]
    assert set(rs["subscores"]) == {"profit", "speculation", "resilience", "trend"}


def test_regime_row_wins_over_estimate(tmp_path):
    _write_regime(tmp_path, [
        {"date": date(2026, 11, 10), "score": 86, "state": "strong"},
        {"date": AS_OF, "score": 78, "state": "strong", "profit_score": 80,
         "speculation_score": 75, "resilience_score": 90, "trend_score": 66},
    ])
    rs = regime_state.resolve_regime_state(tmp_path, AS_OF, regime_state.overview_metrics(**OVERVIEW_KW))
    assert rs == {
        "score": 78, "state": "strong", "label": "强势", "source": "regime", "date": "2026-11-11",
        "subscores": {"profit": 80, "speculation": 75, "resilience": 90, "trend": 66},
    }


def test_missing_day_in_regime_falls_back_to_estimate(tmp_path):
    _write_regime(tmp_path, [{"date": date(2026, 11, 10), "score": 86, "state": "strong"}])
    rs = regime_state.resolve_regime_state(tmp_path, AS_OF, regime_state.overview_metrics(**OVERVIEW_KW))
    assert rs["source"] == "estimate"


def test_no_regime_and_no_metrics_returns_none(tmp_path):
    assert regime_state.resolve_regime_state(tmp_path, AS_OF, None) is None


def test_index_change_pct_picks_regime_index():
    indices = [{"symbol": "399001.SZ", "change_pct": 2.0}, {"symbol": "000001.SH", "change_pct": -0.5}]
    assert regime_state.index_change_pct(indices) == -0.5
    assert regime_state.index_change_pct([]) is None


async def test_recap_uses_only_regime_state(monkeypatch, tmp_path):
    """复盘 meta/摘要/prompt 都只用 regime_state, 不再出现看板盘面温度。"""
    from app.services import market_recap

    overview = {
        "as_of": "2026-11-11",
        "indices": [],
        "emotion": {"score": 71, "label": "强势"},
        "radar": [{"key": "money", "label": "量能", "value": 40}],
        "regime_state": {
            "score": 78, "state": "strong", "label": "强势", "source": "regime", "date": "2026-11-11",
            "subscores": {"profit": 80, "speculation": 75, "resilience": 90, "trend": 66},
        },
        "breadth": {"up": 3000, "down": 1800, "flat": 100},
        "amount": {"total": 1.5e12},
        "limit": {"limit_up": 85},
    }
    monkeypatch.setattr(market_recap, "build_market_overview", lambda *a, **k: overview)
    monkeypatch.setattr("app.services.dragon_tiger.build_recap_context", lambda data_dir, target=None: "")
    monkeypatch.setattr("app.services.auction_benchmark.build_recap_context", lambda data_dir, target=None: "")
    monkeypatch.setattr(market_recap, "_load_reports", lambda: [])
    captured: list[list[dict]] = []

    async def _fake_stream(messages, **kwargs):
        captured.append(messages)
        yield "正文"

    monkeypatch.setattr("app.services.ai_provider.stream_ai_text", _fake_stream)
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))

    events = [json.loads(e) async for e in market_recap.recap_market_stream(repo)]
    meta = next(e for e in events if e["type"] == "meta")
    assert (meta["emotion_score"], meta["emotion_label"]) == (78, "强势")
    assert "情绪78(强势)" in meta["summary"]
    user_prompt = captured[-1][1]["content"]
    assert "情绪分: 78 (强势, 盘后口径)" in user_prompt
    assert "赚钱80、投机75、抗跌90、趋势66" in user_prompt
    assert "71" not in user_prompt.split("## 市场情绪")[1].split("##")[0]


def test_emotion_block_marks_intraday_estimate():
    from app.services.market_recap import _build_emotion_block

    block = _build_emotion_block({"regime_state": {"score": 60, "label": "偏强", "source": "estimate", "subscores": {}}})
    assert "盘中估算" in block
    assert _build_emotion_block({}) == "(暂无)"
