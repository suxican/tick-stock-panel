"""首板对现有模拟账户的只读视图、风险预算和退出提示。"""
from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, time
from pathlib import Path

from app.market_time import CN_TZ
from app.strategy import paper

_NOTE = "统计覆盖整个关联模拟账户, 并非首板策略独立归因; 退出提示不执行卖出。"


def _number(value: object, *, positive: bool = False) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
        if not math.isfinite(number) or (positive and number <= 0):
            return None
        return number
    except (ValueError, TypeError):
        return None


def portfolio_snapshot(data_dir: Path, account_id: str | None, prices: dict[str, float]) -> dict:
    """复用 paper 总览和回合统计, 缺行情时不把成本估值当作实时净值。"""
    empty = {"initialized": False, "account_id": account_id, "holdings": [],
             "stats": {"rounds": 0, "win_rate": None}, "note": _NOTE}
    if not account_id:
        return empty
    with paper.PAPER_LOCK:
        try:
            valid_prices = {symbol: value for symbol, price in prices.items()
                            if (value := _number(price, positive=True)) is not None}
            result = paper.overview(data_dir, valid_prices, account_id)
            if not result.get("initialized"):
                return empty
            statistics = paper.stats(data_dir, account_id)
            if not statistics.get("rounds"):
                statistics["win_rate"] = None
                statistics["avg_holding_days"] = None
            missing = []
            for holding in result["holdings"]:
                if holding["symbol"] not in valid_prices:
                    missing.append(holding["symbol"])
                    for field in ("last_price", "market_value", "pnl", "pnl_pct"):
                        holding[field] = None
            if missing:
                for field in ("market_value", "total", "total_pnl"):
                    result[field] = None
            result.update(stats=statistics, note=_NOTE, missing_prices=missing)
            return result
        except (ValueError, TypeError, KeyError, OSError):
            return {**empty, "error": "关联模拟账户数据不可用"}


def _limits(config: dict) -> dict | None:
    defaults = {"max_positions": 4, "max_stock_weight": 0.25,
                "max_sector_weight": 0.5, "total_exposure": 0.5}
    limits = {key: _number(config.get(key, default), positive=True) for key, default in defaults.items()}
    if any(value is None for value in limits.values()):
        return None
    if limits["max_positions"] % 1 or limits["max_positions"] > paper.MAX_POSITION_SYMBOLS:
        return None
    if any(limits[field] > 1 for field in ("max_stock_weight", "max_sector_weight", "total_exposure")):
        return None
    return limits


def buy_budget(
    data_dir: Path, account_id: str | None, *, symbol: str, prices: dict[str, float],
    sectors: dict[str, str], config: dict,
) -> dict:
    """按原价返回可买百股预算, 费用/滑点与 pending 买单均占用风险容量。

    本函数不会预占资金。调用方如要下单, 必须在同一 PAPER_LOCK 临界区内先
    计算预算再调用 paper.create_order, 否则两个独立预检之间仍存在竞争窗口。
    amount 是 qty 乘以原价, estimated_cost 才是含费和滑点的现金占用。
    """
    if not account_id:
        return {"allowed": True, "amount": None, "reasons": ["未关联模拟账户，仓位仅作提示"]}  # noqa: RUF001

    def denied(reason: str, **details) -> dict:
        return {"allowed": False, "amount": None, "qty": 0, "reasons": [reason], **details}

    limits = _limits(config)
    if limits is None:
        return denied("仓位上限配置无效")
    with paper.PAPER_LOCK:
        try:
            account = paper.get_account(data_dir, account_id)
            if not account:
                return denied("关联模拟账户不存在")
            if account.get("status") != "active":
                return denied("关联模拟账户已冻结")
            positions = paper.load_positions(data_dir, account_id)
            orders = paper.load_orders(data_dir, account_id)
            cash = _number(account.get("cash"))
            commission = _number(account.get("commission_pct"))
            slippage = _number(account.get("slippage_bps"))
            if (cash is None or cash < 0 or commission is None or not 0 <= commission <= 0.01
                    or slippage is None or not 0 <= slippage <= 200):
                return denied("账户现金或费用设置无效")
            quantities = {}
            for held_symbol, position in positions.items():
                qty = _number(position.get("qty"))
                if qty is None or qty < 0:
                    return denied("持仓数量无效")
                if qty > 0:
                    quantities[held_symbol] = qty
            pending = [order for order in orders
                       if order.get("status") == "pending" and order.get("side") == "buy"]
            symbols = {symbol, *quantities, *[str(order.get("symbol") or "") for order in pending]}
            raw_prices = {item: _number(prices.get(item), positive=True) for item in symbols}
            missing_prices = sorted(item for item, price in raw_prices.items() if price is None)
            if missing_prices:
                return denied("缺少候选、持仓或待成交标的的有效原价行情", missing_prices=missing_prices)
            missing_sectors = sorted(item for item in symbols
                                     if not isinstance(sectors.get(item), str) or not sectors[item].strip())
            if missing_sectors:
                return denied("缺少候选、持仓或待成交标的的行业归属", missing_sectors=missing_sectors)
            stock_values = defaultdict(float)
            sector_values = defaultdict(float)
            for held_symbol, qty in quantities.items():
                value = qty * raw_prices[held_symbol]
                stock_values[held_symbol] += value
                sector_values[sectors[held_symbol]] += value
            market_value = sum(stock_values.values())
            nav = cash + market_value
            if nav <= 0:
                return denied("账户净值不足")
            reserved = 0.0
            pending_symbols = set()
            for order in pending:
                pending_symbol = str(order.get("symbol") or "")
                qty = _number(order.get("qty"), positive=True)
                if qty is None or qty % paper.LOT_SIZE:
                    return denied("待成交买单数量无效")
                reference = _number(order.get("ref_price"), positive=True)
                estimate = paper.apply_slippage(max(reference or 0, raw_prices[pending_symbol]), "buy", slippage)
                cost = qty * estimate + paper.buy_fee(int(qty), estimate, commission)
                reserved += cost
                stock_values[pending_symbol] += cost
                sector_values[sectors[pending_symbol]] += cost
                pending_symbols.add(pending_symbol)
            active_symbols = set(quantities) | pending_symbols
            if symbol not in active_symbols and len(active_symbols) >= limits["max_positions"]:
                return denied("持仓及待成交买单的标的数已达到上限", reserved_cash=round(reserved, 2), nav=nav)
            capacity = min(
                cash - reserved,
                nav * limits["total_exposure"] - market_value - reserved,
                nav * limits["max_stock_weight"] - stock_values[symbol],
                nav * limits["max_sector_weight"] - sector_values[sectors[symbol]],
            )
            execution_price = paper.apply_slippage(raw_prices[symbol], "buy", slippage)
            # 同时满足比例佣金与最低佣金, 再按 paper 的百股规则向下取整。
            quantity_upper = min(capacity / (execution_price * (1 + commission)),
                                 (capacity - paper.MIN_COMMISSION) / execution_price)
            qty = paper.normalize_qty(max(0, int(quantity_upper)))
            if qty <= 0:
                return denied("扣除待成交占资及仓位上限后不足买入一手", reserved_cash=round(reserved, 2), nav=nav)
            cost = qty * execution_price + paper.buy_fee(qty, execution_price, commission)
            if cost > capacity + 1e-6:
                return denied("含费用预算超过剩余可用额度", reserved_cash=round(reserved, 2), nav=nav)
            return {"allowed": True, "amount": round(qty * raw_prices[symbol], 6), "qty": qty,
                    "estimated_cost": round(cost, 2), "nav": round(nav, 2),
                    "reserved_cash": round(reserved, 2),
                    "reasons": ["已计入持仓、待成交买单、百股数量、滑点和佣金"]}
        except (ValueError, TypeError, KeyError, OSError, OverflowError):
            return denied("关联模拟账户数据不可用, 暂停买入预算")


def exit_candidates(
    data_dir: Path, account_id: str | None, prices: dict[str, float], *, now: datetime,
    exit_time: str = "10:30", exit_loss_pct: float = 0.03,
) -> list[dict]:
    """仅提示与首板买单关联且 T+1 可卖的持仓, 不下单。"""
    if not account_id:
        return []
    local_now = now.astimezone(CN_TZ) if now.tzinfo else now.replace(tzinfo=CN_TZ)
    try:
        deadline = time.fromisoformat(exit_time)
    except (TypeError, ValueError):
        return []
    loss_limit = _number(exit_loss_pct, positive=True)
    if loss_limit is None or loss_limit >= 1 or deadline.tzinfo is not None:
        return []
    day = local_now.date().isoformat()
    with paper.PAPER_LOCK:
        try:
            if not paper.get_account(data_dir, account_id):
                return []
            orders = paper.load_orders(data_dir, account_id)
            positions = paper.load_positions(data_dir, account_id)
            source_ids = {order["id"] for order in orders if order.get("side") == "buy"
                          and order.get("status") == "filled"
                          and str(order.get("source") or "").startswith("first_board:")}
            # 复用成交台账的买入日期与现有剩余 lots 关联, 避免历史首板已清仓后
            # 又手动买入同一股票时仍被旧首板订单触发退出提示。
            source_days = defaultdict(set)
            for fill in paper.load_fills(data_dir, account_id):
                if fill.get("side") == "buy" and fill.get("order_id") in source_ids:
                    source_days[fill["symbol"]].add(fill["date"])
            pending_sells = defaultdict(int)
            for order in orders:
                if order.get("status") == "pending" and order.get("side") == "sell":
                    pending_sells[order["symbol"]] += int(order["qty"])
            result = []
            for symbol, position in positions.items():
                if not source_days[symbol]:
                    continue
                price = _number(prices.get(symbol), positive=True)
                average = _number(position.get("avg_cost"), positive=True)
                if price is None or average is None:
                    continue
                linked = sum(lot["qty"] for lot in position.get("lots", [])
                             if lot.get("date") in source_days[symbol] and lot["date"] < day)
                available = min(paper._available_of(position, day), int(linked)) - pending_sells[symbol]
                if available <= 0:
                    continue
                loss = price <= average * (1 - loss_limit) + 1e-9
                if not loss and local_now.time() < deadline:
                    continue
                result.append({"account_id": account_id, "symbol": symbol, "qty": available,
                               "price": price, "reason": "达到实验止损门槛, 待人工确认退出" if loss
                               else "已到次日退出时间, 待人工确认退出"})
            return result
        except (ValueError, TypeError, KeyError, OSError, OverflowError):
            return []
