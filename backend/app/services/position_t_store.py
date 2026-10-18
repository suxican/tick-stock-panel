"""持仓做 T 的单机事务账本。

行情、策略和券商接入不在此模块。所有接受计划/成交写入使用同一个 SQLite
事务，JSON 是版本化投影，events 保存输入事实。金额计算使用 Decimal，现金
投影以分舍入；人工来源始终不宣称券商可执行性。不会迁移或改写旧 lots/paper。
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path

from app.strategy.position_t import DEFAULT_CONFIG, estimate_costs, order_fee, validate_quantity

CN_TZ = timezone(timedelta(hours=8))
CENT = Decimal("0.01")
TERMINAL_ORDERS = {"filled", "cancelled", "rejected"}
INACTIVE_PLANS = {"completed", "cancelled"}


class LedgerError(ValueError):
    def __init__(self, message: str, code: str = "constraint", status_code: int = 422):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def _decimal(value) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise LedgerError("金额或价格格式错误") from exc
    if not result.is_finite():
        raise LedgerError("金额或价格必须为有限数值")
    return result


def _money(value) -> float:
    return float(_decimal(value).quantize(CENT, rounding=ROUND_HALF_UP))


def _quantity(value, label="数量", *, zero=True) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < (0 if zero else 1):
        raise LedgerError(f"{label}必须是{'非负' if zero else '正'}整数股")
    return value


def _now(value=None) -> datetime:
    if value is None:
        return datetime.now(CN_TZ)
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise LedgerError("时间必须包含时区")
    return value.astimezone(CN_TZ)


def _id(prefix):
    return f"{prefix}_{uuid.uuid4().hex}"


def _encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


class PositionTStore:
    """一个数据目录一个账本。连接按操作打开，支持线程及进程间原子接受计划。"""

    def __init__(self, data_dir: Path | str):
        folder = Path(data_dir) / "user_data" / "position_t"
        folder.mkdir(parents=True, exist_ok=True)
        self.path = folder / "ledger.sqlite3"
        self._lock = threading.RLock()
        with self._tx() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS accounts(id TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS positions(
                    account_id TEXT NOT NULL, symbol TEXT NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY(account_id,symbol));
                CREATE TABLE IF NOT EXISTS signals(
                    id TEXT PRIMARY KEY, account_id TEXT NOT NULL, symbol TEXT NOT NULL,
                    payload TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS signals_account ON signals(account_id);
                CREATE TABLE IF NOT EXISTS plans(
                    id TEXT PRIMARY KEY, account_id TEXT NOT NULL, symbol TEXT NOT NULL,
                    active INTEGER NOT NULL, request_key TEXT UNIQUE NOT NULL, payload TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS plans_active_account
                    ON plans(account_id,symbol,active);
                CREATE TABLE IF NOT EXISTS executions(
                    id TEXT PRIMARY KEY, plan_id TEXT NOT NULL, request_key TEXT UNIQUE NOT NULL,
                    payload TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS executions_plan ON executions(plan_id);
                CREATE TABLE IF NOT EXISTS orders(
                    plan_id TEXT NOT NULL, order_id TEXT NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY(plan_id,order_id));
                CREATE TABLE IF NOT EXISTS events(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT NOT NULL,
                    account_seq INTEGER NOT NULL, plan_id TEXT, kind TEXT NOT NULL,
                    request_key TEXT UNIQUE, payload TEXT NOT NULL, created_at TEXT NOT NULL,
                    UNIQUE(account_id,account_seq));
                CREATE INDEX IF NOT EXISTS events_plan ON events(plan_id);
                CREATE TABLE IF NOT EXISTS notifications(
                    id TEXT PRIMARY KEY, dedup_key TEXT UNIQUE NOT NULL, delivered INTEGER NOT NULL,
                    payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS observations(
                    account_id TEXT NOT NULL, symbol TEXT NOT NULL, minute_key TEXT NOT NULL,
                    trade_date TEXT NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY(account_id,symbol,minute_key));
            """)

    @contextmanager
    def _tx(self):
        with self._lock:
            db = sqlite3.connect(self.path, timeout=15, isolation_level=None)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA busy_timeout=15000")
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                if db.in_transaction:
                    db.commit()
            except BaseException:
                if db.in_transaction:
                    db.rollback()
                raise
            finally:
                db.close()

    @staticmethod
    def _get(db, table, identifier):
        row = db.execute(f"SELECT payload FROM {table} WHERE id=?", (identifier,)).fetchone()
        if row is None:
            raise LedgerError("记录不存在", "not_found", 404)
        return json.loads(row["payload"])

    @staticmethod
    def _position(db, account_id, symbol):
        row = db.execute("SELECT payload FROM positions WHERE account_id=? AND symbol=?",
                         (account_id, symbol)).fetchone()
        if row is None:
            raise LedgerError("持仓不存在", "not_found", 404)
        return json.loads(row["payload"])

    @staticmethod
    def _rows(db, query, args=()):
        return [json.loads(row["payload"]) for row in db.execute(query, args).fetchall()]

    @staticmethod
    def _save_account(db, value):
        db.execute("UPDATE accounts SET payload=? WHERE id=?", (_encode(value), value["id"]))

    @staticmethod
    def _save_position(db, value):
        db.execute("INSERT INTO positions VALUES(?,?,?) ON CONFLICT(account_id,symbol) "
                   "DO UPDATE SET payload=excluded.payload",
                   (value["account_id"], value["symbol"], _encode(value)))

    @staticmethod
    def _save_plan(db, value):
        db.execute("UPDATE plans SET active=?,payload=? WHERE id=?",
                   (int(value["state"] not in INACTIVE_PLANS), _encode(value), value["id"]))

    @staticmethod
    def _check_version(value, expected):
        if expected is not None and value["version"] != expected:
            raise LedgerError("数据版本已变化，请刷新后重试", "version_conflict", 409)

    def _event(self, db, account, kind, payload, now, *, plan_id=None, request_key=None):
        account["event_seq"] += 1
        account["version"] += 1
        account["updated_at"] = now.isoformat()
        db.execute("INSERT INTO events(account_id,account_seq,plan_id,kind,request_key,payload,created_at) "
                   "VALUES(?,?,?,?,?,?,?)", (account["id"], account["event_seq"], plan_id, kind,
                   request_key, _encode(payload), now.isoformat()))
        self._save_account(db, account)

    def _active_plans(self, db, account_id):
        return self._rows(db, "SELECT payload FROM plans WHERE account_id=? AND active=1", (account_id,))

    def _daily_usage(self, db, account_id, trade_date):
        plans = self._rows(db, "SELECT payload FROM plans WHERE account_id=?", (account_id,))
        today = [p for p in plans if p["trade_date"] == trade_date and p["state"] != "cancelled"]
        risk = sum((_decimal(p["risk_reserved"]) for p in plans
                    if p["state"] not in INACTIVE_PLANS), Decimal(0))
        for plan in plans:
            executions = {x["id"]: x for x in self._executions(db, plan["id"])}
            for match in plan.get("matches", []):
                completed_at = executions[match["second_execution_id"]]["executed_at"]
                if _now(completed_at).date().isoformat() == trade_date:
                    risk += max(Decimal(0), -_decimal(match["net"] if match["net"] is not None else match["provisional_net"]))
        return today, risk

    def _account_view(self, db, account):
        reserved = sum((_decimal(p["reserved_cash"]) for p in self._active_plans(db, account["id"])), Decimal(0))
        account["reserved_cash"] = _money(reserved)
        cash = account["available_cash"]
        account["effective_cash"] = None if cash is None else _money(
            _decimal(cash) - (0 if account["cash_net_of_frozen"] else _decimal(account["broker_frozen_cash"])) - reserved)
        account["verification_status"] = "simulation" if account["mode"] == "simulation" else "manual_pending"
        today, risk = self._daily_usage(db, account["id"], account["trade_date"])
        account["daily_plans"] = len(today)
        account["daily_risk_used"] = _money(risk)
        account["fees_unknown"] = bool(db.execute(
            "SELECT 1 FROM executions e JOIN plans p ON p.id=e.plan_id WHERE p.account_id=? "
            "AND json_extract(e.payload,'$.fee_unverified')=1 LIMIT 1", (account["id"],)).fetchone())
        if account["fees_unknown"]:
            account["needs_reconciliation"] = True
        return account

    def _position_view(self, db, position):
        reserved = sum(p["reserved_qty"] for p in self._active_plans(db, position["account_id"])
                       if p["symbol"] == position["symbol"])
        position["reserved_qty"] = reserved
        sellable = position["sellable_qty"]
        position["effective_sellable"] = None if sellable is None else (
            sellable - (0 if position["sellable_net_of_frozen"] else position["broker_frozen_qty"]) - reserved)
        account = self._get(db, "accounts", position["account_id"])
        today, _ = self._daily_usage(db, position["account_id"], account["trade_date"])
        same = [p for p in today if p["symbol"] == position["symbol"]]
        position["daily_plans"] = len(same)
        position["daily_t_qty"] = sum(max(p["leg1_filled"], p["quantity"] if not p["first_stopped"] else 0) for p in same)
        position["cooldown_until"] = (max(_now(p["updated_at"]) for p in same) +
                                      timedelta(minutes=account["config"]["cooldown_minutes"])).isoformat() if same else None
        return position

    def list_accounts(self):
        with self._tx() as db:
            return [self._account_view(db, value) for value in self._rows(db, "SELECT payload FROM accounts ORDER BY rowid")]

    def get_account(self, account_id):
        with self._tx() as db:
            return self._account_view(db, self._get(db, "accounts", account_id))

    def get_position(self, account_id, symbol):
        with self._tx() as db:
            return self._position_view(db, self._position(db, account_id, symbol))

    def list_positions(self, account_id=None):
        with self._tx() as db:
            sql = "SELECT payload FROM positions" + (" WHERE account_id=?" if account_id else "")
            return [self._position_view(db, value) for value in self._rows(db, sql, (account_id,) if account_id else ())]

    @staticmethod
    def _config(payload, previous=None):
        unknown = set(payload) - set(DEFAULT_CONFIG)
        if unknown:
            raise LedgerError("未知配置项：" + "、".join(sorted(unknown)))
        result = {**DEFAULT_CONFIG, **(previous or {}), **payload}
        for key, value in result.items():
            if isinstance(DEFAULT_CONFIG[key], bool):
                if not isinstance(value, bool):
                    raise LedgerError(f"{key} 必须为布尔值")
            elif isinstance(DEFAULT_CONFIG[key], (int, float)):
                if isinstance(value, bool) or _decimal(value) < 0:
                    raise LedgerError(f"{key} 必须非负")
                if key in {"max_daily_plans", "max_symbol_daily_plans", "cooldown_minutes",
                           "max_leg_minutes", "min_history_days"}:
                    if _decimal(value) != int(_decimal(value)):
                        raise LedgerError(f"{key} 必须为整数")
                    result[key] = int(value)
                else:
                    result[key] = float(value)
        if result.get("minute_timestamp_semantics") not in {"unknown", "start", "end"}:
            raise LedgerError("分钟时间语义必须为 unknown、start 或 end")
        if not isinstance(result["new_plan_cutoff"], str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", result["new_plan_cutoff"]):
            raise LedgerError("新建计划截止时间须为 HH:MM")
        for key in ("daily_quantity_ratio", "min_net_return", "commission_rate", "stamp_tax_rate",
                    "transfer_rate", "other_fee_rate", "participation_rate", "min_sector_coverage",
                    "market_filter", "sector_filter"):
            if not 0 <= result[key] <= 1:
                raise LedgerError(f"{key} 比例须在 0 与 1 之间")
        if not 0 < result["participation_rate"] <= .1:
            raise LedgerError("成交参与率须大于 0 且不超过 10%")
        for key in ("fee_effective_from", "fee_effective_to"):
            value = result[key]
            if value is None and key == "fee_effective_to":
                continue
            try:
                if not isinstance(value, str) or len(value) != 10:
                    raise ValueError("invalid date")
                date.fromisoformat(value)
            except ValueError as exc:
                raise LedgerError("费用生效日期须为 YYYY-MM-DD") from exc
        if result["fee_effective_to"] and result["fee_effective_to"] < result["fee_effective_from"]:
            raise LedgerError("费用结束日期不能早于起始日期")
        return result

    def create_account(self, payload, now=None):
        now = _now(now)
        mode = payload.get("mode", "manual")
        if mode not in {"manual", "simulation"}:
            raise LedgerError("首版只支持人工或模拟账户")
        cash = payload.get("available_cash")
        if cash is not None and _decimal(cash) < 0:
            raise LedgerError("可用资金不可为负")
        frozen = _money(payload.get("broker_frozen_cash", 0))
        value = {
            "id": _id("account"), "name": str(payload.get("name") or "持仓账户")[:100],
            "mode": mode, "available_cash": None if cash is None else _money(cash),
            "broker_frozen_cash": frozen, "cash_net_of_frozen": payload.get("cash_net_of_frozen", True),
            "trade_date": payload.get("trade_date", now.date().isoformat()),
            "snapshot_at": _now(payload.get("snapshot_at", now)).isoformat(),
            "reconciled": bool(payload.get("reconciled", False)), "needs_reconciliation": False,
            "pending_proceeds": 0, "version": 1, "event_seq": 0,
            "config": self._config(payload.get("config", {})),
            "created_at": now.isoformat(), "updated_at": now.isoformat(),
            "baseline_cash": None if cash is None else _money(_decimal(cash) + (_decimal(frozen) if payload.get("cash_net_of_frozen", True) else 0)),
            "benchmark_valid": cash is not None,
        }
        if frozen < 0:
            raise LedgerError("冻结资金不可为负")
        with self._tx() as db:
            db.execute("INSERT INTO accounts VALUES(?,?)", (value["id"], _encode(value)))
            self._event(db, value, "account_created", value.copy(), now)
            return self._account_view(db, value)

    def _check_snapshot(self, db, account, payload):
        self._check_version(account, payload.get("expected_version"))
        if payload.get("included_event_seq") != account["event_seq"]:
            raise LedgerError("人工核对必须包含当前完整事件水位", "watermark_conflict", 409)
        for plan in self._active_plans(db, account["id"]):
            for order in self._orders(db, plan["id"]):
                if self._order_pending(order, self._order_executed(db, plan["id"], order["order_id"])):
                    raise LedgerError("存在未决委托，先核对最终委托及成交后再覆盖快照", "pending_orders", 409)
        if self._active_plans(db, account["id"]) and (
                payload.get("broker_frozen_cash", 0) or payload.get("broker_frozen_qty", 0)):
            raise LedgerError("活动计划与券商冻结的覆盖关系未知，请先核对委托", "freeze_unknown", 409)

    def update_snapshot(self, account_id, payload, now=None):
        now = _now(now)
        with self._tx() as db:
            account = self._get(db, "accounts", account_id)
            self._check_snapshot(db, account, payload)
            old_economic = self._economic_cash(account)
            for key in ("available_cash", "broker_frozen_cash", "cash_net_of_frozen", "trade_date", "reconciled"):
                if key in payload:
                    account[key] = payload[key]
            for key in ("available_cash", "broker_frozen_cash"):
                if account[key] is not None:
                    account[key] = _money(account[key])
                    if account[key] < 0:
                        raise LedgerError("核对资金不可为负")
            account["snapshot_at"] = _now(payload.get("snapshot_at", now)).isoformat()
            confirms_cash = "available_cash" in payload and payload["available_cash"] is not None and account["reconciled"]
            if confirms_cash:
                account["pending_proceeds"] = 0
                account["cash_estimated"] = False
            account["needs_reconciliation"] = not account["reconciled"]
            for plan in self._rows(db, "SELECT payload FROM plans WHERE account_id=?", (account_id,)):
                if plan.get("pending_proceeds") and confirms_cash:
                    plan["pending_proceeds"] = 0
                    plan["reasons"] = [r for r in plan["reasons"] if r != "回补资金待核对"]
                    plan["version"] += 1
                    self._refresh_plan(db, plan, now)
                    self._save_plan(db, plan)
                for execution in self._executions(db, plan["id"]):
                    if execution.get("proceeds_pending") and confirms_cash:
                        execution["proceeds_pending"] = False
                        db.execute("UPDATE executions SET payload=? WHERE id=?", (_encode(execution), execution["id"]))
            if old_economic != self._economic_cash(account):
                account["benchmark_valid"] = False
            self._event(db, account, "account_snapshot", payload, now)
            return self._account_view(db, account)

    def upsert_position(self, account_id, symbol, payload, now=None):
        now = _now(now)
        with self._tx() as db:
            account = self._get(db, "accounts", account_id)
            return self._upsert_position(db, account, symbol, payload, now)

    def sync_lots(self, account_id, positions, skipped, expected_version, now=None):
        """仅新增批次持仓；存在性检查和写入共享事务，重试不产生新事件。"""
        now = _now(now)
        _quantity(expected_version, "账户版本")
        with self._tx() as db:
            account = self._get(db, "accounts", account_id)
            self._check_version(account, expected_version)
            created, skipped = [], list(skipped)
            has_history = bool(db.execute("SELECT 1 FROM plans WHERE account_id=? LIMIT 1", (account_id,)).fetchone()
                               or db.execute("SELECT 1 FROM observations WHERE account_id=? LIMIT 1", (account_id,)).fetchone())
            for item in positions:
                symbol = item["symbol"]
                if db.execute("SELECT 1 FROM positions WHERE account_id=? AND symbol=?", (account_id, symbol)).fetchone():
                    skipped.append({"symbol": symbol, "reason": "已有做 T 持仓，保留原数量、边界和计划，请在持仓核对中处理变更"})
                    continue
                if has_history:
                    account["benchmark_valid"] = False
                payload = {key: value for key, value in item.items() if key != "source_lot_ids"}
                payload.update(sellable_qty=None, core_qty=item["qty"], target_qty=item["qty"],
                               max_t_qty=0, max_extra_qty=0, enabled=False, reconciled=False,
                               cost_method="manual_reference", trade_date=account["trade_date"],
                               snapshot_at=now.isoformat())
                value = self._upsert_position(db, account, symbol, payload, now, provenance={
                    "source": "lots", "source_synced_at": now.isoformat(), "source_lot_ids": item["source_lot_ids"],
                })
                created.append({key: value[key] for key in ("symbol", "name", "qty")})
            return {"created": created, "skipped": skipped, "account_version": account["version"]}

    def _upsert_position(self, db, account, symbol, payload, now, *, provenance=None):
        account_id = account["id"]
        if not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", symbol):
            raise LedgerError("证券代码须包含交易所，例如 600000.SH")
        allowed = {"symbol", "name", "exchange", "board", "security_type", "qty", "sellable_qty",
                   "today_bought", "today_sold", "broker_frozen_qty", "sellable_net_of_frozen",
                   "external_sell_qty", "core_qty", "target_qty", "max_t_qty", "max_extra_qty",
                   "cost_price", "cost_method", "enabled", "trade_date", "snapshot_at", "reconciled",
                   "benchmark_symbol", "sector_id", "expected_version", "included_event_seq"}
        if set(payload) - allowed:
            raise LedgerError("持仓包含未知或内部派生字段：" + "、".join(sorted(set(payload) - allowed)))
        if payload.get("symbol", symbol) != symbol:
            raise LedgerError("持仓证券代码与请求不一致")
        for key in ("sellable_net_of_frozen", "enabled", "reconciled"):
            if key in payload and not isinstance(payload[key], bool):
                raise LedgerError(f"{key} 必须为布尔值")
        row = db.execute("SELECT payload FROM positions WHERE account_id=? AND symbol=?", (account_id, symbol)).fetchone()
        old = json.loads(row["payload"]) if row else None
        if old:
            self._check_version(old, payload.get("expected_version"))
            self._check_snapshot(db, account, {**payload, "expected_version": account["version"]})
        qty = _quantity(payload.get("qty", old["qty"] if old else 0), "总持仓")
        value = {
            "account_id": account_id, "symbol": symbol, "name": symbol,
            "exchange": symbol[-2:], "board": "main", "security_type": "stock",
            "qty": qty, "sellable_qty": None, "today_bought": 0, "today_sold": 0,
            "broker_frozen_qty": 0, "sellable_net_of_frozen": True, "external_sell_qty": 0,
            "core_qty": qty * 80 // 100, "target_qty": qty,
            "max_t_qty": qty * 20 // 100, "max_extra_qty": qty * 20 // 100,
            "cost_price": None, "cost_method": "manual_reference", "enabled": True,
            "trade_date": account["trade_date"], "snapshot_at": now.isoformat(),
            "reconciled": False, "version": 0, "benchmark_symbol": "000001.SH", "sector_id": None,
            "baseline_qty": qty, "day_initial_sellable": payload.get("sellable_qty"),
            "day_inventory_basis": "first_confirmed_available" if payload.get("sellable_qty") is not None else "unknown",
            **(old or {}), **{key: val for key, val in payload.items()
                              if key not in {"expected_version", "included_event_seq", "account_id", "symbol", "version", "baseline_qty", "day_initial_sellable"}},
        }
        value["qty"] = qty
        for key in ("qty", "today_bought", "today_sold", "broker_frozen_qty", "external_sell_qty", "core_qty", "target_qty", "max_t_qty", "max_extra_qty"):
            _quantity(value[key], key)
        if value["sellable_qty"] is not None:
            _quantity(value["sellable_qty"], "可卖数量")
            if value["sellable_qty"] > qty:
                raise LedgerError("可卖数量不能大于总持仓")
        if (not old and value["core_qty"] > qty) or value["security_type"] != "stock":
            raise LedgerError("核心下限不能超过持仓；首版仅支持 A 股股票")
        if value["exchange"] != symbol[-2:] or value["board"] not in {"main", "chinext", "star", "bse"}:
            raise LedgerError("证券交易所或板块不受支持")
        if old and old["trade_date"] != value["trade_date"]:
            value["day_initial_sellable"] = value["sellable_qty"]
            value["day_inventory_basis"] = "first_confirmed_available" if value["sellable_qty"] is not None else "unknown"
        elif value.get("day_initial_sellable") is None and value["sellable_qty"] is not None:
            # 首次明确可卖仅作为保守的当日额度基准，不反推日初库存或加回已卖量。
            value["day_initial_sellable"] = value["sellable_qty"]
            value["day_inventory_basis"] = "first_confirmed_available"
        if old and old["qty"] != qty:
            account["benchmark_valid"] = False
            active = [p for p in self._active_plans(db, account_id) if p["symbol"] == symbol]
            if active:
                account["needs_reconciliation"] = True
                for plan in active:
                    warning = "活动计划期间持仓快照发生外部数量变化，须核对第二笔与目标持仓"
                    plan["reasons"] = list(dict.fromkeys([*plan["reasons"], warning]))
                    self._save_plan(db, plan)
                    self._notify(db, f"snapshot:{plan['id']}:{account['event_seq']}", {
                        "kind": "plan_risk", "account_id": account_id, "plan_id": plan["id"],
                        "symbol": symbol, "risk_codes": ["external_position_change"], "reasons": [warning]}, now)
        if value["qty"] < value["core_qty"]:
            account["needs_reconciliation"] = True
        value["snapshot_at"] = _now(value["snapshot_at"]).isoformat()
        value["version"] += 1
        value.update(provenance or {})
        self._save_position(db, value)
        self._event(db, account, "position_snapshot", {**payload, **(provenance or {})}, now)
        return self._position_view(db, value)

    def update_config(self, account_id, payload, now=None):
        now = _now(now)
        with self._tx() as db:
            account = self._get(db, "accounts", account_id)
            self._check_version(account, payload.get("expected_version"))
            config = payload.get("config", {key: value for key, value in payload.items() if key != "expected_version"})
            account["config"] = self._config(config, account["config"])
            self._event(db, account, "config_updated", config, now)
            return self._account_view(db, account)

    def save_signals(self, signals, now=None):
        now = _now(now)
        with self._tx() as db:
            for value in signals:
                item = {"id": _id("signal"), "created_at": now.isoformat(), **value}
                previous = db.execute("SELECT payload FROM signals WHERE account_id=? AND symbol=? "
                                      "AND json_extract(payload,'$.direction')=? ORDER BY rowid DESC LIMIT 1",
                                      (item["account_id"], item["symbol"], item["direction"])).fetchone()
                old = json.loads(previous["payload"]) if previous else None
                db.execute("DELETE FROM signals WHERE account_id=? AND symbol=? "
                           "AND json_extract(payload,'$.direction')=? AND id<>?",
                           (item["account_id"], item["symbol"], item["direction"], item["id"]))
                db.execute("INSERT INTO signals VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
                           (item["id"], item["account_id"], item["symbol"], _encode(item)))
                if item.get("state") == "triggered" and (not old or old.get("state") != "triggered"):
                    key = item.get("episode_id", item["id"])
                    self._notify(db, f"signal:{key}", {"kind": "signal", "signal_id": item["id"],
                        "account_id": item["account_id"], "symbol": item["symbol"], "direction": item["direction"],
                        "expires_at": item.get("expires_at"), "created_at": now.isoformat()}, now)

    def list_signals(self, account_id=None):
        with self._tx() as db:
            sql = "SELECT payload FROM signals" + (" WHERE account_id=?" if account_id else "") + " ORDER BY rowid DESC"
            return self._rows(db, sql, (account_id,) if account_id else ())

    def get_signal(self, signal_id):
        with self._tx() as db:
            return self._get(db, "signals", signal_id)

    def expire_signals(self, account_id=None, reason="数据失效，等待重新校验", now=None):
        with self._tx() as db:
            sql = "SELECT payload FROM signals" + (" WHERE account_id=?" if account_id else "")
            for item in self._rows(db, sql, (account_id,) if account_id else ()):
                if item.get("state") in {"triggered", "watching"}:
                    item.update(state="expired", reasons=[reason], expires_at=_now(now).isoformat())
                    db.execute("UPDATE signals SET payload=? WHERE id=?", (_encode(item), item["id"]))

    def create_plan(self, signal_id, quantity, expected_versions=None, idempotency_key="", now=None):
        now = _now(now)
        quantity = _quantity(quantity, zero=False)
        if not idempotency_key:
            raise LedgerError("建立计划必须提供幂等键")
        with self._tx() as db:
            repeated = db.execute("SELECT payload FROM plans WHERE request_key=?", (idempotency_key,)).fetchone()
            if repeated:
                original = json.loads(repeated["payload"])
                if original["signal_id"] != signal_id or original["quantity"] != quantity:
                    raise LedgerError("同一幂等键不能用于不同计划请求", "idempotency_conflict", 409)
                return original
            signal = self._get(db, "signals", signal_id)
            account = self._account_view(db, self._get(db, "accounts", signal["account_id"]))
            pos = self._position_view(db, self._position(db, account["id"], signal["symbol"]))
            active = self._active_plans(db, account["id"])
            if any(p["symbol"] == pos["symbol"] for p in active):
                raise LedgerError("同一股票已有未完成计划，请先处理第二笔", "active_plan", 409)
            versions = expected_versions or {}
            self._check_version(account, versions.get("account_version", signal.get("account_version")))
            self._check_version(pos, versions.get("position_version", signal.get("position_version")))
            cfg = account["config"]
            if signal.get("state") != "triggered" or _now(signal["expires_at"]) <= now:
                raise LedgerError("候选已失效，请重新判断", "signal_expired", 409)
            if not cfg.get("enabled") or not pos["enabled"]:
                raise LedgerError("账户或持仓监控未启用")
            if not cfg.get("fees_confirmed"):
                raise LedgerError("费用合同待确认")
            if account["needs_reconciliation"] or not account["reconciled"] or not pos["reconciled"]:
                raise LedgerError("账户或持仓待核对")
            if account["trade_date"] != now.date().isoformat() or pos["trade_date"] != now.date().isoformat():
                raise LedgerError("交易日变化，须重新核对库存")
            if any((now - _now(x["snapshot_at"])).total_seconds() > cfg["position_max_age_seconds"]
                   or _now(x["snapshot_at"]) > now for x in (account, pos)):
                raise LedgerError("账户或持仓快照过期")
            if now.strftime("%H:%M") >= cfg["new_plan_cutoff"]:
                raise LedgerError("已超过新建计划截止时间")
            if pos["effective_sellable"] is None or pos["effective_sellable"] < quantity:
                raise LedgerError("可卖数量未知或可卖不足")
            if quantity > min(pos["max_t_qty"], signal.get("quantity", 0)):
                raise LedgerError("超过单次可规划数量")
            direction = signal["direction"]
            if direction not in {"buy_sell", "sell_buy"}:
                raise LedgerError("交易方向不受支持")
            errors = validate_quantity(pos, "buy", quantity) + validate_quantity(pos, "sell", quantity)
            if errors:
                raise LedgerError("；".join(errors))
            first_low, first_high = map(_decimal, signal["first_price"])
            second_low, second_high = map(_decimal, signal["second_price"])
            if min(first_low, second_low) <= 0 or first_high < first_low or second_high < second_low:
                raise LedgerError("价格区间无效")
            buy = first_high if direction == "buy_sell" else second_high
            sell = second_low if direction == "buy_sell" else first_low
            costs = estimate_costs(buy, sell, quantity, cfg, trade_date=account["trade_date"])
            if _decimal(costs["net"]) < max(_decimal(cfg["min_net_profit"]), buy * quantity * _decimal(cfg["min_net_return"])):
                raise LedgerError("净价差不足以覆盖费用与最低要求")
            if direction == "buy_sell":
                if pos["qty"] + quantity > pos["target_qty"] + pos["max_extra_qty"]:
                    raise LedgerError("超过临时增仓上限")
                if buy * quantity > _decimal(cfg["max_extra_notional"]):
                    raise LedgerError("超过临时增仓金额上限")
            elif pos["qty"] - pos["external_sell_qty"] - quantity < pos["core_qty"]:
                raise LedgerError("卖出后低于核心持仓下限")
            stop = _decimal((signal.get("stop_price") if direction == "buy_sell" else signal.get("cover_guard")) or
                            (buy if direction == "buy_sell" else sell))
            risk = abs((buy if direction == "buy_sell" else sell) - stop) * quantity + _decimal(costs["total_fees"]) + _decimal(costs.get("slippage", 0))
            if signal.get("risk_estimate") is not None and signal.get("quantity"):
                risk = max(risk, _decimal(signal["risk_estimate"]) * quantity / signal["quantity"])
            if risk > _decimal(cfg["single_risk_budget"]):
                raise LedgerError("超过单次风险预算")
            self._daily_limits(db, account, pos, quantity, risk, now)
            value = {
                "id": _id("plan"), "account_id": account["id"], "symbol": pos["symbol"],
                "direction": direction, "quantity": quantity, "state": "first_pending",
                "leg1_filled": 0, "leg2_filled": 0, "matched_qty": 0, "unmatched_qty": 0,
                "exposure_qty": 0, "matched_pnl": 0, "reserved_cash": 0, "reserved_qty": quantity,
                "first_price": signal["first_price"], "second_price": signal["second_price"],
                "stop_price": signal.get("stop_price"), "cover_guard": signal.get("cover_guard"),
                "created_at": now.isoformat(), "updated_at": now.isoformat(), "trade_date": account["trade_date"],
                "version": 1, "reasons": [], "legs": [], "strategy_version": signal["strategy_version"],
                "signal_id": signal_id, "first_stopped": False, "risk_reserved": _money(risk),
                "signal_snapshot": signal,
                "config": cfg, "baseline_qty": pos["qty"], "target_qty": pos["target_qty"],
                "source_mode": account["mode"], "pending_proceeds": 0,
            }
            self._refresh_plan(db, value, now)
            if account["effective_cash"] is None or _decimal(account["effective_cash"]) < _decimal(value["reserved_cash"]):
                raise LedgerError("资金不足（包含费用、回补缺口与风险缓冲）")
            db.execute("INSERT INTO plans VALUES(?,?,?,?,?,?)", (value["id"], account["id"], pos["symbol"], 1,
                       idempotency_key, _encode(value)))
            self._event(db, account, "plan_created", {"signal_id": signal_id, "quantity": quantity}, now,
                        plan_id=value["id"], request_key=f"plan:{idempotency_key}")
            return value

    def _daily_limits(self, db, account, pos, quantity, risk, now):
        cfg = account["config"]
        today, used = self._daily_usage(db, account["id"], now.date().isoformat())
        same = [p for p in today if p["symbol"] == pos["symbol"]]
        if len(today) >= cfg["max_daily_plans"] or len(same) >= cfg["max_symbol_daily_plans"]:
            raise LedgerError("达到当日计划次数上限")
        qty_used = sum(max(p["leg1_filled"], p["quantity"] if not p["first_stopped"] else 0) for p in same)
        initial = pos.get("day_initial_sellable")
        if initial is None or qty_used + quantity > _decimal(initial) * _decimal(cfg["daily_quantity_ratio"]):
            raise LedgerError("达到当日累计数量上限或日初库存未知")
        if used + risk > _decimal(cfg["daily_risk_budget"]):
            raise LedgerError("超过当日累计风险预算")
        if same and (now - max(_now(p["updated_at"]) for p in same)).total_seconds() < cfg["cooldown_minutes"] * 60:
            raise LedgerError("处于计划冷却时间")

    def get_plan(self, plan_id):
        with self._tx() as db:
            return self._get(db, "plans", plan_id)

    def get_plan_by_request_key(self, key):
        with self._tx() as db:
            row = db.execute("SELECT payload FROM plans WHERE request_key=?", (key,)).fetchone()
            return json.loads(row["payload"]) if row else None

    def list_plans(self, account_id=None, active_only=False):
        with self._tx() as db:
            clauses, args = [], []
            if account_id:
                clauses.append("account_id=?")
                args.append(account_id)
            if active_only:
                clauses.append("active=1")
            return self._rows(db, "SELECT payload FROM plans" + (" WHERE " + " AND ".join(clauses) if clauses else "") + " ORDER BY rowid DESC", args)

    def _orders(self, db, plan_id):
        return self._rows(db, "SELECT payload FROM orders WHERE plan_id=?", (plan_id,))

    def _executions(self, db, plan_id):
        return self._rows(db, "SELECT payload FROM executions WHERE plan_id=? ORDER BY rowid", (plan_id,))

    def _order_executed(self, db, plan_id, order_id):
        return sum(item["quantity"] for item in self._executions(db, plan_id) if item["order_id"] == order_id)

    @staticmethod
    def _allocate_fees(group, side, config, trade_date):
        """实际逐笔费用保持原值，未知部分分摊收费组尚未覆盖的暂估费用。"""
        known = sum((_decimal(x["fee_input"]) for x in group if x["fee_input"] is not None), Decimal(0))
        unknown = [x for x in group if x["fee_input"] is None]
        notional = sum((_decimal(x["price"]) * x["quantity"] for x in group), Decimal(0))
        qty = sum(x["quantity"] for x in group)
        applicable = config["fee_effective_from"] <= trade_date and (
            not config.get("fee_effective_to") or trade_date <= config["fee_effective_to"])
        # 过期合同只能提供临时现金估值，不能据此输出已经核算的净收益。
        total = max(known, order_fee(side, notional / qty, qty, config,
                    trade_date=trade_date if applicable else None)) if unknown else known
        missing_notional = sum((_decimal(x["price"]) * x["quantity"] for x in unknown), Decimal(0))
        left = total - known
        for item in group:
            if item["fee_input"] is not None:
                item["allocated_fee"] = _money(item["fee_input"])
            else:
                share = left if item is unknown[-1] else ((total - known) * _decimal(item["price"]) * item["quantity"] / missing_notional).quantize(CENT, rounding=ROUND_HALF_UP)
                item["allocated_fee"] = _money(share)
                left -= share
            item["fee_estimated"] = item["fee_input"] is None
            item["fee_unverified"] = item["fee_input"] is None and not applicable
            item["fee_trade_date"] = trade_date
        return total

    @staticmethod
    def _order_pending(order, actual):
        if order["status"] not in TERMINAL_ORDERS:
            return max(0, order["quantity"] - actual)
        reported = order.get("cumulative_filled", 0)
        accounted = reported + order.get("cancelled_quantity", 0)
        if order["status"] == "filled":
            accounted = reported
        return max(0, reported - actual, order["quantity"] - accounted)

    def _refresh_plan(self, db, plan, now):
        executions = self._executions(db, plan["id"])
        orders = self._orders(db, plan["id"])
        first = [x for x in executions if x["leg"] == 1]
        second = [x for x in executions if x["leg"] == 2]
        n1, n2 = sum(x["quantity"] for x in first), sum(x["quantity"] for x in second)
        plan["leg1_filled"], plan["leg2_filled"] = n1, n2
        plan["first_filled_at"] = min((x["executed_at"] for x in first), default=None)
        if n1 >= plan["quantity"]:
            plan["first_stopped"] = True
        pending = {1: 0, 2: 0}
        for order in orders:
            actual = sum(x["quantity"] for x in executions if x["order_id"] == order["order_id"])
            pending[order["leg"]] += self._order_pending(order, actual)
        remaining = max(0 if plan["first_stopped"] else plan["quantity"] - n1, pending[1])
        unmatched = max(0, n1 - n2)
        plan["matched_qty"] = min(n1, n2)
        plan["unmatched_qty"] = unmatched
        plan["exposure_qty"] = (n1 - n2) * (1 if plan["direction"] == "buy_sell" else -1)
        cfg = plan["config"]
        first_low, first_high = map(_decimal, plan["first_price"])
        if plan["direction"] == "buy_sell":
            plan["reserved_qty"] = unmatched + remaining
            reserve = first_high * remaining + (order_fee("buy", first_high, remaining, cfg, trade_date=plan["trade_date"]) if remaining else 0)
            reserve += _decimal(cfg["slippage_per_share"]) * remaining
        else:
            plan["reserved_qty"] = remaining
            cover = _decimal(plan.get("cover_guard") or plan["second_price"][1])
            future_cost = cover * remaining + (order_fee("buy", cover, remaining, cfg, trade_date=plan["trade_date"]) if remaining else 0)
            future_proceeds = first_low * remaining - (order_fee("sell", first_low, remaining, cfg, trade_date=plan["trade_date"]) if remaining else 0)
            cover_actual = cover * unmatched + (order_fee("buy", cover, unmatched, cfg, trade_date=plan["trade_date"]) if unmatched else 0)
            reserve = max(Decimal(0), future_cost - future_proceeds) + cover_actual
            reserve += _decimal(cfg["slippage_per_share"]) * (remaining + unmatched) * 2
        if remaining or (unmatched and plan["direction"] == "sell_buy"):
            reserve += _decimal(cfg.get("risk_cash_buffer", 0))
        plan["reserved_cash"] = _money(reserve)
        plan["matched_pnl_provisional"], plan["matches"] = self._matches(first, second, plan["direction"])
        plan["fees_unknown"] = any(x.get("fee_unverified") for x in executions)
        plan["matched_pnl"] = None if plan["fees_unknown"] else plan["matched_pnl_provisional"]
        fee_warning = "成交日费用合同已失效，现金与预留仅临时估值；请登记实际费用并核对账户"
        plan["reasons"] = [r for r in plan["reasons"] if r != fee_warning]
        if plan["fees_unknown"]:
            plan["reasons"].append(fee_warning)
        if n1 == n2 and not remaining and not pending[2]:
            plan["state"] = "completed" if n1 else "cancelled"
            plan["reserved_cash"] = plan["reserved_qty"] = 0
        elif remaining:
            plan["state"] = "first_partial" if n1 else "first_pending"
        else:
            plan["state"] = "second_partial" if n2 else "second_pending"
        if n2 > n1:
            plan["state"] = "exposure"
        plan["legs"] = [{"leg": leg, "side": ("buy" if (plan["direction"] == "buy_sell") == (leg == 1) else "sell"),
                          "filled": n1 if leg == 1 else n2, "pending_order_qty": pending[leg]} for leg in (1, 2)]
        if plan.get("pending_proceeds", 0) > 0 and "回补资金待核对" not in plan["reasons"]:
            plan["reasons"].append("回补资金待核对")
        plan["updated_at"] = now.isoformat()

    @staticmethod
    def _matches(first, second, direction):
        left = [{**x, "remaining": x["quantity"]} for x in sorted(first, key=lambda x: (x["executed_at"], x["id"]))]
        right = [{**x, "remaining": x["quantity"]} for x in sorted(second, key=lambda x: (x["executed_at"], x["id"]))]
        i = j = 0
        net = Decimal(0)
        matches = []
        while i < len(left) and j < len(right):
            a, b = left[i], right[j]
            q = min(a["remaining"], b["remaining"])
            gross = (_decimal(b["price"]) - _decimal(a["price"])) * q * (1 if direction == "buy_sell" else -1)
            fee = _decimal(a["allocated_fee"]) * q / a["quantity"] + _decimal(b["allocated_fee"]) * q / b["quantity"]
            pnl = gross - fee
            net += pnl
            matches.append({"first_execution_id": a["id"], "second_execution_id": b["id"], "quantity": q,
                            "gross": _money(gross), "fees": None if a.get("fee_unverified") or b.get("fee_unverified") else _money(fee),
                            "net": None if a.get("fee_unverified") or b.get("fee_unverified") else _money(pnl),
                            "provisional_fees": _money(fee), "provisional_net": _money(pnl)})
            a["remaining"] -= q
            b["remaining"] -= q
            i += a["remaining"] == 0
            j += b["remaining"] == 0
        return _money(net), matches

    def stop_first_leg(self, plan_id, expected_version=None, now=None):
        now = _now(now)
        with self._tx() as db:
            plan = self._get(db, "plans", plan_id)
            self._check_version(plan, expected_version)
            plan["first_stopped"] = True
            plan["version"] += 1
            self._refresh_plan(db, plan, now)
            self._save_plan(db, plan)
            account = self._get(db, "accounts", plan["account_id"])
            self._event(db, account, "first_leg_stopped", {}, now, plan_id=plan_id)
            return plan

    def record_order_event(self, payload, now=None):
        now = _now(now)
        event_key = payload.get("event_id")
        if not event_key or payload.get("status") not in {"submitted", "partially_filled", "filled", "cancel_pending", "cancelled", "rejected"}:
            raise LedgerError("委托回报需要唯一事件标识和合法状态")
        if payload.get("leg") not in {1, 2} or not payload.get("order_id"):
            raise LedgerError("委托回报需指定交易腿和委托编号")
        with self._tx() as db:
            plan = self._get(db, "plans", payload["plan_id"])
            key = f"order:{plan['account_id']}:{event_key}"
            if db.execute("SELECT 1 FROM events WHERE request_key=?", (key,)).fetchone():
                return plan
            existing = db.execute("SELECT payload FROM orders WHERE plan_id=? AND order_id=?", (plan["id"], payload["order_id"])).fetchone()
            old = json.loads(existing["payload"]) if existing else None
            if old and old["leg"] != payload["leg"]:
                raise LedgerError("同一委托不能属于两笔交易")
            order = {"quantity": 0, "cumulative_filled": 0, "cancelled_quantity": 0,
                     "occurred_at": now.isoformat(), **(old or {}), **payload}
            for field in ("quantity", "cumulative_filled", "cancelled_quantity"):
                _quantity(order[field], field)
                order[field] = max(order[field], (old or {}).get(field, 0))
            if old and (old["status"] in TERMINAL_ORDERS or
                        (old["status"] == "cancel_pending" and order["status"] not in TERMINAL_ORDERS)):
                order["status"] = old["status"]
            if order["status"] == "rejected":
                # 已拒单没有可继续执行的余量；已报告成交仍必须等逐笔对齐。
                order["cancelled_quantity"] = max(order["cancelled_quantity"], order["quantity"] - order["cumulative_filled"])
            db.execute("INSERT INTO orders VALUES(?,?,?) ON CONFLICT(plan_id,order_id) DO UPDATE SET payload=excluded.payload",
                       (plan["id"], order["order_id"], _encode(order)))
            self._refresh_plan(db, plan, now)
            plan["version"] += 1
            self._save_plan(db, plan)
            account = self._get(db, "accounts", plan["account_id"])
            if sum(o["quantity"] for o in self._orders(db, plan["id"]) if o["leg"] == 1) > plan["quantity"]:
                account["needs_reconciliation"] = True
            self._event(db, account, "order_event", payload, now, plan_id=plan["id"], request_key=key)
            return plan

    def record_execution(self, payload, now=None):
        now = _now(now)
        quantity = _quantity(payload.get("quantity"), zero=False)
        price = _decimal(payload.get("price"))
        if price <= 0 or payload.get("leg") not in {1, 2} or not payload.get("idempotency_key") or not payload.get("order_id"):
            raise LedgerError("成交需指定正价格、交易腿、委托编号和幂等键")
        with self._tx() as db:
            repeated = db.execute("SELECT payload FROM executions WHERE request_key=?", (payload["idempotency_key"],)).fetchone()
            if not repeated and payload.get("id"):
                repeated = db.execute("SELECT payload FROM executions WHERE id=?", (payload["id"],)).fetchone()
            if repeated:
                execution = json.loads(repeated["payload"])
                if any(execution[key] != payload.get(key) for key in ("plan_id", "leg", "order_id", "quantity")) or _decimal(execution["price"]) != price:
                    raise LedgerError("同一幂等键不能用于不同成交事实", "idempotency_conflict", 409)
                return {"plan": self._get(db, "plans", execution["plan_id"]), "execution": execution}
            plan = self._get(db, "plans", payload["plan_id"])
            account = self._get(db, "accounts", plan["account_id"])
            position = self._position(db, account["id"], plan["symbol"])
            side = "buy" if (plan["direction"] == "buy_sell") == (payload["leg"] == 1) else "sell"
            existing = self._executions(db, plan["id"])
            executed_at = _now(payload.get("executed_at", now))
            fee_day = executed_at.date().isoformat()
            group = [x for x in existing if x["order_id"] == payload["order_id"]
                     and _now(x["executed_at"]).date().isoformat() == fee_day]
            if group and group[0]["leg"] != payload["leg"]:
                raise LedgerError("同一委托不能属于两笔交易")
            original_fee = sum((_decimal(x["allocated_fee"]) for x in group), Decimal(0))
            fee_input = payload.get("fee")
            if fee_input is not None and _decimal(fee_input) < 0:
                raise LedgerError("成交费用不能为负")
            execution = {"id": payload.get("id") or _id("execution"), "plan_id": plan["id"],
                "leg": payload["leg"], "side": side, "order_id": payload["order_id"], "quantity": quantity,
                "price": float(price), "executed_at": executed_at.isoformat(),
                "received_at": now.isoformat(), "fee_input": fee_input, "fee": fee_input,
                "allocated_fee": 0, "fee_estimated": fee_input is None,
                "proceeds_pending": side == "sell" and not payload.get("proceeds_available", account["mode"] == "simulation"),
                "idempotency_key": payload["idempotency_key"]}
            group.append(execution)
            group_fee = self._allocate_fees(group, side, plan["config"], fee_day)
            for item in group:
                if item["id"] != execution["id"]:
                    db.execute("UPDATE executions SET payload=? WHERE id=?", (_encode(item), item["id"]))
            fee_delta = group_fee - original_fee
            cash_delta = (-price * quantity - fee_delta) if side == "buy" else (price * quantity - fee_delta)
            issues = []
            if execution["fee_unverified"]:
                account["cash_estimated"] = True
                account["benchmark_valid"] = False
                issues.append("成交日期没有适用费用合同，请登记实际费用并核对账户")
            conflicts = [p for p in self._active_plans(db, account["id"])
                         if p["symbol"] == plan["symbol"] and p["id"] != plan["id"]]
            if conflicts:
                issues.append("迟到成交与同股另一计划冲突，停止新计划并核对")
            if payload["leg"] == 1 and plan["leg1_filled"] + quantity > plan["quantity"]:
                issues.append("第一笔实际成交超过计划数量")
            if payload["leg"] == 2 and plan["leg2_filled"] + quantity > plan["leg1_filled"]:
                issues.append("第二笔实际成交超过第一笔未匹配数量")
            if _now(execution["executed_at"]).date().isoformat() != account["trade_date"]:
                issues.append("成交日期与账户快照日期不一致，请核对交易日库存")
            if side == "buy":
                position["qty"] += quantity
                position["today_bought"] += quantity
            else:
                position["qty"] -= quantity
                position["today_sold"] += quantity
                if position["sellable_qty"] is None:
                    issues.append("卖出时可卖库存未知")
                else:
                    position["sellable_qty"] -= quantity
                    if position["sellable_qty"] < 0:
                        issues.append("卖出超过登记可卖库存")
                if position["qty"] < position["core_qty"]:
                    issues.append("实际持仓低于核心下限")
            proceeds_available = payload.get("proceeds_available", account["mode"] == "simulation")
            if side == "sell" and not proceeds_available:
                account["pending_proceeds"] = _money(_decimal(account["pending_proceeds"]) + cash_delta)
                plan["pending_proceeds"] = _money(_decimal(plan["pending_proceeds"]) + cash_delta)
            elif account["available_cash"] is None:
                issues.append("实际成交时可用资金未知")
            else:
                account["available_cash"] = _money(_decimal(account["available_cash"]) + cash_delta)
                if account["available_cash"] < 0:
                    issues.append("实际成交后登记现金不足")
            if issues:
                account["needs_reconciliation"] = True
                plan["reasons"] = list(dict.fromkeys([*plan["reasons"], *issues]))
            position["version"] += 1
            self._save_position(db, position)
            db.execute("INSERT INTO executions VALUES(?,?,?,?)", (execution["id"], plan["id"], payload["idempotency_key"], _encode(execution)))
            self._refresh_plan(db, plan, now)
            plan["version"] += 1
            self._save_plan(db, plan)
            self._event(db, account, "execution", payload, now, plan_id=plan["id"], request_key=f"execution:{payload['idempotency_key']}")
            self._notify(db, f"execution:{execution['id']}", {"kind": "execution", "plan_id": plan["id"],
                         "account_id": account["id"], "symbol": plan["symbol"], "unmatched_qty": plan["unmatched_qty"],
                         "created_at": now.isoformat()}, now)
            return {"plan": plan, "execution": execution}

    def correct_execution(self, execution_id, payload, now=None):
        """追加实际费用核对；数量/价格错误须先账户核对，首版不静默冲销交易事实。"""
        now = _now(now)
        if not payload.get("idempotency_key") or not str(payload.get("reason", "")).strip():
            raise LedgerError("费用核对必须填写原因与幂等键")
        if "quantity" in payload or "price" in payload or payload.get("fee") is None:
            raise LedgerError("首版更正入口仅核对实际费用；股数或价格错误需通过账户核对处理")
        fee = _money(payload["fee"])
        if fee < 0:
            raise LedgerError("成交费用不能为负")
        with self._tx() as db:
            execution = self._get(db, "executions", execution_id)
            plan = self._get(db, "plans", execution["plan_id"])
            key = f"fee_correction:{payload['idempotency_key']}"
            if db.execute("SELECT 1 FROM events WHERE request_key=?", (key,)).fetchone():
                return {"plan": plan, "execution": execution}
            account = self._get(db, "accounts", plan["account_id"])
            fee_day = _now(execution["executed_at"]).date().isoformat()
            group = [x for x in self._executions(db, plan["id"]) if x["order_id"] == execution["order_id"]
                     and _now(x["executed_at"]).date().isoformat() == fee_day]
            old_allocations = {x["id"]: _decimal(x["allocated_fee"]) for x in group}
            target = next(x for x in group if x["id"] == execution_id)
            old_input = target["fee_input"]
            target["fee_input"] = target["fee"] = fee
            target["fee_reconciled_at"] = now.isoformat()
            self._allocate_fees(group, execution["side"], plan["config"], fee_day)
            for item in group:
                delta = _decimal(item["allocated_fee"]) - old_allocations[item["id"]]
                if item.get("proceeds_pending"):
                    account["pending_proceeds"] = _money(_decimal(account["pending_proceeds"]) - delta)
                    plan["pending_proceeds"] = _money(_decimal(plan["pending_proceeds"]) - delta)
                elif account["available_cash"] is not None:
                    account["available_cash"] = _money(_decimal(account["available_cash"]) - delta)
                else:
                    account["needs_reconciliation"] = True
                db.execute("UPDATE executions SET payload=? WHERE id=?", (_encode(item), item["id"]))
            if account["available_cash"] is not None and account["available_cash"] < 0:
                account["needs_reconciliation"] = True
            plan["version"] += 1
            self._refresh_plan(db, plan, now)
            self._save_plan(db, plan)
            self._event(db, account, "execution_fee_correction", {"execution_id": execution_id,
                        "previous_fee_input": old_input, **payload}, now, plan_id=plan["id"], request_key=key)
            return {"plan": plan, "execution": target}

    def update_plan_monitor(self, plan_id, patch, now=None):
        """只更新派生监控信息，不改成交、预留或账户版本。风险提醒写入同一事务。"""
        now = _now(now)
        with self._tx() as db:
            plan = self._get(db, "plans", plan_id)
            monitor = {**plan.get("monitor", {}), **patch, "computed_at": now.isoformat()}
            plan["monitor"] = monitor
            self._save_plan(db, plan)
            risks = sorted(set(monitor.get("risk_codes") or []))
            if plan["unmatched_qty"] and now.strftime("%H:%M") >= "15:00":
                risks = sorted(set([*risks, "overnight_exposure"]))
            if risks and plan["state"] not in INACTIVE_PLANS:
                key = f"plan:{plan_id}:{now.date().isoformat()}:{','.join(risks)}"
                self._notify(db, key, {"kind": "plan_risk", "account_id": plan["account_id"],
                    "plan_id": plan_id, "symbol": plan["symbol"], "direction": plan["direction"],
                    "risk_codes": risks, "unmatched_qty": plan["unmatched_qty"],
                    "exposure_qty": plan["exposure_qty"], "monitor": monitor}, now)
            return plan

    def plan_detail(self, plan_id):
        with self._tx() as db:
            return {"plan": self._get(db, "plans", plan_id), "executions": self._executions(db, plan_id),
                    "events": [dict(row) | {"payload": json.loads(row["payload"])} for row in db.execute(
                        "SELECT * FROM events WHERE plan_id=? ORDER BY id", (plan_id,)).fetchall()]}

    @staticmethod
    def _economic_cash(account):
        if account["available_cash"] is None:
            return None
        return _decimal(account["available_cash"]) + _decimal(account["pending_proceeds"]) + (
            _decimal(account["broker_frozen_cash"]) if account["cash_net_of_frozen"] else 0)

    def review(self, account_id, date=None, prices=None):
        prices = prices or {}
        with self._tx() as db:
            account = self._get(db, "accounts", account_id)
            positions = self._rows(db, "SELECT payload FROM positions WHERE account_id=?", (account_id,))
            all_plans = self._rows(db, "SELECT payload FROM plans WHERE account_id=?", (account_id,))
            plans = [p for p in all_plans if date is None or p["trade_date"] == date]
            selected_ids = {p["id"] for p in plans}
            executions = [x for p in plans for x in self._executions(db, p["id"])]
            fees_unknown = any(x.get("fee_unverified") for x in executions)
            provisional_pnl = _money(sum((_decimal(p.get("matched_pnl_provisional", p["matched_pnl"])) for p in plans), Decimal(0)))
            provisional_fees = _money(sum((_decimal(x["allocated_fee"]) for x in executions), Decimal(0)))
            missing = [p["symbol"] for p in positions if prices.get(p["symbol"]) is None]
            equity = benchmark = increment = None
            if not missing and self._economic_cash(account) is not None and account["benchmark_valid"]:
                equity = _money(self._economic_cash(account) + sum((_decimal(prices[p["symbol"]]) * p["qty"] for p in positions), Decimal(0)))
                benchmark = _money(_decimal(account["baseline_cash"]) + sum((_decimal(prices[p["symbol"]]) * p["baseline_qty"] for p in positions), Decimal(0)))
                increment = _money(_decimal(equity) - _decimal(benchmark))
            started = [p for p in plans if p["leg1_filled"]]
            completed = [p for p in started if p["state"] == "completed"]
            unfinished = [p for p in plans if p["unmatched_qty"] or p["exposure_qty"]]
            return {"account_id": account_id, "date": date, "matched_pnl": None if fees_unknown else provisional_pnl,
                    "matched_pnl_provisional": provisional_pnl,
                    "fees": None if fees_unknown else provisional_fees, "fees_provisional": provisional_fees,
                    "fees_unknown": fees_unknown,
                    "unmatched_qty": sum(p["unmatched_qty"] for p in plans), "unfinished_plans": unfinished,
                    "plan_count": len(plans), "started_count": len(started), "completed_count": len(completed),
                    "completion_rate": len(completed) / len(started) if started else None,
                    "unfinished_ratio": len(unfinished) / len(started) if started else None,
                    "total_equity": equity, "hold_equity": benchmark, "incremental_equity": increment,
                    "valuation_missing": missing, "benchmark_valid": account["benchmark_valid"],
                    "valuation_scope": "当前账户权益；date 仅筛选计划，非历史收盘估值",
                    "sample_size": len(selected_ids), "probability": None, "out_of_sample": None,
                    "max_drawdown": None, "max_adverse_excursion": None,
                    "limitations": ["人工数据待券商核对", "缺少权益轨迹时不输出回撤", "未经样本外验证，不代表盈利概率"]}

    @staticmethod
    def _notify(db, key, payload, now):
        db.execute("INSERT OR IGNORE INTO notifications VALUES(?,?,0,?)", (_id("notification"), key,
                   _encode({"created_at": now.isoformat(), **payload})))

    def pending_notifications(self, limit=100):
        with self._tx() as db:
            return [dict(row) | {"payload": json.loads(row["payload"])} for row in db.execute(
                "SELECT * FROM notifications WHERE delivered=0 ORDER BY rowid LIMIT ?", (max(1, min(limit, 1000)),)).fetchall()]

    def ack_notification(self, notification_id):
        with self._tx() as db:
            db.execute("UPDATE notifications SET delivered=1 WHERE id=?", (notification_id,))

    def save_observation(self, account_id, symbol, observation):
        minute_key = observation.get("minute_key") or observation.get("computed_at") or observation.get("calculated_at")
        if not minute_key:
            raise LedgerError("观察样本缺少计算时间")
        trade_date = observation.get("trade_date") or _now(minute_key).date().isoformat()
        with self._tx() as db:
            db.execute("INSERT INTO observations VALUES(?,?,?,?,?) ON CONFLICT(account_id,symbol,minute_key) DO NOTHING",
                       (account_id, symbol, str(minute_key), trade_date, _encode(observation)))

    def list_observations(self, account_id, symbol, start_date, end_date):
        with self._tx() as db:
            return self._rows(db, "SELECT payload FROM observations WHERE account_id=? AND symbol=? AND trade_date>=? AND trade_date<=? ORDER BY minute_key",
                              (account_id, symbol, start_date, end_date))
