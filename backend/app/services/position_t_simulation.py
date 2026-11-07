"""明确启用的模拟账户执行器：行情之后的完整分钟才可产生模拟成交。

只编排现有做 T 账本和保守撮合，不接券商，也不接管人工计划。各模拟账户
代表独立实验，参与率在每个账户内限制。停机期间不补造历史成交。
"""
from __future__ import annotations

import math
from datetime import timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from app.services.position_t_store import TERMINAL_ORDERS, LedgerError
from app.strategy.position_t import as_cn, continuous_session, first_leg_expired


class PositionTSimulation:
    def __init__(self, store, calendar, clock):
        self.store, self.calendar, self.clock = store, calendar, clock
        self.status_by_account = {}
        self._resume_after = {}
        self._started_at = as_cn(clock())

    def _status(self, account_id, state, now, reasons=()):
        self.status_by_account[account_id] = {"state": state, "reasons": list(reasons), "last_run": now.isoformat()}

    def _session(self, now):
        return self.calendar(now.date()) is True and continuous_session(now)

    @staticmethod
    def _enabled(account):
        return account["mode"] == "simulation" and account["config"].get("auto_simulation_enabled") and account["config"].get("enabled")

    def prepare(self, now):
        now, changed = as_cn(now), False
        for account in self.store.list_accounts():
            now = as_cn(self.clock())
            aid = account["id"]
            if not self._session(now):
                self._status(aid, "idle", now, ["非连续交易时段，自动模拟暂停"])
                continue
            if not self._enabled(account):
                changed |= self.store.cancel_simulation_orders(aid, now)
                self._resume_after.pop(aid, None)
                self._status(aid, "disabled", now)
                continue
            try:
                changed |= self.store.prepare_simulation(aid, now, self.calendar)
                self._resume_after.setdefault(aid, max(now, self._started_at))
                self._status(aid, "running", now)
            except (LedgerError, ValueError) as exc:
                changed |= self.store.cancel_simulation_orders(aid, now, str(exc))
                self._resume_after.pop(aid, None)
                self._status(aid, "paused", now, [str(exc)])
        return changed

    @staticmethod
    def _quote(data, now, cfg):
        quote = data.get("quote", {})
        if quote.get("trade_status") != "trading" or quote.get("data_errors"):
            raise LedgerError("行情状态不可确认，暂停自动模拟")
        for key in ("price", "bid", "ask"):
            value = quote.get(key)
            if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value <= 0:
                raise LedgerError("模拟执行缺少有效价格或盘口")
        if quote["bid"] > quote["ask"]:
            raise LedgerError("模拟执行盘口顺序无效")
        for key in ("market_ts", "received_ts", "book_market_ts"):
            if not quote.get(key) or not 0 <= (now-as_cn(quote[key])).total_seconds() <= cfg["quote_max_age_seconds"]:
                raise LedgerError("行情或盘口过期，暂停自动模拟")
        return quote

    def _latest_bar(self, data, now, cfg, quote):
        bars = []
        for value in data.get("bars", []):
            end = as_cn(value.get("end", value.get("datetime")))
            if value.get("complete", True) is not True or end > now-timedelta(seconds=3) or end.date() != now.date():
                continue
            if value.get("received_ts") and as_cn(value["received_ts"]) > now:
                continue
            if not continuous_session(end-timedelta(minutes=1)) or end.second or end.microsecond:
                continue
            bars.append((end, value))
        if not bars:
            raise LedgerError("尚无完整分钟可用于模拟撮合")
        end, value = max(bars, key=lambda item: item[0])
        if (now-end).total_seconds() > cfg["minute_max_age_seconds"]:
            raise LedgerError("分钟行情过期，暂停模拟撮合")
        for key in ("open", "high", "low", "close", "volume_shares", "amount"):
            item = value.get(key)
            if isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(item) or item <= 0:
                raise LedgerError("分钟价格、成交量或成交额无效")
        if not value["low"]-.01 <= value["amount"]/value["volume_shares"] <= value["high"]+.01:
            raise LedgerError("分钟成交量额单位不一致，暂停模拟撮合")
        return {**value, "end": end.isoformat(), "trade_status": quote["trade_status"],
                **{key: quote.get(key) for key in ("limit_up_price", "limit_down_price", "no_price_limit")}}

    def process_pending(self, outputs, now):
        now, changed = as_cn(now), False
        if not self._session(now):
            return False
        for plan in self.store.list_plans(active_only=True):
            now = as_cn(self.clock())
            if not self._session(now):
                return changed
            if plan.get("execution_mode") != "automatic_simulation":
                continue
            aid = plan["account_id"]
            account = self.store.get_account(aid)
            if not self._enabled(account):
                changed |= self.store.cancel_simulation_orders(aid, now)
                self._status(aid, "disabled", now)
                continue
            try:
                data = outputs.get((aid, plan["symbol"]), {})
                quote = self._quote(data, now, plan["config"])
                bar = self._latest_bar(data, now, plan["config"], quote)
                for order in self.store.plan_detail(plan["id"])["orders"]:
                    if order.get("source") == "simulation_auto" and order["status"] not in TERMINAL_ORDERS:
                        now = as_cn(self.clock())
                        if not self._session(now):
                            return changed
                        self._quote(data, now, plan["config"])
                        changed |= self.store.fill_simulation_order(plan["id"], order["order_id"], bar, now, self.calendar,
                            not_before=self._resume_after.get(aid, max(now, self._started_at)))
                self._status(aid, "running", now)
            except (LedgerError, ValueError, TypeError, KeyError) as exc:
                self._status(aid, "paused", now, [str(exc)])
            if not plan["first_stopped"] and first_leg_expired(plan["created_at"], now, self.calendar, plan["config"]["new_plan_cutoff"]):
                changed |= self.store.cancel_simulation_orders(aid, now, "首笔模拟委托超过有效期", plan_id=plan["id"])
        return changed

    def _submit_order(self, plan, leg, data, now, purpose="target"):
        now = as_cn(self.clock())
        if not self._session(now):
            raise LedgerError("已离开连续交易时段，停止自动模拟")
        quote = self._quote(data, now, plan["config"])
        side = "buy" if (plan["direction"] == "buy_sell") == (leg == 1) else "sell"
        price = Decimal(str(quote["ask" if side == "buy" else "bid"]))
        slip = Decimal(0) if plan["config"].get("reference_includes_slippage") else Decimal(str(plan["config"]["slippage_per_share"]))
        price = (price + slip if side == "buy" else price - slip).quantize(Decimal(".01"), rounding=ROUND_CEILING if side == "buy" else ROUND_FLOOR)
        qty = plan["quantity"]-plan["leg1_filled"] if leg == 1 else plan["unmatched_qty"]
        return self.store.submit_simulation_order(plan["id"], leg, qty, float(price), quote, now, self.calendar,
            expected_version=plan["version"], purpose=purpose)

    def submit(self, signals, outputs, now):
        now, changed = as_cn(now), False
        if not self._session(now):
            return False
        # 先处理已有敞口；任何新委托只能在之后的完整分钟成交。
        for plan in self.store.list_plans(active_only=True):
            now = as_cn(self.clock())
            if not self._session(now):
                return changed
            aid = plan["account_id"]
            if plan.get("execution_mode") != "automatic_simulation" or not self._enabled(self.store.get_account(aid)):
                continue
            monitor = plan.get("monitor", {})
            risks = set(monitor.get("risk_codes", []))
            try:
                if not plan["first_stopped"]:
                    if risks & {"price_invalid", "first_expired"}:
                        changed |= self.store.cancel_simulation_orders(aid, now, "首笔条件失效", plan_id=plan["id"])
                    elif not self.store.plan_detail(plan["id"])["orders"]:
                        changed |= self._submit_order(plan, 1, outputs.get((aid, plan["symbol"]), {}), now)
                elif plan["unmatched_qty"] and (monitor.get("state") == "ready" or (
                        risks & {"price_invalid", "time_expired", "close_approaching"} and monitor.get("state") == "risk")):
                    purpose = "target" if monitor.get("state") == "ready" else "risk"
                    data = outputs.get((aid, plan["symbol"]), {})
                    pending_target = any(o.get("purpose") == "target" and o["leg"] == 2 and o["status"] not in TERMINAL_ORDERS
                                         for o in self.store.plan_detail(plan["id"])["orders"])
                    if purpose == "risk" and pending_target:
                        now = as_cn(self.clock())
                        if not self._session(now):
                            return changed
                        quote = self._quote(data, now, plan["config"])
                        changed |= self.store.cancel_simulation_target(plan["id"], quote, now, self.calendar)
                    else:
                        changed |= self._submit_order(plan, 2, data, now, purpose)
            except (LedgerError, ValueError, TypeError, KeyError) as exc:
                self._status(aid, "paused", now, [str(exc)])
        for signal in signals:
            now = as_cn(self.clock())
            if not self._session(now):
                return changed
            aid = signal["account_id"]
            if signal.get("state") != "triggered" or not self._enabled(self.store.get_account(aid)):
                continue
            plan = None
            try:
                data = outputs.get((aid, signal["symbol"]), {})
                self._quote(data, now, self.store.get_account(aid)["config"])
                now = as_cn(self.clock())
                if not self._session(now):
                    return changed
                plan = self.store.create_plan(signal["id"], signal["quantity"], idempotency_key=f"simulation:{signal['id']}",
                    now=now, automatic_simulation=True)
                changed = True
                changed |= self._submit_order(plan, 1, data, now)
                self._status(aid, "running", now)
            except (LedgerError, ValueError, TypeError, KeyError) as exc:
                if plan and not plan["leg1_filled"]:
                    changed |= self.store.cancel_simulation_orders(aid, now, str(exc), plan_id=plan["id"])
                self._status(aid, "waiting", now, [str(exc)])
        return changed
