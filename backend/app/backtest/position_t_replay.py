"""持仓做 T 保守分钟回放：共享规则与事务账本，不写用户账户。

分钟内未知顺序不作有利假设；只用上一分钟已经生成的计划；默认5秒数据延迟
导致首次撮合等待一个完整可执行分钟。两腿永不在同一根K线成交，收盘不补单。
"""
from __future__ import annotations

import math
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from app.strategy.position_t import (
    DEFAULT_CONFIG,
    as_cn,
    continuous_session,
    evaluate_candidate,
    order_fee,
    validate_quantity,
)


def conservative_fill(bar: dict, side: str, limit_price: float, remaining_qty: int,
                      participation_rate: float = .01) -> dict | None:
    """严格穿过限价才撮合，按限价成交、不取有利开盘价；量上限以股计。

    这是已合法申报订单的部分成交，不对每个部分成交再施加整手限制。
    停牌、一字板、未知状态/限价、零成交量均不成交。
    """
    required = ("open", "high", "low", "close", "volume_shares")
    if side not in {"buy", "sell"} or bar.get("trade_status") != "trading":
        return None
    if any(not isinstance(bar.get(k), (int, float)) or not math.isfinite(bar[k]) or bar[k] <= 0 for k in required):
        return None
    if not 0 < participation_rate <= .1 or remaining_qty <= 0 or limit_price <= 0:
        return None
    if not bar["low"] <= min(bar["open"], bar["close"]) <= max(bar["open"], bar["close"]) <= bar["high"]:
        return None
    if bar["high"] == bar["low"]:
        return None
    if not bar.get("no_price_limit") and (bar.get("limit_up_price") is None or bar.get("limit_down_price") is None):
        return None
    if bar.get("limit_up_price") is not None and limit_price > bar["limit_up_price"]:
        return None
    if bar.get("limit_down_price") is not None and limit_price < bar["limit_down_price"]:
        return None
    if (side == "buy" and not bar["low"] < limit_price) or (side == "sell" and not bar["high"] > limit_price):
        return None
    quantity = min(remaining_qty, math.floor(bar["volume_shares"] * participation_rate))
    return {"price": limit_price, "quantity": quantity} if quantity else None


def _end(bar):
    return as_cn(bar.get("end", bar.get("datetime")))


def _json(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json(item) for item in value]
    return value


def _environment(environments, end):
    value = environments.get(end.isoformat())
    if value is None:
        value = environments.get(end.replace(tzinfo=None).isoformat())
    return value


def _available_at(bar, environment, end):
    """记录接收/计算时间是可用性下界，不能把本分钟稍后的环境前移到收盘时。"""
    values = [end + timedelta(seconds=5)]
    for value in (bar.get("received_ts"), (environment or {}).get("observed_at"),
                  (bar.get("quote") or {}).get("received_ts"), (bar.get("quote") or {}).get("computed_at")):
        if value is not None:
            values.append(as_cn(value))
    return max(values)


def _quote(bar, end, session):
    """无历史买卖报价只能使用明确标记的分钟代理，不能称精确可成交报价。"""
    if bar.get("quote"):
        return {**bar["quote"], "is_trading_day": session}
    return {"price": bar["close"], "bid": bar["close"], "ask": bar["close"],
            "market_ts": end.isoformat(), "received_ts": end.isoformat(),
            "source": "simulation_minute_proxy", "simulated": True,
            "is_trading_day": session, "trade_status": bar.get("trade_status"),
            "limit_up_price": bar.get("limit_up_price"), "limit_down_price": bar.get("limit_down_price"),
            "no_price_limit": bar.get("no_price_limit", False),
            "history_days": bar.get("history_days", 0),
            "median_minute_volume_shares": bar.get("median_minute_volume_shares")}


def _order_event(store, plan, leg, status, quantity, filled, end, order_id):
    return store.record_order_event({"plan_id": plan["id"], "leg": leg, "order_id": order_id,
                                    "event_id": f"{order_id}:{status}:{filled}", "status": status,
                                    "quantity": quantity, "cumulative_filled": filled,
                                    "cancelled_quantity": quantity-filled if status == "cancelled" else 0,
                                    "occurred_at": end.isoformat()}, now=end)


def replay_position_t(bars: list[dict], environments: dict, sessions: list[str], account: dict,
                      position: dict, config=None, direction=None, *, evaluator=evaluate_candidate,
                      store_factory=None) -> dict:
    """独立模拟账户，最多60个实际交易日。

    sessions由调用方已核验的交易日历提供。环境值是当时已经可用的数据，不能
    用现在的环境补历史。公司行动/外部资金流未支持的区间拒绝回放。
    """
    from app.services.position_t_store import PositionTStore

    if not bars or not sessions:
        raise ValueError("分钟数据或真实交易日历缺失")
    if direction not in {None, "buy_sell", "sell_buy"}:
        raise ValueError("回放方向无效")
    if len(sessions) > 60 or len(bars) > 60 * 240:
        raise ValueError("首版回放最多支持 60 个交易日")
    ordered = sorted(bars, key=_end)
    ends = [_end(bar) for bar in ordered]
    if len(ends) != len(set(ends)):
        raise ValueError("回放分钟重复，须先明确修订版本")
    for bar in ordered:
        if not bar.get("complete", True) or any(not isinstance(bar.get(k), (int, float))
                or not math.isfinite(bar[k]) or bar[k] < 0 for k in ("open", "high", "low", "close", "volume_shares", "amount")):
            raise ValueError("回放需要完整且有效的原始分钟数据")
    calendar = {str(value)[:10] for value in sessions}
    if any(_end(bar).date().isoformat() not in calendar for bar in ordered):
        raise ValueError("分钟日期不在已确认交易日历内")
    if any(bar.get("corporate_action") or bar.get("external_flow") for bar in ordered):
        raise ValueError("该区间包含尚未建模的公司行动或外部资金流")
    if any(position.get(key, 0) for key in ("broker_frozen_qty", "external_sell_qty")) or account.get("broker_frozen_cash", 0):
        raise ValueError("回放初始账户有冻结或外部委托，需先提供独立无未决委托的模拟基准")
    if position.get("reserved_qty", 0) or account.get("reserved_cash", 0) or account.get("pending_proceeds", 0):
        raise ValueError("回放初始账户有内部预留或待确认所得，不能重复使用已占资源")
    if account.get("available_cash") is None or position.get("sellable_qty") is None:
        raise ValueError("回放初始现金和可卖数量必须明确")
    if str(position.get("trade_date"))[:10] != _end(ordered[0]).date().isoformat():
        raise ValueError("初始持仓必须属于回放首个交易日，不能把今天库存倒填到历史")
    cfg = {**DEFAULT_CONFIG, **account.get("config", {}), **(config or {})}
    if not cfg["fees_confirmed"]:
        raise ValueError("须确认回放日期适用的费用配置")
    cfg.update(enabled=True)
    first_time = _end(ordered[0])
    if not account.get("snapshot_at") or not position.get("snapshot_at"):
        raise ValueError("回放初始快照必须具有可核验时间")
    initial_at = max(_available_at(ordered[0], _environment(environments, first_time), first_time),
                     as_cn(account["snapshot_at"]), as_cn(position["snapshot_at"]))
    if initial_at.date() != first_time.date():
        raise ValueError("初始账户快照必须属于回放首个交易日")
    with TemporaryDirectory(prefix="position-t-replay-") as temporary:
        store = (store_factory or PositionTStore)(Path(temporary))
        replay_account = store.create_account({"name": "历史保守模拟", "mode": "simulation",
            "available_cash": account["available_cash"], "trade_date": first_time.date().isoformat(),
            "reconciled": True, "snapshot_at": initial_at.isoformat(), "config": cfg}, now=initial_at)
        account_id, symbol = replay_account["id"], position["symbol"]
        # 使用写入契约白名单，不把日内统计或内部预留等视图字段回灌进新账本。
        position_fields = {"name", "exchange", "board", "security_type", "qty", "sellable_qty",
            "today_bought", "today_sold", "broker_frozen_qty", "sellable_net_of_frozen",
            "external_sell_qty", "core_qty", "target_qty", "max_t_qty", "max_extra_qty",
            "cost_price", "cost_method", "enabled", "trade_date", "snapshot_at", "reconciled",
            "benchmark_symbol", "sector_id"}
        input_position = {key: value for key, value in position.items() if key in position_fields}
        input_position.update(snapshot_at=initial_at.isoformat(), reconciled=True, enabled=True)
        store.upsert_position(account_id, symbol, input_position, now=initial_at)
        return _run(store, ordered, environments, calendar, account_id, symbol, cfg, direction, evaluator, initial_at)


def _run(store, bars, environments, calendar, account_id, symbol, cfg, direction, evaluator, initial_at):
    history, curve, signals, executions, skipped = [], [], [], [], []
    counts = Counter()
    active = None
    ready_at = None
    order_ids = {}
    order_sizes = {}
    order_filled = Counter()
    current_day = None
    last_end = None
    last_refresh = None
    per_plan_mae = {}
    reference_cash = store.get_account(account_id)["available_cash"]
    reference_qty = store.get_position(account_id, symbol)["qty"]

    def cancel_open_order(plan, leg, end):
        key = (plan["id"], leg)
        if key in order_ids:
            _order_event(store, plan, leg, "cancelled", order_sizes[key], order_filled[key], end, order_ids[key])
            del order_ids[key]

    for index, bar in enumerate(bars):
        end = _end(bar)
        day = end.date().isoformat()
        env = _environment(environments, end)
        now = _available_at(bar, env, end)
        next_end = _end(bars[index+1]) if index+1 < len(bars) else None
        observation_late = next_end is not None and now >= next_end
        if observation_late:
            skipped.append({"time": end.isoformat(), "reason": "历史观察晚于下一分钟，不前移环境生成信号"})
        if current_day != day:
            if active and current_day:
                for leg in (1, 2):
                    cancel_open_order(active, leg, last_end)
                active = store.stop_first_leg(active["id"], now=last_end)
            history = []
            current_day, last_refresh = day, None
            if last_end is not None:
                account = store.get_account(account_id)
                store.update_snapshot(account_id, {"trade_date": day, "snapshot_at": now.isoformat(),
                    "reconciled": True, "included_event_seq": account["event_seq"],
                    "expected_version": account["version"]}, now=now)
                account, pos = store.get_account(account_id), store.get_position(account_id, symbol)
                store.upsert_position(account_id, symbol, {"trade_date": day, "snapshot_at": now.isoformat(),
                    "sellable_qty": pos["qty"], "today_bought": 0, "today_sold": 0,
                    "included_event_seq": account["event_seq"], "expected_version": pos["version"]}, now=now)
        history.append(bar)
        contiguous = last_end is None or end-last_end == timedelta(minutes=1) or (
            end.strftime("%H:%M") == "13:01" and last_end.strftime("%H:%M") == "11:30") or end.date() != last_end.date()
        quote = _quote(bar, end, day in calendar)
        if not env:
            skipped.append({"time": end.isoformat(), "reason": "缺少当时可用的大盘和板块环境"})
        if active:
            active = store.get_plan(active["id"])
            if active["state"] in {"completed", "cancelled"}:
                active = None
        if active:
            leg = 1 if not active["first_stopped"] else 2
            side = "buy" if (active["direction"] == "buy_sell") == (leg == 1) else "sell"
            key = (active["id"], leg)
            remaining = active["quantity"]-active["leg1_filled"] if leg == 1 else active["unmatched_qty"]
            price = active["first_price"][1 if side == "buy" else 0] if leg == 1 else active["second_price"][1 if side == "buy" else 0]
            start = end-timedelta(minutes=1)
            # 本分钟收盘尚未发生，不能用它撤回已有效或应在分钟开始提交的委托。
            # 只有开始前已知的时间/数据限制才能先于本分钟成交模拟生效。
            if leg == 1 and (start.strftime("%H:%M") >= cfg["new_plan_cutoff"] or not contiguous):
                cancel_open_order(active, 1, start)
                active = store.stop_first_leg(active["id"], now=start)
            elif remaining and continuous_session(end-timedelta(seconds=1)) and contiguous and start >= ready_at:
                pos, account = store.get_position(account_id, symbol), store.get_account(account_id)
                if key not in order_ids and not validate_quantity(pos, side, remaining):
                    order_ids[key] = f"replay:{active['id']}:{leg}:{day}"
                    order_sizes[key], order_filled[key] = remaining, 0
                    _order_event(store, active, leg, "submitted", remaining, 0, end-timedelta(minutes=1), order_ids[key])
                fill = conservative_fill(bar, side, price, remaining, cfg["participation_rate"]) if key in order_ids else None
                if fill:
                    # 使用本订单累计费用差，避免对每个部分成交重复最低佣金。
                    previous = order_filled[key]
                    fee_delta = float(order_fee(side, price, previous+fill["quantity"], cfg, trade_date=day)
                                      - order_fee(side, price, previous, cfg, trade_date=day))
                    available = account["available_cash"]
                    can_fill = (side == "buy" and available >= price*fill["quantity"]+fee_delta) or (
                        side == "sell" and pos["sellable_qty"] >= fill["quantity"] and pos["qty"]-fill["quantity"] >= pos["core_qty"])
                    if can_fill:
                        recorded = store.record_execution({"plan_id": active["id"], "leg": leg,
                            "order_id": order_ids[key], **fill, "executed_at": end.isoformat(),
                            "idempotency_key": f"{order_ids[key]}:{end.isoformat()}"}, now=now)
                        executions.append(recorded["execution"])
                        active = recorded["plan"]
                        order_filled[key] += fill["quantity"]
                        if leg == 1:
                            # 首笔一旦发生部分成交，保守取消未成交量并确认；不继续增加敞口。
                            cancel_open_order(active, 1, end)
                            active = store.stop_first_leg(active["id"], now=end)
                            ready_at = now
                        else:
                            status = "filled" if order_filled[key] == order_sizes[key] else "partially_filled"
                            active = _order_event(store, active, leg, status, order_sizes[key], order_filled[key], end, order_ids[key])
                            if status == "filled":
                                del order_ids[key]
            # 成交优先后再按已完成分钟判断价格失效，只取消仍未成交的第一笔余量。
            invalid = active.get("stop_price") is not None and (
                bar["close"] < active["stop_price"] if active["direction"] == "buy_sell" else bar["close"] > active["stop_price"])
            if leg == 1 and invalid and not active["first_stopped"]:
                cancel_open_order(active, 1, end)
                active = store.stop_first_leg(active["id"], now=end)
            details = store.plan_detail(active["id"])
            signed_cash = sum(((-1 if x["side"] == "buy" else 1)*x["price"]*x["quantity"]-x["allocated_fee"]) for x in details["executions"])
            impact = signed_cash + details["plan"]["exposure_qty"]*bar["close"]
            per_plan_mae[active["id"]] = min(per_plan_mae.get(active["id"], 0), impact)
        elif env and not observation_late and now >= initial_at:
            # 模拟账户每次由本地确定性账本核对；真实模式不得自动刷新人工核对时间。
            if last_refresh is None or (now-last_refresh).total_seconds() >= 600:
                account = store.get_account(account_id)
                store.update_snapshot(account_id, {"snapshot_at": now.isoformat(), "reconciled": True,
                    "included_event_seq": account["event_seq"], "expected_version": account["version"]}, now=now)
                account, pos = store.get_account(account_id), store.get_position(account_id, symbol)
                store.upsert_position(account_id, symbol, {"snapshot_at": now.isoformat(),
                    "included_event_seq": account["event_seq"], "expected_version": pos["version"]}, now=now)
                last_refresh = now
            account, pos = store.get_account(account_id), store.get_position(account_id, symbol)
            for option in ([direction] if direction else ["buy_sell", "sell_buy"]):
                signal = evaluator(history, quote, env, pos, account, now, option, cfg)
                counts[signal["state"]] += 1
                if signal["state"] != "triggered":
                    continue
                store.save_signals([_json(signal)], now=now)
                signals.append(_json(signal))
                try:
                    active = store.create_plan(signal["id"], signal["quantity"],
                        expected_versions={"account_version": account["version"], "position_version": pos["version"]},
                        idempotency_key=f"replay:{signal['id']}", now=now)
                except ValueError as exc:
                    skipped.append({"time": end.isoformat(), "reason": str(exc)})
                    continue
                ready_at = now
                break
        if now < initial_at or end < initial_at.replace(second=0, microsecond=0) or observation_late:
            last_end = end
            continue
        pos, account = store.get_position(account_id, symbol), store.get_account(account_id)
        equity = round(account["available_cash"]+pos["qty"]*bar["close"], 2)
        hold = round(reference_cash+reference_qty*bar["close"], 2)
        curve.append({"time": now.isoformat(), "valuation_market_time": end.isoformat(), "equity": equity, "hold_equity": hold,
                      "incremental_equity": round(equity-hold, 2)})
        last_end = end
    if active:
        for leg in (1, 2):
            cancel_open_order(active, leg, last_end)
        store.stop_first_leg(active["id"], now=last_end)
    review = store.review(account_id, prices={symbol: bars[-1]["close"]} if curve else {})
    peak = curve[0]["equity"] if curve else 0
    delta_peak, drawdown, delta_drawdown = 0., 0., 0.
    for point in curve:
        peak = max(peak, point["equity"])
        delta_peak = max(delta_peak, point["incremental_equity"])
        drawdown = max(drawdown, (peak-point["equity"])/peak if peak else 0)
        delta_drawdown = max(delta_drawdown, delta_peak-point["incremental_equity"])
    limitations = ["保守模拟，不是实盘可成交证明", "分钟代理报价，无队列与逐笔顺序",
                  "首次成交等待延迟后的完整分钟；首笔部分成交后取消余量；两腿不在同根成交",
                  "无收盘补齐，所有未匹配敞口计入期末权益", "历史环境缺失的分钟不生成新机会",
                  "未覆盖公司行动和外部资金流的区间不得使用", "参数未经样本外验证，不输出盈利概率"]
    return {"status": "completed" if counts else "insufficient_data", "mode": "simulation",
            "review": review, "position": store.get_position(account_id, symbol),
            "account": store.get_account(account_id), "plans": store.list_plans(account_id),
            "executions": executions, "signals": signals, "signal_counts": dict(counts),
            "equity_curve": curve, "skipped": skipped, "sample_size": review["started_count"],
            "max_drawdown": round(drawdown, 6), "incremental_drawdown": round(delta_drawdown, 2),
            "max_adverse_excursion": round(max((-x for x in per_plan_mae.values()), default=0), 2),
            "fee_drag": review["fees"], "out_of_sample": None, "probability": None,
            "strategy_versions": sorted({s["strategy_version"] for s in signals}), "limitations": limitations}
