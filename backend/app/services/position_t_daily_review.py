"""持仓做 T 的本地盘后事实汇总；不取行情、不执行交易、不调整参数。"""
from __future__ import annotations

import json
import threading
from collections import Counter, defaultdict
from datetime import date, time
from decimal import Decimal

from app.services.position_t_store import TERMINAL_ORDERS, LedgerError, _encode, _money
from app.strategy.position_t import as_cn, continuous_session


def _day(value):
    return as_cn(value).date().isoformat()


def _sum(values):
    return _money(sum((Decimal(str(value)) for value in values), Decimal(0)))


def _source(execution, mode):
    if execution.get("source") == "simulation_auto":
        return "simulation_auto"
    if mode == "simulation":
        return "simulation_manual"
    return "manual_record"


def _order_state(events, day, now):
    """重建指定日结束前已发生的委托状态，不拿次日终态覆盖前日未决委托。"""
    orders, stopped = {}, False
    for event in events:
        payload = event["payload"]
        stamp = payload.get("occurred_at", event["created_at"])
        if _day(stamp) > day or as_cn(stamp) > now:
            continue
        if event["kind"] == "first_leg_stopped":
            stopped = True
        if event["kind"] != "order_event":
            continue
        key = payload["order_id"]
        old = orders.get(key, {})
        order = {**old, **payload}
        for field in ("quantity", "cumulative_filled", "cancelled_quantity"):
            order[field] = max(old.get(field, 0), order.get(field, 0))
        if old.get("status") in TERMINAL_ORDERS:
            order["status"] = old["status"]
        if order["status"] == "rejected":
            order["cancelled_quantity"] = max(order["cancelled_quantity"], order["quantity"] - order["cumulative_filled"])
        orders[key] = order
    return orders, stopped


class PositionTDailyReview:
    def __init__(self, store, calendar):
        self.store, self.calendar = store, calendar
        self._lock = threading.RLock()
        self._last_scan = None
        with store._tx() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS daily_reviews(
                account_id TEXT NOT NULL, trade_date TEXT NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY(account_id,trade_date))""")
            db.execute("""CREATE TABLE IF NOT EXISTS daily_review_samples(
                account_id TEXT NOT NULL, symbol TEXT NOT NULL, direction TEXT NOT NULL,
                trade_date TEXT NOT NULL, minute_key TEXT NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY(account_id,symbol,direction,minute_key))""")
            db.execute("CREATE INDEX IF NOT EXISTS daily_review_samples_date ON daily_review_samples(account_id,trade_date)")
            db.execute("""CREATE TABLE IF NOT EXISTS daily_review_execution_samples(
                account_id TEXT NOT NULL, trade_date TEXT NOT NULL, minute_key TEXT NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY(account_id,trade_date,minute_key))""")

    def record_cycle(self, signals, now, simulation_status=None):
        now = as_cn(now)
        if (not signals and not simulation_status) or self.calendar(now.date()) is not True or not continuous_session(now):
            return
        minute = now.replace(second=0, microsecond=0).isoformat()
        with self.store._tx() as db:
            for signal in signals:
                if not signal.get("account_id") or not signal.get("symbol") or signal.get("direction") not in {"buy_sell", "sell_buy"}:
                    continue
                sample = {key: signal.get(key) for key in
                          ("state", "strategy_version", "source_mode", "episode_id", "id")}
                sample.update(reasons=list(dict.fromkeys(signal.get("reasons") or [])), observed_at=now.isoformat())
                db.execute("""INSERT INTO daily_review_samples VALUES(?,?,?,?,?,?)
                    ON CONFLICT(account_id,symbol,direction,minute_key) DO UPDATE SET payload=excluded.payload""",
                    (signal["account_id"], signal["symbol"], signal["direction"], now.date().isoformat(), minute, _encode(sample)))
            for account_id, status in (simulation_status or {}).items():
                reasons = list(dict.fromkeys(status.get("reasons") or []))
                if status.get("state") in {"waiting", "paused"} and reasons:
                    sample = {"state": status["state"], "reasons": reasons, "observed_at": now.isoformat()}
                    db.execute("""INSERT INTO daily_review_execution_samples VALUES(?,?,?,?)
                        ON CONFLICT(account_id,trade_date,minute_key) DO UPDATE SET payload=excluded.payload""",
                        (account_id, now.date().isoformat(), minute, _encode(sample)))
                else:
                    # 该分钟已恢复时不把先前临时失败当成最后一次执行状态。
                    db.execute("DELETE FROM daily_review_execution_samples WHERE account_id=? AND trade_date=? AND minute_key=?",
                               (account_id, now.date().isoformat(), minute))

    def finalize_due(self, now):
        now = as_cn(now)
        # 盘前可补归档历史交易日；当天盘后再扫描一次，休市轮次不反复查账本。
        scan = (now.date(), now.time() >= time(15))
        with self._lock:
            if self._last_scan == scan:
                return []
            with self.store._tx() as db:
                rows = db.execute("""SELECT DISTINCT c.account_id,c.trade_date FROM (
                    SELECT account_id,trade_date FROM daily_review_samples
                    UNION SELECT account_id,trade_date FROM daily_review_execution_samples
                    UNION SELECT account_id,trade_date FROM signal_history
                    UNION SELECT account_id,json_extract(payload,'$.trade_date') FROM plans
                    UNION SELECT p.account_id,substr(json_extract(e.payload,'$.executed_at'),1,10)
                        FROM executions e JOIN plans p ON p.id=e.plan_id
                    ) c LEFT JOIN daily_reviews r ON r.account_id=c.account_id AND r.trade_date=c.trade_date
                    WHERE r.account_id IS NULL AND c.trade_date<=? ORDER BY c.trade_date,c.account_id""",
                    (now.date().isoformat(),)).fetchall()
            reports = []
            for row in rows:
                day = date.fromisoformat(row["trade_date"])
                if self._due(day, now):
                    report = self.get_report(row["account_id"], row["trade_date"], now)
                    if report["status"] == "final":
                        reports.append(report)
            self._last_scan = scan
            return reports

    def _due(self, day, now):
        return self.calendar(day) is True and (day < now.date() or (day == now.date() and now.time() >= time(15)))

    def get_report(self, account_id, day, now):
        now = as_cn(now)
        try:
            target = date.fromisoformat(day)
        except (TypeError, ValueError) as exc:
            raise LedgerError("复盘日期须为 YYYY-MM-DD") from exc
        if target > now.date():
            raise LedgerError("不能生成未来交易日复盘")
        with self._lock, self.store._tx() as db:
            existing = db.execute("SELECT payload FROM daily_reviews WHERE account_id=? AND trade_date=?", (account_id, day)).fetchone()
            if existing:
                return json.loads(existing["payload"])
            result = self._build(db, account_id, day, now)
            if self._due(target, now) and result["evidence_status"] == "available":
                result["status"] = "final"
                db.execute("INSERT INTO daily_reviews VALUES(?,?,?)", (account_id, day, _encode(result)))
            return result

    def _build(self, db, account_id, day, now):
        store = self.store
        account = store._get(db, "accounts", account_id)
        plans = store._rows(db, "SELECT payload FROM plans WHERE account_id=?", (account_id,))
        samples = store._rows(db, "SELECT payload FROM daily_review_samples WHERE account_id=? AND trade_date=? ORDER BY minute_key",
                             (account_id, day))
        execution_samples = store._rows(db, "SELECT payload FROM daily_review_execution_samples WHERE account_id=? AND trade_date=? ORDER BY minute_key",
                                       (account_id, day))
        history = store._rows(db, "SELECT payload FROM signal_history WHERE account_id=? AND trade_date=? ORDER BY id", (account_id, day))
        executions = store._rows(db, "SELECT e.payload FROM executions e JOIN plans p ON p.id=e.plan_id WHERE p.account_id=? ORDER BY e.rowid", (account_id,))
        events = [dict(row) | {"payload": json.loads(row["payload"])} for row in db.execute(
            "SELECT kind,plan_id,payload,created_at FROM events WHERE account_id=? AND plan_id IS NOT NULL ORDER BY id", (account_id,)).fetchall()]
        by_plan, plan_events = defaultdict(list), defaultdict(list)
        for execution in executions:
            if _day(execution["executed_at"]) <= day and as_cn(execution["executed_at"]) <= now:
                by_plan[execution["plan_id"]].append(execution)
        for event in events:
            plan_events[event["plan_id"]].append(event)
        trades, summaries, unfinished, configs = [], [], [], []
        for plan in plans:
            if _day(plan["created_at"]) > day or as_cn(plan["created_at"]) > now:
                continue
            fills = by_plan[plan["id"]]
            first = [x for x in fills if x["leg"] == 1]
            second = [x for x in fills if x["leg"] == 2]
            first_qty, second_qty = sum(x["quantity"] for x in first), sum(x["quantity"] for x in second)
            orders, stopped = _order_state(plan_events[plan["id"]], day, now)
            pending = sum(store._order_pending(order, sum(x["quantity"] for x in fills if x["order_id"] == order["order_id"]))
                          for order in orders.values())
            stopped = stopped or first_qty >= plan["quantity"]
            remaining_first_qty = max(0, plan["quantity"]-first_qty) if not stopped else 0
            complete = first_qty > 0 and first_qty == second_qty and stopped and not pending
            first_day = min((_day(x["executed_at"]) for x in first), default=None)
            today = [x for x in fills if _day(x["executed_at"]) == day]
            _, matches = store._matches(first, second, plan["direction"])
            indexed = {x["id"]: x for x in fills}
            today_matches = [m for m in matches if _day(indexed[m["second_execution_id"]]["executed_at"]) == day]
            exposure = (first_qty-second_qty) * (1 if plan["direction"] == "buy_sell" else -1)
            if not (today or exposure or pending or remaining_first_qty or _day(plan["created_at"]) == day):
                continue
            unknown = any(m["net"] is None for m in today_matches)
            provisional = _sum(m["provisional_net"] for m in today_matches)
            summary = {"plan_id": plan["id"], "symbol": plan["symbol"], "direction": plan["direction"],
                "strategy_version": plan["strategy_version"], "execution_mode": plan.get("execution_mode", "manual_record"),
                "realized_net": None if unknown else provisional, "realized_net_provisional": provisional,
                "realized_gross": _sum(m["gross"] for m in today_matches),
                "matched_fees": None if unknown else _sum(m["provisional_fees"] for m in today_matches),
                "matched_quantity": sum(m["quantity"] for m in today_matches),
                "started_today": first_day == day, "completed_today": first_day == day and complete,
                "closed_today": complete and any(x["leg"] == 2 for x in today),
                "has_realized": bool(today_matches), "trade_count": len(today),
                "remaining_first_qty": remaining_first_qty}
            summaries.append(summary)
            configs.append({"plan_id": plan["id"], "strategy_version": plan["strategy_version"], "config": plan["config"]})
            if exposure or pending or remaining_first_qty:
                unfinished.append({"plan_id": plan["id"], "symbol": plan["symbol"], "direction": plan["direction"],
                    "first_qty": first_qty, "second_qty": second_qty, "unmatched_qty": max(0, first_qty-second_qty),
                    "exposure_qty": exposure, "pending_order_qty": pending, "remaining_first_qty": remaining_first_qty,
                    "state": "exposure" if second_qty > first_qty else "unfinished"})
            for execution in today:
                trades.append({key: execution[key] for key in ("id", "plan_id", "leg", "side", "quantity", "price", "executed_at")} |
                    {"symbol": plan["symbol"], "direction": plan["direction"],
                     "fee": None if execution.get("fee_unverified") else execution["allocated_fee"],
                     "fee_provisional": execution["allocated_fee"], "fee_estimated": execution.get("fee_estimated", False),
                     "source": _source(execution, account["mode"])})
        summary = self._aggregate(summaries)
        summary.update(trade_count=len(trades), buy_qty=sum(x["quantity"] for x in trades if x["side"] == "buy"),
                       sell_qty=sum(x["quantity"] for x in trades if x["side"] == "sell"),
                       trade_fees=None if any(x["fee"] is None for x in trades) else _sum(x["fee_provisional"] for x in trades),
                       trade_fees_provisional=_sum(x["fee_provisional"] for x in trades))
        blockers = Counter(reason for sample in samples if sample["state"] in {"paused", "blocked", "expired"}
                           for reason in sample["reasons"])
        execution_blockers = Counter(reason for sample in execution_samples for reason in sample["reasons"])
        triggered = {event["signal"].get("episode_id") or event["signal"]["id"] for event in history if event["signal"].get("state") == "triggered"}
        accepted = {p.get("signal_snapshot", {}).get("episode_id") or p.get("signal_id") for p in plans if _day(p["created_at"]) == day}
        unaccepted = len(triggered-accepted)
        source_counts = dict(Counter(x["source"] for x in trades))
        versions = {x["strategy_version"] for x in summaries} | {s["strategy_version"] for s in samples if s.get("strategy_version")}
        available = bool(samples or execution_samples or history or summaries)
        return {"account_id": account_id, "date": day, "schema_version": 1, "revision": 1,
            "status": "preliminary", "evidence_status": "available" if available else "insufficient",
            "generated_at": now.isoformat(), "source_mode": account["mode"], "source_event_seq": account["event_seq"],
            "summary": summary, "by_symbol": self._groups(summaries, "symbol"), "by_direction": self._groups(summaries, "direction"),
            "unfinished": unfinished, "trades": sorted(trades, key=lambda x: (x["executed_at"], x["id"])), "plans": summaries,
            "blockers": [{"reason": reason, "count": count} for reason, count in blockers.most_common()],
            "execution_blockers": [{"reason": reason, "count": count} for reason, count in execution_blockers.most_common()],
            "coverage": {"sample_count": len(samples), "first_sample_at": min((s["observed_at"] for s in samples), default=None),
                "last_sample_at": max((s["observed_at"] for s in samples), default=None),
                "state_counts": dict(Counter(s["state"] for s in samples)), "source_counts": source_counts,
                "triggered_episode_count": len(triggered), "unaccepted_episode_count": unaccepted,
                "execution_sample_count": len(execution_samples),
                "estimated_fee_count": sum(x["fee_estimated"] for x in trades)},
            "strategy_versions": sorted(versions), "config_evidence": configs,
            "valuation": {"close_equity": None, "hold_equity": None, "incremental_equity": None, "max_drawdown": None,
                "reason": "没有该日完整账户权益轨迹及核验收盘估值；盘中最后价格不是收盘价"},
            "suggestions": self._suggestions(summary, unfinished, blockers, unaccepted, available, execution_blockers),
            "limitations": ["模拟成交与人工登记分别标识，不代表券商实际成交或盈利能力",
                "净收益按第二笔成交日归属；配对费用含两腿，当日发生费用仅含当天成交",
                "完成率仅针对当天首次成交计划的当天完成情况；盈亏分布按当天有配对收益的计划计数",
                "阻断次数为账户/股票/方向每分钟最后一次状态样本，不是每次轮询次数或完整持续时长",
                "执行原因仅保留每账户分钟最后一次有原因的等待/暂停状态，分钟内恢复后清除；不是逐委托完整决策日志，不推断错失收益",
                "未接受机会只说明没有对应计划，不推断错失盈利；旧数据没有状态样本时原因覆盖不足",
                "归档为生成时已记录的事实；后续迟到成交和费用更正不会自动回写本报告",
                "费用可为合同暂估；没有样本外验证，不输出盈利概率，不自动修改参数"]}

    @staticmethod
    def _aggregate(rows):
        started = sum(row["started_today"] for row in rows)
        completed = sum(row["completed_today"] for row in rows)
        known = [r["realized_net"] for r in rows if r["has_realized"] and r["realized_net"] is not None]
        return {"realized_net": None if any(r["realized_net"] is None for r in rows) else _sum(r["realized_net_provisional"] for r in rows),
            "realized_net_provisional": _sum(r["realized_net_provisional"] for r in rows),
            "realized_gross": _sum(r["realized_gross"] for r in rows),
            "matched_fees": None if any(r["matched_fees"] is None for r in rows) else _sum(r["matched_fees"] for r in rows),
            "matched_quantity": sum(r["matched_quantity"] for r in rows),
            "started_count": started, "completed_count": completed,
            "closed_today_count": sum(r["closed_today"] for r in rows), "completion_rate": completed/started if started else None,
            "win_count": sum(value > 0 for value in known), "loss_count": sum(value < 0 for value in known),
            "flat_count": sum(value == 0 for value in known), "unknown_pnl_count": sum(r["realized_net"] is None for r in rows)}

    @classmethod
    def _groups(cls, summaries, field):
        groups = defaultdict(list)
        for row in summaries:
            groups[row[field]].append(row)
        return [{"key": key, **cls._aggregate(rows)} for key, rows in sorted(groups.items())]

    @staticmethod
    def _suggestions(summary, unfinished, blockers, unaccepted, available, execution_blockers):
        items = []
        if not available:
            return [{"code": "missing_evidence", "title": "先积累可核验记录", "detail": "该日没有交易或监控证据，不能判断参数优劣。"}]
        if unfinished:
            items.append({"code": "unfinished_risk", "title": "优先检查未完成敞口", "detail": f"仍有 {len(unfinished)} 个未完成或未决计划，已实现收益不能代替这部分风险。"})
        data_count = sum(count for reason, count in blockers.items() if any(word in reason for word in ("数据", "行情", "盘口", "分钟", "核对")))
        if data_count:
            items.append({"code": "data_quality", "title": "先解决数据与核对阻断", "detail": f"记录到 {data_count} 次相关分钟原因样本；应先核验输入，再研究交易阈值。"})
        if execution_blockers:
            reason, count = execution_blockers.most_common(1)[0]
            items.append({"code": "execution_blocked", "title": "核查自动执行等待原因", "detail": f"最常记录的原因是“{reason}”（{count} 个账户分钟样本）；先核对实际执行条件，不将其解释为错失收益。"})
        if summary["realized_gross"] > 0 and summary["realized_net"] is not None and summary["realized_net"] <= 0:
            items.append({"code": "cost_drag", "title": "检查费用侵蚀", "detail": "当日已配对毛价差为正，但扣两腿费用后不为正；请按实际收费与数量复核可交易空间。"})
        if unaccepted:
            items.append({"code": "unaccepted_opportunities", "title": "核查未转为计划的机会", "detail": f"有 {unaccepted} 个已记录机会未关联接受计划；这不代表错过同等数量的盈利交易。"})
        items.append({"code": "sample_limit", "title": "保持参数，继续独立验证", "detail": f"本日 {summary['started_count']} 个新起始计划、{summary['closed_today_count']} 个完成计划；单日结果不足以证明稳定优势，建议分方向和股票复核后继续积累独立交易日。"})
        return items
