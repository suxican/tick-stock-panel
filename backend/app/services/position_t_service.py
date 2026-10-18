"""独立后台轮询与薄 API 共用的持仓做 T 编排。AI 不在执行依赖链中。"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
from datetime import datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from app.market_time import CN_TZ
from app.services.position_t_store import LedgerError, PositionTStore
from app.strategy import lots
from app.strategy.position_t import as_cn, evaluate_candidate, validate_quantity

logger = logging.getLogger(__name__)


def trading_seconds(start, end, calendar):
    start, end = as_cn(start), as_cn(end)
    total = 0.0
    day = start.date()
    while day <= end.date():
        if calendar(day) is True:
            for first, last in ((time(9, 30), time(11, 30)), (time(13), time(14, 57))):
                left = max(start, datetime.combine(day, first, CN_TZ))
                right = min(end, datetime.combine(day, last, CN_TZ))
                total += max(0, (right - left).total_seconds())
        day += timedelta(days=1)
    return total


class PositionTService:
    def __init__(self, data_dir, market, *, store=None, clock=None, notifier=None):
        self.data_dir, self.market = data_dir, market
        self.store = store or PositionTStore(data_dir)
        self.clock = clock or (lambda: datetime.now(CN_TZ))
        self.notifier = notifier or self._notify
        self._stop = threading.Event()
        self._run_lock = threading.Lock()
        self._thread = None
        self._archived_days = set()
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
            now = self.clock()
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
            now = self.clock()
            signals = []
            for pos in positions:
                account = accounts[pos["account_id"]]
                key = (account["id"], pos["symbol"])
                data = outputs.get(key, {"bars": [], "quote": {"data_errors": errors or ["未取得行情"]}, "environment": {}})
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
                    signal["expires_at"] = (now + timedelta(seconds=account["config"]["quote_max_age_seconds"])).isoformat()
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
            self.store.save_signals(signals, now=now)
            for plan in active:
                self._track_plan(plan, outputs.get((plan["account_id"], plan["symbol"]), {}), now)
            self._drain_notifications(now)
            paused = errors or (signals and all(s["state"] in {"paused", "expired"} for s in signals))
            self.health = {"running": bool(self._thread and self._thread.is_alive()), "status": "paused" if paused else "monitoring" if positions else "idle",
                           "last_run": now.isoformat(), "reasons": errors, "monitored_positions": len(positions)}

    def _track_plan(self, plan, data, now):
        quote = data.get("quote", {})
        risks, reasons = [], []
        fresh = quote.get("market_ts") and 0 <= (now - as_cn(quote["market_ts"])).total_seconds() <= plan["config"]["quote_max_age_seconds"]
        if not fresh or quote.get("data_errors"):
            risks.append("data_unavailable")
            reasons.append("行情不足；保留未匹配数量和第二笔资源，需人工核对")
        if quote.get("trade_status") in {"suspended", "delisted"}:
            risks.append("security_unavailable")
            reasons.append("证券暂停交易，当前无法确认第二笔可执行")
        executions = self.store.plan_detail(plan["id"])["executions"]
        first_times = [as_cn(x["executed_at"]) for x in executions if x["leg"] == 1]
        started_at = min(first_times) if first_times else as_cn(plan["created_at"])
        age = trading_seconds(started_at, now, self.market.trading_day)
        if age >= plan["config"]["max_leg_minutes"] * 60 and first_times:
            risks.append("time_expired")
            reasons.append("计划超过跟踪时限，需处理实际敞口")
        first_expired = not first_times and (age >= 5 * 60 or now.date() > started_at.date() or now.strftime("%H:%M") >= plan["config"]["new_plan_cutoff"])
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
        if account.get("effective_cash") is None or account["effective_cash"] < 0 or plan.get("pending_proceeds", 0):
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
        price = quote.get("price") if fresh else None
        if price and plan.get("stop_price") and (price <= plan["stop_price"] if plan["direction"] == "buy_sell" else price >= plan["stop_price"]):
            risks.append("price_invalid")
            reasons.append("价格方向失效；风险阈值不保证成交")
        if price and price in (quote.get("limit_up_price"), quote.get("limit_down_price")):
            risks.append("limit_price")
            reasons.append("触及涨跌停价，不能按触价视为成交")
        condition = "等待第一笔实际成交" if not plan["unmatched_qty"] else (
            f"等待{'卖出旧库存' if plan['direction'] == 'buy_sell' else '买回'} {plan['unmatched_qty']} 股；价格区间 {plan['second_price'][0]:.2f}–{plan['second_price'][1]:.2f}，登记实际成交后才匹配")
        self.store.update_plan_monitor(plan["id"], {"risk_codes": risks, "reasons": reasons,
            "quote": quote, "unmatched_qty": plan["unmatched_qty"], "second_leg_condition": condition,
            "computed_at": now.isoformat(), "exposure_value": round(plan["exposure_qty"] * price, 2) if price else None}, now=now)

    def accept_plan(self, payload):
        # 网络校验在事务外，最终版本与资源校验在账本事务内。
        repeated = self.store.get_plan_by_request_key(payload["idempotency_key"])
        if repeated:
            if repeated["signal_id"] != payload["signal_id"] or repeated["quantity"] != payload["quantity"]:
                raise LedgerError("幂等键已用于不同计划请求", "idempotency_conflict", 409)
            return repeated
        try:
            old = self.store.get_signal(payload["signal_id"])
        except LedgerError as exc:
            raise LedgerError("候选已更新，请刷新后确认", "signal_changed", 409) from exc
        self.run_once()
        current = next((s for s in self.store.list_signals(old["account_id"]) if s["symbol"] == old["symbol"] and s["direction"] == old["direction"]), None)
        if not current or current["id"] != old["id"] or current["state"] != "triggered":
            raise LedgerError("行情或候选已变化，请刷新后重新确认", "signal_changed", 409)
        return self.store.create_plan(current["id"], payload["quantity"],
            {"account_version": payload["expected_account_version"], "position_version": payload["expected_position_version"]},
            payload["idempotency_key"], now=self.clock())

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
                "health": self.health, "config": account["config"] if account else None}

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
            expiry = event["payload"].get("expires_at")
            if expiry and as_cn(expiry) <= now:
                self.store.ack_notification(event["id"])
                continue
            self.notifier(event)
            self.store.ack_notification(event["id"])

    def _notify(self, event):
        from app.services import alert_store

        # outbox 重试与站内记录以稳定事件 id 去重；已读永不写成成交。
        if any(e.get("event_id") == event["id"] for e in alert_store.list_recent(self.data_dir, source="position_t")):
            return
        payload = event["payload"]
        direction = "先买后卖" if payload.get("direction") == "buy_sell" else "先卖后买"
        alert_store.append(self.data_dir, {"ts": int(self.clock().timestamp() * 1000), "event_id": event["id"],
            "rule_id": event["dedup_key"], "source": "position_t", "type": "position_t", "symbol": payload.get("symbol"),
            "message": f"持仓做 T：{direction}候选，需人工核对" if payload.get("kind") == "signal" else "持仓做 T：未完成计划需处理",
            **payload})
