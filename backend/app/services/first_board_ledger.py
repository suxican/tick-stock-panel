"""Read-only evidence and FIFO projection over the existing paper ledger.

No account creation, migration, order matching, or second ledger is performed.
The aggregate and per-lot corporate-action rounding follow paper.replay_positions.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict, deque
from datetime import date
from itertools import islice
from pathlib import Path

from app.strategy import paper

MAX_ORDERS = 20_000
MAX_FILLS = 100_000
MAX_LEDGER_BYTES = 64 * 1024 * 1024


class LedgerError(ValueError):
    """Evidence cannot support a reliable financial result."""


class LedgerUnavailableError(LedgerError):
    """No selected account exists; unlike malformed evidence, this is not corruption."""


def number(value: object, *, positive: bool = False) -> float:
    if isinstance(value, bool):
        raise LedgerError("账本数量或金额不是有效数值")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise LedgerError("账本数量或金额缺失或无效") from exc
    if not math.isfinite(result) or (positive and result <= 0):
        raise LedgerError("账本数量或金额超出有效范围")
    return result


def trading_date(value: object) -> str:
    try:
        if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
            raise ValueError
    except (TypeError, ValueError) as exc:
        raise LedgerError("账本交易日期缺失或无效") from exc
    return value


def _json(raw: bytes, label: str) -> dict:
    try:
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError
        json.dumps(result, allow_nan=False)
        return result
    except (UnicodeError, ValueError, TypeError) as exc:
        raise LedgerError(f"{label}损坏，不能跳过记录后统计收益") from exc


def read_ledger_snapshot(data_dir: Path, account_id: str) -> dict:
    """Strict, bounded read of modern or legacy layout, without paper._root side effects."""
    try:
        paper.validate_account_id(account_id)
    except ValueError as exc:
        raise LedgerError("模拟账户标识无效") from exc
    base = Path(data_dir).resolve()
    modern = paper.accounts_base(base) / account_id
    legacy = base / "paper"
    root = modern
    if (not (modern / "account.json").exists() and account_id == paper.DEFAULT_ACCOUNT_ID
            and (legacy / "account.json").exists() and not paper.accounts_base(base).exists()):
        root = legacy
    if not root.resolve().is_relative_to(base):
        raise LedgerError("模拟账户路径越界")
    account_path = root / "account.json"
    if not account_path.exists():
        raise LedgerUnavailableError("所选模拟账户不存在")
    try:
        # Bounded snapshot acquisition only; all FIFO and report work happens outside this lock.
        with paper.PAPER_LOCK:
            orders = sorted(islice((root / "orders").glob("order_*.json"), MAX_ORDERS + 1))
            if len(orders) > MAX_ORDERS:
                raise LedgerError("模拟账户订单数量超过审计上限")
            paths = [account_path, *orders]
            for name in ("fills.jsonl", "positions.json"):
                if (root / name).exists():
                    paths.append(root / name)
            before = {p: (p.stat().st_mtime_ns, p.stat().st_size) for p in paths}
            digest = hashlib.sha256()
            documents = {}
            budget = MAX_LEDGER_BYTES
            for path in paths:
                if not path.resolve().is_relative_to(root.resolve()):
                    raise LedgerError("模拟账本文件路径越界")
                with path.open("rb") as handle:
                    raw = handle.read(budget + 1)
                budget -= len(raw)
                if budget < 0:
                    raise LedgerError("模拟账本大小超过审计上限")
                key = path.relative_to(root).as_posix()
                digest.update(key.encode())
                digest.update(b"\0")
                digest.update(raw)
                documents[key] = raw
            after = {p: (p.stat().st_mtime_ns, p.stat().st_size) for p in paths}
            if before != after or orders != sorted(islice((root / "orders").glob("order_*.json"), MAX_ORDERS + 1)):
                raise LedgerError("模拟账本在读取期间发生变化，请重试")
        account = _json(documents["account.json"], "模拟账户文件")
        if account.get("id", paper.DEFAULT_ACCOUNT_ID) != account_id:
            raise LedgerError("模拟账户文件与所选账户不一致")
        number(account.get("initial_cash"), positive=True)
        number(account.get("cash"))
        records = []
        for line in documents.get("fills.jsonl", b"").splitlines():
            if not line.strip():
                continue
            if len(records) >= MAX_FILLS:
                raise LedgerError("模拟成交记录数量超过审计上限")
            records.append(_json(line, "模拟成交记录"))
        positions = None
        if "positions.json" in documents:
            positions = _json(documents["positions.json"], "模拟持仓文件").get("positions")
            if not isinstance(positions, dict):
                raise LedgerError("模拟持仓结构无效")
        return {"account": account, "orders": [_json(documents[p.relative_to(root).as_posix()], "模拟订单") for p in orders],
                "fills": records, "positions": positions, "fingerprint": digest.hexdigest(),
                "legacy_layout": root == legacy}
    except OSError as exc:
        raise LedgerError("模拟账本无法完整读取，请检查文件状态后重试") from exc


def project_fifo(fills: list[dict], *, through_day: str | None = None, enforce_t1: bool = False) -> dict:
    """Project actual fills, preserving entry identity and cost for every FIFO match.

    Pending orders do not consume lots. File order is authoritative; seq timestamps
    need not be unique. Missing historical fee/kind follows the existing paper contract.
    """
    pools: dict[str, deque] = defaultdict(deque)
    aggregate = defaultdict(float)
    last_days = defaultdict(str)
    actions = set()
    entries, matches, normalized = [], [], []
    legacy_fees = 0
    for index, fill in enumerate(fills):
        if not isinstance(fill, dict) or not isinstance(fill.get("symbol"), str) or not fill["symbol"]:
            raise LedgerError("成交记录缺少股票标识")
        symbol = fill["symbol"]
        day = trading_date(fill.get("date"))
        if day < last_days[symbol] or (through_day is not None and day > through_day):
            raise LedgerError("成交记录日期顺序无效")
        last_days[symbol] = day
        kind = fill.get("kind", "fill")
        if kind == "corp_action":
            if (symbol, day) in actions:
                raise LedgerError("同一股票同日公司行动记录重复")
            actions.add((symbol, day))
            if "qty_before" in fill and not math.isclose(number(fill["qty_before"]), aggregate[symbol],
                                                         rel_tol=0, abs_tol=1e-6):
                raise LedgerError("公司行动前数量与 FIFO 重放不一致")
            factor = number(fill.get("factor"), positive=True)
            for lot in pools[symbol]:
                lot["qty"] = number(round(lot["qty"] * factor, 6), positive=True)
            aggregate[symbol] = number(round(aggregate[symbol] * factor, 6))
            continue
        if kind != "fill" or fill.get("side") not in {"buy", "sell"}:
            raise LedgerError("成交记录类型不受支持")
        qty = number(fill.get("qty"), positive=True)
        price = number(fill.get("price"), positive=True)
        fee = number(fill.get("fee", 0))
        if qty % 1 or fee < 0:
            raise LedgerError("成交数量或费用无效")
        cost = number(qty * price + fee, positive=True)
        fee_defaulted = "fee" not in fill
        legacy_fees += fee_defaulted
        row = {"ledger_index": index, "order_id": fill.get("order_id"), "symbol": symbol,
               "side": fill["side"], "date": day, "ts": fill.get("ts"), "qty": qty,
               "price": price, "fee": fee, "fee_defaulted": fee_defaulted}
        normalized.append(row)
        if fill["side"] == "buy":
            lot = {"symbol": symbol, "date": day, "qty": qty, "cost": cost, "entry_cost": cost,
                   "order_id": fill.get("order_id"), "fill_index": index, "ts": fill.get("ts")}
            pools[symbol].append(lot)
            entries.append(lot)
            aggregate[symbol] = number(aggregate[symbol] + qty)
            continue
        remain = qty
        aggregate[symbol] = max(0, aggregate[symbol] - qty)
        while remain > 1e-9 and pools[symbol]:
            lot = pools[symbol][0]
            if enforce_t1 and lot["date"] >= day:
                raise LedgerError("成交台账违反 T+1 可卖约束")
            take = min(remain, lot["qty"])
            allocated_cost = lot["cost"] * take / lot["qty"]
            allocated_fee = fee * take / qty
            matches.append({"symbol": symbol, "buy_order_id": lot["order_id"],
                            "buy_fill_index": lot["fill_index"], "sell_order_id": fill.get("order_id"),
                            "sell_fill_index": index, "qty": take, "date": day, "ts": fill.get("ts"),
                            "price": price, "allocated_buy_cost": allocated_cost,
                            "allocated_sell_fee": allocated_fee,
                            "net_pnl": number(take * price - allocated_fee - allocated_cost)})
            lot["qty"] -= take
            lot["cost"] -= allocated_cost
            remain -= take
            if lot["qty"] <= 1e-9:
                lot["qty"], lot["cost"] = 0., 0.
                pools[symbol].popleft()
        if remain > 1e-9:
            raise LedgerError("成交台账卖出数量超过 FIFO 持仓")
    return {"lots": {symbol: list(pool) for symbol, pool in pools.items()}, "aggregate_qty": dict(aggregate),
            "entries": entries, "matches": matches, "fills": normalized, "legacy_fee_default_count": legacy_fees}


def position_matches(lots: list[dict], aggregate_qty: float, position: dict) -> bool:
    """Compare independent aggregate and per-lot rounding with materialized paper data."""
    try:
        stored = position.get("lots", [])
        if not isinstance(stored, list) or len(lots) != len(stored):
            return False
        total = number(position.get("qty"))
        if total < 0 or not math.isclose(aggregate_qty, total, rel_tol=0, abs_tol=1e-6):
            return False
        return all(isinstance(item, dict) and lot["date"] == item.get("date")
                   and math.isclose(lot["qty"], number(item.get("qty"), positive=True), rel_tol=0, abs_tol=1e-6)
                   for lot, item in zip(lots, stored, strict=True))
    except (LedgerError, TypeError, AttributeError):
        return False
