"""独立后台轮询与薄 API 共用的持仓做 T 编排。AI 不在执行依赖链中。"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import threading
from datetime import datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from app.market_time import CN_TZ
from app.services.position_t_daily_review import PositionTDailyReview
from app.services.position_t_simulation import PositionTSimulation
from app.services.position_t_store import LedgerError, PositionTStore
from app.strategy import lots
from app.strategy.position_t import (
    as_cn,
    continuous_session,
    evaluate_candidate,
    first_leg_expired,
    order_fee,
    trading_seconds,
    validate_execution,
    validate_quantity,
)

logger = logging.getLogger(__name__)


def _fresh_timestamp(value, now, max_age):
    try:
        return 0 <= (now - as_cn(value)).total_seconds() <= max_age
    except (ValueError, TypeError):
        return False


def _positive(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0


def _quote_expiry(quote, now, max_age):
    stamps = [as_cn(quote[k]) for k in ("market_ts", "received_ts", "book_market_ts")
              if _fresh_timestamp(quote.get(k), now, max_age)]
    return min([now + timedelta(seconds=max_age), *(s + timedelta(seconds=max_age) for s in stamps)]).isoformat()


def _remaining_net(plan, executions, price, day):
    """按账本 FIFO 计算未匹配首笔成本；仅对尚未执行的第二笔预估费用及偏移。"""
    skip, quantity = plan["matched_qty"], plan["unmatched_qty"]
    notional, paid_fee = Decimal(0), Decimal(0)
    for execution in sorted((x for x in executions if x["leg"] == 1), key=lambda x: (x["executed_at"], x["id"])):
        used = min(skip, execution["quantity"])
        skip -= used
        left = execution["quantity"] - used
        notional += Decimal(str(execution["price"])) * left
        paid_fee += Decimal(str(execution["allocated_fee"])) * left / execution["quantity"]
    cfg = plan["config"]
    side = "sell" if plan["direction"] == "buy_sell" else "buy"
    amount = Decimal(str(price)) * quantity
    future_fee = order_fee(side, price, quantity, cfg, trade_date=day)
    slippage = Decimal(0) if cfg["reference_includes_slippage"] else Decimal(str(cfg["slippage_per_share"])) * quantity
    gross = amount - notional if side == "sell" else notional - amount
    buy_amount = notional if side == "sell" else amount
    return float(gross - paid_fee - future_fee - slippage), float(buy_amount), float(future_fee + slippage)


class PositionTService:
    def __init__(self, data_dir, market, *, store=None, clock=None, notifier=None):
        self.data_dir, self.market = data_dir, market
        self.store = store or PositionTStore(data_dir)
        self.clock = clock or (lambda: datetime.now(CN_TZ))
        # 批次同步只需要证券目录；日历能力在实际监控时访问，并保留动态更新。
        def trading_day(day):
            return self.market.trading_day(day)
        self.simulation = PositionTSimulation(self.store, trading_day, self.clock)
        self.daily_review = PositionTDailyReview(self.store, trading_day)
        self.notifier = notifier or self._notify
        self._stop = threading.Event()
        self._run_lock = threading.Lock()
        self._thread = None
        self._archived_days = set()
        self._session_paused = False
        self.health = {"running": False, "status": "idle", "last_run": None, "reasons": []}
        # 启动后不接受上次进程残留的候选；真实成交和预留始终保留。
        self.store.expire_signals(reason="服务重启，等待新行情重新校验", now=self.clock())

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="position-t-monitor", daemon=True)
        self._thread.start()

    def sync_lots(self, account_id, expected_version):
        """批次只提供数量和参考成本，账户可卖、资金与交易事实必须另行核对。"""
        self.store.get_account(account_id)
        groups = {}
        for lot in lots.load_all(Path(self.data_dir)):
            symbol = str(lot.get("symbol") or "").strip().upper()
            groups.setdefault(symbol, []).append(lot)
        positions, skipped = [], []
        if groups:
            try:
                instruments = {row["symbol"]: row for row in self.market.repo.get_instruments().to_dicts()}
            except Exception as exc:
                raise LedgerError("证券目录暂不可用，未同步任何持仓", "data_unavailable", 503) from exc
            for symbol, records in sorted(groups.items()):
                reason = None
                instrument = instruments.get(symbol)
                if not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", symbol):
                    reason = "证券代码不完整，须包含交易所"
                elif not instrument:
                    reason = "未在股票目录确认证券身份，请先同步证券目录"
                else:
                    try:
                        if self.market.repo.resolve_asset_type(symbol) != "stock" or any(
                                instrument.get(field) not in (None, "stock") for field in ("asset_type", "type")):
                            reason = "首版仅同步 A 股股票，不支持该证券类型"
                    except Exception:
                        logger.exception("批次同步的证券类型核验失败")
                        reason = "证券类型暂不可核验"
                quantity, cost_total = 0, Decimal(0)
                if reason is None:
                    for lot in records:
                        try:
                            raw_qty, raw_cost = lot.get("qty"), lot.get("cost_price")
                            qty, cost = Decimal(str(raw_qty)), Decimal(str(raw_cost))
                            if (not isinstance(raw_qty, (int, float)) or not isinstance(raw_cost, (int, float))
                                    or isinstance(raw_qty, bool) or isinstance(raw_cost, bool)
                                    or not qty.is_finite() or not cost.is_finite()
                                    or qty <= 0 or qty != qty.to_integral_value() or qty > 100_000_000
                                    or cost <= 0 or cost > Decimal("1e12")):
                                raise ValueError("invalid lot")
                            quantity += int(qty)
                            cost_total += qty * cost
                        except (InvalidOperation, TypeError, ValueError):
                            reason = "批次含无效数量或成本，须为正整数股和有限正数成本；该股票未同步"
                            break
                    if quantity > 100_000_000:
                        reason = "合并股数超过持仓上限，该股票未同步"
                if reason:
                    skipped.append({"symbol": symbol, "reason": reason})
                    continue
                exchange = symbol[-2:]
                board = ("bse" if exchange == "BJ" else "star" if symbol.startswith(("688", "689"))
                         else "chinext" if symbol.startswith(("300", "301")) else "main")
                positions.append({"symbol": symbol, "name": str(instrument.get("name") or symbol),
                                  "exchange": exchange, "board": board, "security_type": "stock",
                                  "qty": quantity, "cost_price": float(cost_total / quantity),
                                  "source_lot_ids": sorted({str(lot["id"]) for lot in records if lot.get("id")})})
        return self.store.sync_lots(account_id, positions, skipped, expected_version, now=self.clock())

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception:
                logger.exception("持仓做 T 后台轮次失败")
                self.health = {"running": True, "status": "paused", "last_run": self.clock().isoformat(), "reasons": ["后台轮次失败，等待恢复"]}
                self.store.expire_signals(reason="后台异常，候选失效", now=self.clock())
            self._stop.wait(5)
        self.health = {**self.health, "running": False}

    def run_once(self):
        with self._run_lock:
            now = as_cn(self.clock())
            if self._pause_outside_session(now):
                # 盘后只汇总本地交易证据；不会恢复行情采集或机会扫描。
                try:
                    self.daily_review.finalize_due(now)
                except Exception:
                    logger.exception("持仓做 T 盘后复盘暂不可用")
                    self.health["reasons"].append("盘后复盘暂不可用，稍后重试")
                return
            self._session_paused = False
            self.simulation.prepare(now)
            accounts = {a["id"]: a for a in self.store.list_accounts()}
            active = self.store.list_plans(active_only=True)
            plan_keys = {(p["account_id"], p["symbol"]) for p in active}
            positions = [p for p in self.store.list_positions() if (p["enabled"] and accounts[p["account_id"]]["config"]["enabled"]) or (p["account_id"], p["symbol"]) in plan_keys]
            positions.sort(key=lambda p: (p["account_id"], p["symbol"]) not in plan_keys)
            enabled_keys = {(p["account_id"], p["symbol"]) for p in positions}
            previous = {(s["account_id"], s["symbol"], s["direction"]): s for s in self.store.list_signals()}
            outputs, errors = {}, []
            if positions:
                try:
                    outputs = self.market.collect(positions, accounts, now)
                except Exception:
                    logger.exception("持仓行情数据暂不可用")
                    errors = ["行情适配失败，暂停判断并等待下一轮恢复"]
            now = as_cn(self.clock())
            # 请求可能跨过午休或收盘；返回的数据不能继续产生机会或观察样本。
            if self._pause_outside_session(now):
                return
            if self.simulation.process_pending(outputs, now):
                # 模拟成交会改变资金、可卖和版本，下一次决策必须使用成交后的账本。
                accounts = {a["id"]: a for a in self.store.list_accounts()}
                active = self.store.list_plans(active_only=True)
                plan_keys = {(p["account_id"], p["symbol"]) for p in active}
                positions = [self.store.get_position(p["account_id"], p["symbol"]) for p in positions]
            now = as_cn(self.clock())
            if self._pause_outside_session(now):
                return
            signals, contexts = [], {}
            for pos in positions:
                now = as_cn(self.clock())
                if self._pause_outside_session(now):
                    return
                account = accounts[pos["account_id"]]
                key = (account["id"], pos["symbol"])
                data = outputs.get(key, {"bars": [], "quote": {"data_errors": errors or ["未取得行情"]}, "environment": {}})
                contexts[key] = {**data, "account": account, "position": pos, "computed_at": now.isoformat()}
                for direction in ("buy_sell", "sell_buy"):
                    signal = evaluate_candidate(**data, position=pos, account=account, now=now, direction=direction)
                    if key in plan_keys:
                        signal.update(state="blocked", quantity=0, reasons=["已有未完成计划，优先跟踪第二笔"])
                    signature = {k: signal.get(k) for k in ("account_id", "symbol", "direction", "state", "first_price", "second_price", "stop_price", "cover_guard", "net_min", "quantity", "account_version", "position_version", "strategy_version")}
                    signature["minute"] = now.replace(second=0, microsecond=0).isoformat()
                    signal["id"] = "pt_" + hashlib.sha256(json.dumps(signature, sort_keys=True).encode()).hexdigest()[:24]
                    old = previous.get((*key, direction))
                    continuous = old and old.get("state") == "triggered" and signal["state"] == "triggered" and as_cn(old["expires_at"]) >= now
                    cooling = old and old.get("episode_at") and (now - as_cn(old["episode_at"])).total_seconds() < account["config"]["cooldown_minutes"] * 60
                    signal["episode_id"] = old.get("episode_id", old["id"]) if continuous or cooling else signal["id"]
                    signal["episode_at"] = old.get("episode_at", now.isoformat()) if continuous or cooling else now.isoformat()
                    signal["expires_at"] = _quote_expiry(data["quote"], now, account["config"]["quote_max_age_seconds"])
                    signals.append(signal)
                if data["bars"]:
                    bar = data["bars"][-1]
                    day_key = (*key, now.date().isoformat())
                    warmup = data["bars"] if day_key not in self._archived_days else []
                    self.store.save_observation(account["id"], pos["symbol"], {
                        "minute_key": bar["end"], "trade_date": now.date().isoformat(), "bar": bar,
                        "quote": data["quote"], "environment": data["environment"], "computed_at": now.isoformat(),
                        "account": account, "position": pos,
                        "warmup_bars": warmup,
                    })
                    self._archived_days = {k for k in self._archived_days if k[-1] == now.date().isoformat()}
                    self._archived_days.add(day_key)
            for key, old in previous.items():
                if key[:2] not in enabled_keys and old["state"] != "expired":
                    signals.append({**old, "state": "expired", "expires_at": now.isoformat(), "reasons": ["监控已关闭或持仓不在监控范围"]})
            self.store.save_signals(signals, now=now, contexts=contexts)
            for plan in active:
                now = as_cn(self.clock())
                if self._pause_outside_session(now):
                    return
                self._track_plan(plan, outputs.get((plan["account_id"], plan["symbol"]), {}), now)
            now = as_cn(self.clock())
            if self._pause_outside_session(now):
                return
            self.simulation.submit(signals, outputs, now)
            self.daily_review.record_cycle(signals, now, self.simulation.status_by_account)
            now = as_cn(self.clock())
            if self._pause_outside_session(now):
                return
            self._drain_notifications(now)
            now = as_cn(self.clock())
            if self._pause_outside_session(now):
                return
            paused = errors or (signals and all(s["state"] in {"paused", "expired"} for s in signals))
            self.health = {"running": bool(self._thread and self._thread.is_alive()), "status": "paused" if paused else "monitoring" if positions else "idle",
                           "last_run": now.isoformat(), "reasons": errors, "monitored_positions": len(positions)}

    def _session_pause_reason(self, now):
        trading_day = self.market.trading_day(now.date())
        if trading_day is None:
            return "交易日历未确认，做 T 监控暂停"
        if trading_day is not True or not continuous_session(now):
            return "非交易时段，做 T 机会监控休息中"
        return None

    def _pause_outside_session(self, now):
        reason = self._session_pause_reason(now)
        if reason is None:
            return False
        if not self._session_paused:
            # 只在进入休息时撤销派生机会，不扫描行情、不动成交和资源、不发风险提醒。
            self.store.expire_signals(reason=reason, now=now, pause_plans=True)
            self._session_paused = True
        self.health = {"running": bool(self._thread and self._thread.is_alive()), "status": "idle",
                       "last_run": now.isoformat(), "reasons": [reason], "monitored_positions": 0}
        return True

    def _track_plan(self, plan, data, now):
        quote = data.get("quote", {})
        risks, reasons = [], []
        cfg = plan["config"]
        fresh = all(_fresh_timestamp(quote.get(k), now, cfg["quote_max_age_seconds"])
                    for k in ("market_ts", "received_ts", "book_market_ts"))
        valid_book = _positive(quote.get("bid")) and _positive(quote.get("ask")) and quote["bid"] <= quote["ask"]
        if not fresh or not valid_book or not _positive(quote.get("price")) or quote.get("data_errors"):
            risks.append("data_unavailable")
            reasons.append("行情不足；保留未匹配数量和第二笔资源，需人工核对")
        if quote.get("trade_status") != "trading":
            risks.append("security_unavailable")
            reasons.append("证券暂停交易，当前无法确认第二笔可执行")
        if self.market.trading_day(now.date()) is not True or not continuous_session(now):
            risks.append("session_unavailable")
            reasons.append("当前不在已确认的连续竞价时段")
        executions = self.store.plan_detail(plan["id"])["executions"]
        first_times = [as_cn(x["executed_at"]) for x in executions if x["leg"] == 1]
        started_at = min(first_times) if first_times else as_cn(plan["created_at"])
        age = trading_seconds(started_at, now, self.market.trading_day)
        if age >= plan["config"]["max_leg_minutes"] * 60 and first_times:
            risks.append("time_expired")
            reasons.append("计划超过跟踪时限，需处理实际敞口")
        first_expired = first_leg_expired(plan["created_at"], now, self.market.trading_day, cfg["new_plan_cutoff"])
        if first_expired and not plan["first_stopped"]:
            # 仅停止继续增加首笔；账本仍保留未确认撤单/已成交所需资源。
            plan = self.store.stop_first_leg(plan["id"], now=now)
            risks.append("first_expired")
            reasons.append("首笔等待超过有效期，已停止新增；未决委托仍需确认撤单")
        account = self.store.get_account(plan["account_id"])
        position = self.store.get_position(plan["account_id"], plan["symbol"])
        for obj, label in ((account, "账户"), (position, "持仓")):
            if (obj.get("needs_reconciliation") or not obj.get("reconciled") or obj.get("trade_date") != now.date().isoformat()
                    or not 0 <= (now - as_cn(obj["snapshot_at"])).total_seconds() <= plan["config"]["position_max_age_seconds"]):
                risks.append("account_unreconciled")
                reasons.append(f"{label}数据过期或待核对，第二笔可执行性未知")
        if account.get("effective_cash") is None or account["effective_cash"] < 0 or (
                plan["direction"] == "sell_buy" and plan.get("pending_proceeds", 0)):
            risks.append("funds_changed")
            reasons.append("资金不足或卖出所得待确认，需核对回补资金")
        if position.get("effective_sellable") is None or position["effective_sellable"] < 0 or position["qty"] < position["core_qty"]:
            risks.append("inventory_changed")
            reasons.append("可卖库存或核心持仓约束已变化")
        if plan["unmatched_qty"]:
            second_side = "sell" if plan["direction"] == "buy_sell" else "buy"
            quantity_reasons = validate_quantity(position, second_side, plan["unmatched_qty"])
            if quantity_reasons:
                risks.append("quantity_unavailable")
                reasons.extend([*quantity_reasons, "实际未匹配股数不可按普通申报量直接完成；需人工处理，不自动凑量"])
        if now.time() >= time(14, 45):
            risks.append("close_approaching")
            reasons.append("临近或已收盘；第二笔可能无法完成")
        price = quote.get("price") if fresh and _positive(quote.get("price")) else None
        if price and plan.get("stop_price") and (price <= plan["stop_price"] if plan["direction"] == "buy_sell" else price >= plan["stop_price"]):
            risks.append("price_invalid")
            reasons.append("价格方向失效；风险阈值不保证成交")
        if price and price in (quote.get("limit_up_price"), quote.get("limit_down_price")):
            risks.append("limit_price")
            reasons.append("触及涨跌停价，不能按触价视为成交")
        condition = "等待第一笔实际成交" if not plan["unmatched_qty"] else (
            f"等待{'卖出旧库存' if plan['direction'] == 'buy_sell' else '买回'} {plan['unmatched_qty']} 股；价格区间 {plan['second_price'][0]:.2f}–{plan['second_price'][1]:.2f}，登记实际成交后才匹配")
        state = "waiting_first" if not plan["unmatched_qty"] else "waiting"
        actionable_price, net = None, None
        if plan["unmatched_qty"] and fresh and valid_book:
            side = "sell" if plan["direction"] == "buy_sell" else "buy"
            actionable_price = quote["bid" if side == "sell" else "ask"]
            invalid = validate_execution(side, plan["unmatched_qty"], actionable_price, position, quote, now,
                                         is_trading_day=self.market.trading_day(now.date()))
            if invalid:
                risks.append("execution_unavailable")
                reasons.extend(invalid)
            try:
                net, buy_amount, future_cost = _remaining_net(plan, executions, actionable_price, now.date())
            except ValueError:
                risks.append("fees_unavailable")
                reasons.append("第二笔成交日费用不可确认，暂停机会判断")
            else:
                qty = plan["unmatched_qty"]
                if side == "sell" and ((position.get("effective_sellable") or 0) + plan["reserved_qty"] < qty
                                       or position["qty"] - qty < position["core_qty"]):
                    risks.append("inventory_changed")
                    reasons.append("第二笔剩余股数超出可用旧库存或核心下限")
                if side == "buy" and (account.get("effective_cash") or 0) + plan["reserved_cash"] < actionable_price * qty + future_cost:
                    risks.append("funds_changed")
                    reasons.append("当前回补价格对应的资金不足")
                reached = actionable_price >= plan["second_price"][0] if side == "sell" else actionable_price <= plan["second_price"][1]
                pending = any(leg.get("pending_order_qty", 0) for leg in plan["legs"])
                if pending:
                    condition = "存在未决委托，先核对委托及成交，避免重复提交第二笔"
                elif not plan["first_stopped"]:
                    condition = "第一笔尚可继续成交，先停止新增并核对余量，再判断第二笔"
                elif reached and net >= max(cfg["min_net_profit"], buy_amount * cfg["min_net_return"]):
                    state = "ready"
                    condition = f"{'卖出旧库存' if side == 'sell' else '买回'}条件已满足：{qty} 股，参考{'买一' if side == 'sell' else '卖一'} {actionable_price:.2f} 元；登记实际成交后才匹配"
                elif reached:
                    condition = "已到参考目标，但按实际首笔成本计算的剩余净价差尚未达标"
        if risks:
            state = "paused" if any(r in {"data_unavailable", "security_unavailable", "session_unavailable", "account_unreconciled", "fees_unavailable"} for r in risks) else "risk"
            condition = "；".join(dict.fromkeys(reasons))
        self.store.update_plan_monitor(plan["id"], {"risk_codes": risks, "reasons": reasons,
            "state": state, "actionable_price": actionable_price, "net_profit": round(net, 2) if net is not None else None,
            "expires_at": _quote_expiry(quote, now, cfg["quote_max_age_seconds"]),
            "plan_version": plan["version"], "account_version": account["version"], "position_version": position["version"],
            "quote": quote, "unmatched_qty": plan["unmatched_qty"], "second_leg_condition": condition,
            "computed_at": now.isoformat(), "exposure_value": round(plan["exposure_qty"] * price, 2) if price else None}, now=now)

    def accept_plan(self, payload):
        # 网络校验在事务外，最终版本与资源校验在账本事务内。
        repeated = self.store.get_plan_by_request_key(payload["idempotency_key"])
        if repeated:
            if repeated["signal_id"] != payload["signal_id"] or repeated["quantity"] != payload["quantity"]:
                raise LedgerError("幂等键已用于不同计划请求", "idempotency_conflict", 409)
            return repeated
        reason = self._session_pause_reason(as_cn(self.clock()))
        if reason:
            raise LedgerError(reason, "outside_session", 409)
        try:
            old = self.store.get_signal(payload["signal_id"])
        except LedgerError as exc:
            raise LedgerError("候选已更新，请刷新后确认", "signal_changed", 409) from exc
        self.run_once()
        current = next((s for s in self.store.list_signals(old["account_id"]) if s["symbol"] == old["symbol"] and s["direction"] == old["direction"]), None)
        if not current or current["id"] != old["id"] or current["state"] != "triggered":
            raise LedgerError("行情或候选已变化，请刷新后重新确认", "signal_changed", 409)
        now = as_cn(self.clock())
        reason = self._session_pause_reason(now)
        if reason:
            raise LedgerError(reason, "outside_session", 409)
        return self.store.create_plan(current["id"], payload["quantity"],
            {"account_version": payload["expected_account_version"], "position_version": payload["expected_position_version"]},
            payload["idempotency_key"], now=now)

    def overview(self, account_id=None):
        accounts = self.store.list_accounts()
        chosen = account_id or (accounts[0]["id"] if accounts else None)
        account = self.store.get_account(chosen) if chosen else None
        signals = self.store.list_signals(chosen) if chosen else []
        now = self.clock()
        for signal in signals:
            if signal["state"] in {"triggered", "watching"} and as_cn(signal["expires_at"]) <= now:
                signal.update(state="expired", reasons=["候选已过期，等待下一轮行情"])
        return {"accounts": accounts, "account": account, "positions": self.store.list_positions(chosen) if chosen else [],
                "signals": signals, "plans": self.store.list_plans(chosen) if chosen else [],
                "health": self.health, "config": account["config"] if account else None,
                "simulation": self._simulation_status(account, as_cn(now))}

    def _simulation_status(self, account, now):
        if not account or account["mode"] != "simulation":
            return None
        if not account["config"].get("auto_simulation_enabled", False):
            return {"state": "disabled", "reasons": [], "last_run": None}
        reason = self._session_pause_reason(now)
        if reason:
            return {"state": "idle", "reasons": [reason], "last_run": self.health.get("last_run")}
        if not account["config"]["enabled"]:
            return {"state": "paused", "reasons": ["请先在策略与费用中启用账户监控"], "last_run": None}
        return self.simulation.status_by_account.get(account["id"],
            {"state": "waiting", "reasons": ["等待后台按最新行情校验"], "last_run": None})

    def daily_review_report(self, account_id, day=None):
        now = as_cn(self.clock())
        return self.daily_review.get_report(account_id, day or now.date().isoformat(), now)

    def review(self, account_id, day=None):
        prices = {}
        now = self.clock()
        for signal in self.store.list_signals(account_id):
            q = signal.get("market", {})
            if q.get("market_ts") and 0 <= (now - as_cn(q["market_ts"])).total_seconds() <= 10:
                prices[signal["symbol"]] = q.get("price")
        return self.store.review(account_id, day, prices)

    def replay(self, payload):
        from app.backtest.position_t_replay import replay_position_t

        first, last = (datetime.fromisoformat(payload[k]).date() for k in ("start_date", "end_date"))
        if first > last or (last - first).days > 90:
            raise LedgerError("回放区间须为顺序日期且不超过 90 个自然日")
        if first != last:
            raise LedgerError("首版仅开放单交易日保守模拟；跨日回放尚缺公司行动和外部资金流事件证据", "cross_day_not_supported")
        try:
            sessions = self.market.sessions(first, last)
        except ValueError as exc:
            raise LedgerError("回放日期超出已核验交易日历覆盖") from exc
        if not sessions:
            raise LedgerError("所选日期不是已确认交易日")
        rows = self.store.list_observations(payload["account_id"], payload["symbol"], first.isoformat(), last.isoformat())
        if not rows:
            raise LedgerError("没有当时留存的完整分钟及大盘/板块证据；不能用今天的成分或模拟数据冒充历史回放", "missing_history")
        if any(not r["account"].get("benchmark_valid", True) for r in rows):
            raise LedgerError("该区间包含无法核对的账户变动，持续持有对照无效", "invalid_benchmark")
        initial = rows[0]
        if any(not x.get("reconciled") or x.get("needs_reconciliation") or x.get("trade_date") != first.isoformat()
               for x in (initial["account"], initial["position"])):
            raise LedgerError("回放起始账户和持仓快照尚未核对")
        bar_map, environments = {}, {}
        for row in rows:
            quote = row["quote"]
            for bar in row.get("warmup_bars", []):
                bar_map.setdefault(bar["end"], {**bar, "received_ts": row["computed_at"]})
            bar_map[row["bar"]["end"]] = {**row["bar"], "quote": quote, "received_ts": row["computed_at"],
                **{k: quote.get(k) for k in ("trade_status", "limit_up_price", "limit_down_price", "history_days", "median_minute_volume_shares")}}
            environments[row["bar"]["end"]] = {**row["environment"], "observed_at": row["computed_at"]}
        bars = sorted(bar_map.values(), key=lambda b: b["end"])
        result = replay_position_t(bars, environments, sessions, rows[0]["account"], rows[0]["position"],
                                   rows[0]["account"]["config"], payload.get("direction"))
        return {**result.get("review", {}), **result, "source_mode": "simulation", "data_source": "recorded_observations",
                "initial_snapshot_at": rows[0]["computed_at"], "out_of_sample": None,
                "valuation_scope": "以首个已核对快照为起点的单日保守模拟，非真实账户历史收益"}

    def _drain_notifications(self, now):
        for event in self.store.pending_notifications():
            now = as_cn(self.clock())
            if event["payload"].get("kind") in {"signal", "second_leg_ready"} and self._session_pause_reason(now):
                self.store.ack_notification(event["id"])
                continue
            expiry = event["payload"].get("expires_at")
            if expiry and as_cn(expiry) <= now:
                self.store.ack_notification(event["id"])
                continue
            payload = event["payload"]
            if payload.get("kind") == "signal":
                try:
                    signal = self.store.get_signal(payload["signal_id"])
                except LedgerError:
                    signal = None
                if not signal or signal["state"] != "triggered" or as_cn(signal["expires_at"]) <= now:
                    self.store.ack_notification(event["id"])
                    continue
                account = self.store.get_account(signal["account_id"])
                position = self.store.get_position(signal["account_id"], signal["symbol"])
                if (signal.get("account_version") != account["version"]
                        or signal.get("position_version") != position["version"]
                        or not account["config"]["enabled"] or not position["enabled"]
                        or any(not _fresh_timestamp(obj.get("snapshot_at"), now, account["config"]["position_max_age_seconds"])
                               for obj in (account, position))):
                    self.store.ack_notification(event["id"])
                    continue
            if payload.get("kind") == "second_leg_ready":
                plan = self.store.get_plan(payload["plan_id"])
                monitor = plan.get("monitor", {})
                account = self.store.get_account(plan["account_id"])
                position = self.store.get_position(plan["account_id"], plan["symbol"])
                if (plan["state"] in {"completed", "cancelled"} or monitor.get("state") != "ready"
                        or monitor.get("ready_event_key") != payload.get("ready_event_key")
                        or monitor.get("plan_version") != plan["version"]
                        or monitor.get("account_version") != account["version"]
                        or monitor.get("position_version") != position["version"]
                        or any(not _fresh_timestamp(obj.get("snapshot_at"), now, plan["config"]["position_max_age_seconds"])
                               for obj in (account, position))
                        or not monitor.get("expires_at") or as_cn(monitor["expires_at"]) <= now):
                    self.store.ack_notification(event["id"])
                    continue
            self.notifier(event)
            self.store.ack_notification(event["id"])

    def _notify(self, event):
        from app.services import alert_store

        # outbox 重试与站内记录以稳定事件 id 去重；已读永不写成成交。
        payload = event["payload"]
        direction = "先买后卖" if payload.get("direction") == "buy_sell" else "先卖后买"
        candidate = f"{direction}候选，需人工核对"
        if payload.get("first_price") and payload.get("quantity"):
            low, high = payload["first_price"]
            candidate = f"{direction}候选：第一笔参考 {low:.2f}–{high:.2f} 元，{payload['quantity']} 股，需人工核对"
        descriptions = {"signal": candidate, "second_leg_ready": payload.get("monitor", {}).get("second_leg_condition", "第二笔条件已满足"),
                        "execution": "模拟成交已登记，请查看剩余计划" if payload.get("source_mode") == "simulation" else "实际成交已登记，请核对剩余计划",
                        "plan_risk": "未完成计划需处理：" + "；".join(payload.get("monitor", {}).get("reasons", []))}
        alert = {"ts": int(self.clock().timestamp() * 1000), "event_id": event["id"],
            "rule_id": event["dedup_key"], "source": "position_t", "type": "position_t", "symbol": payload.get("symbol"),
            "message": f"持仓做 T：{descriptions.get(payload.get('kind'), '计划状态已更新')}", **payload}
        if not any(e.get("event_id") == event["id"] for e in alert_store.list_recent(self.data_dir, source="position_t")):
            alert_store.append(self.data_dir, alert)
        # 重试仍广播稳定事件 ID，前端去重；不能因留痕成功而跳过曾失败的送达。
        self.market.quotes.push_alerts([alert])
