from datetime import timedelta

import pytest

from app.backtest.position_t_replay import conservative_fill, replay_position_t
from tests.test_position_t_rules import sample_context


def test_touch_is_not_a_fill_and_limit_lock_is_not_liquidity():
    bar = {"low": 10, "high": 10.2, "open": 10.1, "close": 10.1,
           "volume_shares": 10000, "trade_status": "trading",
           "limit_up_price": 11, "limit_down_price": 9}
    assert conservative_fill(bar, "buy", 10, 200, .01) is None
    assert conservative_fill(bar, "buy", 10.1, 200, .01) == {"price": 10.1, "quantity": 100}
    assert conservative_fill({**bar, "high": 11, "low": 11, "open": 11, "close": 11}, "buy", 11, 200, .01) is None
    assert conservative_fill({**bar, "trade_status": "suspended"}, "sell", 10, 200, .01) is None


def historical_fixture():
    bars, quote, env, pos, account, _now, cfg = sample_context()
    account.update(mode="simulation")
    cfg["participation_rate"] = .02
    for bar in bars:
        bar.update({key: quote[key] for key in ["trade_status", "limit_up_price", "limit_down_price", "history_days", "median_minute_volume_shares"]})
    envs = {bar["datetime"].isoformat(): {**env, "market_ts": bar["datetime"], "sector_ts": bar["datetime"]} for bar in bars}
    return bars, envs, pos, account, cfg


def one_signal(bars, quote, env, pos, account, now, direction, config):
    # 隔离撮合契约：在10:20给唯一确定候选；指标规则另有真实固定样本测试。
    if now.strftime("%H:%M") != "10:20" or direction != "buy_sell":
        return {"state": "none"}
    return {"id": "fixture-signal", "state": "triggered", "account_id": account["id"],
            "symbol": pos["symbol"], "direction": direction, "quantity": 200,
            "first_price": [9.99, 10.01], "second_price": [10.3, 10.32], "stop_price": 9.9,
            "account_version": account["version"], "position_version": pos["version"],
            "strategy_version": "test", "created_at": now.isoformat(),
            "expires_at": (now + timedelta(seconds=60)).isoformat(), "reasons": []}


def add_bar(bars, envs, *, low, high, close, volume=10000):
    bar = {**bars[-1], "datetime": bars[-1]["datetime"]+timedelta(minutes=1),
           "open": close, "close": close, "low": low, "high": high,
           "volume_shares": volume, "amount": volume * close}
    bars.append(bar)
    envs[bar["datetime"].isoformat()] = {**next(iter(envs.values())), "market_ts": bar["datetime"], "sector_ts": bar["datetime"]}


def test_two_legs_cannot_fill_in_one_bar_and_no_close_fill():
    bars, envs, pos, account, cfg = historical_fixture()
    add_bar(bars, envs, low=9.9, high=10.5, close=10.3)
    add_bar(bars, envs, low=9.9, high=10.5, close=10.3)
    out = replay_position_t(bars, envs, ["2026-10-16"], account, pos, cfg, "buy_sell", evaluator=one_signal)
    assert len(out["executions"]) == 1
    assert out["executions"][0]["executed_at"].startswith("2026-10-16T10:22")
    assert out["review"]["unmatched_qty"] == 200
    assert out["review"]["completion_rate"] == 0
    assert out["position"]["qty"] == 1200
    assert out["position"]["sellable_qty"] == 1000


@pytest.mark.parametrize("already_submitted", [False, True])
@pytest.mark.parametrize("volume,filled", [(10000, 200), (5000, 100)])
def test_current_bar_stop_cannot_cancel_before_possible_first_fill(already_submitted, volume, filled):
    bars, envs, pos, account, cfg = historical_fixture()
    # 第一根在五秒延迟内不可完整撮合；第二根可挂单但不穿过买入限价。
    add_bar(bars, envs, low=10.02, high=10.1, close=10.05)
    if already_submitted:
        add_bar(bars, envs, low=10.02, high=10.1, close=10.05)
    # 不知道分钟内先后顺序；不能看到收盘跌破9.90就抹掉10.01的可能成交。
    add_bar(bars, envs, low=9.8, high=10.05, close=9.85, volume=volume)
    out = replay_position_t(bars, envs, ["2026-10-16"], account, pos, cfg, "buy_sell", evaluator=one_signal)
    assert [execution["quantity"] for execution in out["executions"]] == [filled]
    assert out["review"]["unmatched_qty"] == filled
    assert out["position"]["qty"] == 1000 + filled
    assert out["position"]["sellable_qty"] == 1000
    expected_loss = round((10.01 - 9.85) * filled + (5.02 if filled == 200 else 5.01), 2)
    assert out["review"]["incremental_equity"] == pytest.approx(-expected_loss)
    assert out["max_adverse_excursion"] >= expected_loss


def test_partial_first_tracks_only_actual_and_total_equity_includes_unmatched():
    bars, envs, pos, account, cfg = historical_fixture()
    add_bar(bars, envs, low=9.9, high=10.02, close=9.95, volume=5000)
    add_bar(bars, envs, low=9.9, high=10.02, close=9.95, volume=5000)
    add_bar(bars, envs, low=10.2, high=10.5, close=10.35)
    add_bar(bars, envs, low=10.2, high=10.5, close=10.35)
    out = replay_position_t(bars, envs, ["2026-10-16"], account, pos, cfg, "buy_sell", evaluator=one_signal)
    assert [x["quantity"] for x in out["executions"]] == [100, 100]
    assert out["position"]["qty"] == 1000
    assert out["position"]["sellable_qty"] == 900
    assert out["review"]["completion_rate"] == 1
    assert out["review"]["incremental_equity"] == pytest.approx(out["review"]["matched_pnl"])
    assert out["max_drawdown"] >= 0
    assert out["out_of_sample"] is None


def test_observed_environment_is_never_used_before_its_actual_availability():
    bars, envs, pos, account, cfg = historical_fixture()
    for bar in bars:
        stamp = bar["datetime"] + timedelta(seconds=50)
        bar["received_ts"] = stamp.isoformat()
        envs[bar["datetime"].isoformat()].update(observed_at=stamp.isoformat(), market_ts=stamp, sector_ts=stamp)
    checked = []

    def evaluator(history, quote, env, pos, account, now, direction, config):
        assert now >= env["market_ts"]
        checked.append(now)
        return {"state": "none"}

    out = replay_position_t(bars, envs, ["2026-10-16"], account, pos, cfg, evaluator=evaluator)
    assert checked and checked[-1].second == 50
    assert out["executions"] == []


def test_missing_historical_environment_and_calendar_do_not_fabricate_inputs():
    bars, _, pos, account, cfg = historical_fixture()
    out = replay_position_t(bars, {}, ["2026-10-16"], account, pos, cfg)
    assert out["executions"] == []
    assert out["skipped"]
    assert out["status"] == "insufficient_data"
    with pytest.raises(ValueError, match="交易日历"):
        replay_position_t(bars, {}, [], account, pos, cfg)


def test_initial_snapshot_is_not_backdated_into_warmup_and_reserved_cash_rejected():
    bars, envs, pos, account, cfg = historical_fixture()
    checked = []

    def evaluator(history, quote, env, pos, account, now, direction, config):
        checked.append(now)
        return {"state": "none"}

    replay_position_t(bars, envs, ["2026-10-16"], account, pos, cfg, evaluator=evaluator)
    assert checked and all(stamp >= account["snapshot_at"] for stamp in checked)
    with pytest.raises(ValueError, match="预留"):
        replay_position_t(bars, envs, ["2026-10-16"], {**account, "reserved_cash": 100}, pos, cfg)


def test_warmup_window_received_together_does_not_create_old_equity_history():
    bars, envs, pos, account, cfg = historical_fixture()
    observed = bars[-1]["datetime"] + timedelta(seconds=5)
    for bar in bars:
        bar["received_ts"] = observed.isoformat()
    # 初始核对早于监控，但完整预热窗口仅在10:20后首次可用。
    account["snapshot_at"] = pos["snapshot_at"] = bars[0]["datetime"] - timedelta(minutes=10)
    out = replay_position_t(bars, envs, ["2026-10-16"], account, pos, cfg)
    assert len(out["equity_curve"]) == 1
    assert out["equity_curve"][0]["time"] == observed.isoformat()
    assert all(s["created_at"] >= observed.isoformat() for s in out["signals"])


def test_overnight_exposure_unlocks_only_at_next_provided_trading_session():
    bars, envs, pos, account, cfg = historical_fixture()
    add_bar(bars, envs, low=9.9, high=10.02, close=9.95)
    add_bar(bars, envs, low=9.9, high=10.02, close=9.95)
    monday = bars[-1]["datetime"].replace(day=19, hour=9, minute=31)
    next_bar = {**bars[-1], "datetime": monday, "low": 10.2, "high": 10.5,
                "open": 10.35, "close": 10.35, "amount": 103500}
    bars.append(next_bar)
    envs[monday.isoformat()] = {**next(iter(envs.values())), "market_ts": monday, "sector_ts": monday}
    out = replay_position_t(bars, envs, ["2026-10-16", "2026-10-19"], account, pos, cfg, "buy_sell", evaluator=one_signal)
    assert [x["leg"] for x in out["executions"]] == [1, 2]
    assert out["executions"][1]["executed_at"].startswith("2026-10-19")
    assert out["position"]["qty"] == 1000
    assert out["position"]["sellable_qty"] == 1000
