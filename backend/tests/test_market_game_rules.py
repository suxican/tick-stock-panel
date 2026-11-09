"""Snapshot rules: phase evidence, execution vetoes and shared theme ranking."""
import copy
from datetime import date

import polars as pl
import pytest

from app.services import market_game, market_mainline


def _metrics(**updates):
    return market_game._metrics({
        "breadth_up": 0.5, "median_return": 0.0, "above_ma20": 0.6,
        "index_ret20": 0.04, "index_ret5": 0.01, "index_return": 0.005,
        "index_above_ma20": 0.75, "limit_up": 30, "limit_down": 5,
        "seal_rate": 0.8, "previous_limit_premium": 0.02, "amount_ratio": 1,
        **updates,
    })


def _stock(index, **updates):
    return {
        "symbol": f"{600000 + index}.SH", "name": "样本", "sectors": ["制造"],
        "ref_close": 10, "raw_low": 9.9, "ma5": 9.9, "ma20": 9,
        "ret5": 0.03, "ret20": 0.1, "change_pct": 0.02,
        "amount": 2e8, "atr_pct": 0.02, "close_location": 0.8,
        "drawdown5": 0.09, "listing_days": 365, "history_days": 50,
        "eligible": True, "is_st": False, "limit_up": False,
        "limit_down": False, "one_price": False, "boards": 0, **updates,
    }


def _snapshot():
    return {
        "as_of": "2026-11-06", "quality": "ready", "metrics": _metrics(),
        "previous": _metrics(), "stocks": [_stock(i) for i in range(4)],
    }


def test_index_veto_does_not_overwrite_breadth_repair():
    previous = _metrics(breadth_up=0.306, median_return=-0.015)
    current = _metrics(breadth_up=0.592, median_return=0.005, index_ret5=-0.061)
    state, matches = market_game._market_state(current, previous)
    assert state["phase"] == "修复"
    assert matches["short_index_risk"]
    assert state["risk_vetoes"]
    assert not matches["panic_repair"]  # Broad repair is not the panic entry pattern.


def test_pattern_and_phase_remain_visible_when_index_veto_blocks_allocation():
    snapshot = _snapshot()
    before = copy.deepcopy(snapshot)
    normal = market_game.analyze_snapshot(snapshot)
    assert normal["candidates"]
    assert all(row["role"] == "短线趋势回踩" for row in normal["candidates"])
    assert snapshot == before
    snapshot["metrics"]["index_ret5"] = -0.06
    blocked = market_game.analyze_snapshot(snapshot)
    assert blocked["market_state"]["phase"] == normal["market_state"]["phase"]
    assert blocked["allocation"]["total_cap"] == 0
    assert blocked["allocation"]["max"] == 0 and not blocked["candidates"]
    assert next(h for h in blocked["hypotheses"] if h["id"] == "trend_pullback")["status"] == "matched"


def test_ladder_strength_and_collapse_affect_phase():
    neutral = _metrics(breadth_up=0.52, median_return=0.002, above_ma20=0.5)
    strong = _metrics(**{**neutral, "max_boards": 7, "promotion_rate": 0.25})
    state, _ = market_game._market_state(strong, neutral)
    assert state["phase"] == "主升"
    assert state["max_boards"] == 7 and state["promotion_rate"] == 0.25
    collapsed = _metrics(**{**neutral, "max_boards": 4, "promotion_rate": 0.1})
    state, _ = market_game._market_state(collapsed, strong)
    assert state["phase"] == "退潮"
    assert not state["risk_vetoes"]  # The phase is from the ladder, not an index veto.


@pytest.mark.parametrize("height,promo", [(None, None), (-1, 0.2), (5, 1.1), (float("nan"), 0.2)])
def test_missing_or_invalid_ladder_does_not_manufacture_evidence(height, promo):
    current = _metrics(max_boards=height, promotion_rate=promo, above_ma20=0.5)
    state, _ = market_game._market_state(current, _metrics())
    assert state["phase"] == "待确认"


def test_shared_member_filter_honors_blacklist_and_full_membership_count():
    stocks = [_stock(i, sectors=["制造", "宽基", "屏蔽"]) for i in range(4)]
    sectors, _ = market_game._sectors(
        stocks, 0, {"min_members": 4, "max_members": 600, "blacklist": ["屏蔽"]},
        {"制造": 4, "宽基": 700, "屏蔽": 4},
    )
    assert [row["name"] for row in sectors] == ["制造"]


def test_mainline_score_orders_ladder_strength_before_day_return():
    stocks = []
    for group, levels, change in [("高梯队", [7, 3, 2, 0], 0.02), ("单日强", [1, 1, 1, 0], 0.04)]:
        for level in levels:
            stocks.append(_stock(len(stocks), sectors=[group], boards=level,
                                 limit_up=level > 0, change_pct=change))
    sectors, _ = market_game._sectors(stocks, 0)
    assert [row["name"] for row in sectors] == ["高梯队", "单日强"]
    aggregate = pl.DataFrame([
        {"date": date(2026, 11, 6), "member": "高梯队", "limit_up_count": 3,
         "max_boards": 7, "ge2_count": 3, "rungs_filled": 3},
        {"date": date(2026, 11, 6), "member": "单日强", "limit_up_count": 3,
         "max_boards": 1, "ge2_count": 0, "rungs_filled": 0},
    ])
    expected = {row["member"]: row["score"] for row in market_mainline.score_mainline_frame(aggregate).to_dicts()}
    assert {row["name"]: row["mainline_score"] for row in sectors} == expected


def test_unknown_ladder_is_not_filled_as_first_board():
    sectors, _ = market_game._sectors([_stock(i, limit_up=True, boards=None) for i in range(4)], 0)
    assert sectors[0]["mainline_score"] is None


def _sentiment(level, score):
    return {"status": "ready", "as_of": "2026-11-06", "fetched_at": "2026-11-06T17:00:00+08:00",
            "point_in_time": True, "reason": "盘后情绪与亏钱效应门可用", "metrics": [],
            "loss_gate": {"score": score, "level": level}}


def test_loss_gate_veto_blocks_and_caution_halves_new_exposure():
    snapshot = _snapshot()
    normal = market_game.analyze_snapshot(snapshot)
    assert normal["allocation"]["total_cap"] == pytest.approx(0.4)
    assert any("亏钱效应门未生效" in text for text in normal["limitations"])
    snapshot["sentiment"] = _sentiment("veto", 86.0)
    blocked = market_game.analyze_snapshot(snapshot)
    assert blocked["allocation"]["total_cap"] == 0 and not blocked["candidates"]
    assert any("亏钱效应" in text for text in blocked["market_state"]["risk_vetoes"])
    assert "亏钱效应处于历史高位" in blocked["allocation"]["reason"]
    assert blocked["market_state"]["phase"] == normal["market_state"]["phase"]
    snapshot["sentiment"] = _sentiment("caution", 70.0)
    halved = market_game.analyze_snapshot(snapshot)
    assert halved["allocation"]["total_cap"] == pytest.approx(0.2)
    assert not halved["market_state"]["risk_vetoes"]
    assert any("减半" in text for text in halved["limitations"])
    assert any("亏钱效应门综合分位 70" in fact for fact in halved["hypotheses"][0]["facts"])
    snapshot["sentiment"] = _sentiment("veto", None)  # an unscored gate never vetoes
    assert market_game.analyze_snapshot(snapshot)["allocation"]["total_cap"] == pytest.approx(0.4)


def test_crowded_exit_alone_is_a_prompt_not_a_retreat_or_zero_cap():
    previous = _metrics(breadth_up=0.25, median_return=-0.02, limit_down=20, seal_rate=0.8,
                        previous_limit_premium=-0.005)
    current = _metrics(breadth_up=0.8, median_return=0.02, limit_down=5, seal_rate=0.55,
                       previous_limit_premium=0.01, amount_ratio=1.3)
    state, matches = market_game._market_state(current, previous)
    assert matches["crowded_exit"] and matches["panic_repair"]
    assert state["phase"] == "修复" and state["emotion"] == "分歧"
    assert not state["risk_vetoes"]
    snapshot = {**_snapshot(), "metrics": current, "previous": previous}
    report = market_game.analyze_snapshot(snapshot)
    assert report["allocation"]["total_cap"] == pytest.approx(0.2)
    assert "一致兑现" not in report["allocation"]["reason"]
    assert next(h for h in report["hypotheses"] if h["id"] == "crowded_exit")["status"] == "matched"
