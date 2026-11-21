"""首板机会记录的收盘结果与模拟成交核验: 只做纯计算, 证据不足一律返回待核验, 不用盘中状态顶替。"""
from __future__ import annotations

import math
from collections.abc import Iterable
from datetime import date

import polars as pl

from app.strategy.first_board import observation_limit_price

_TOLERANCE = 0.005
_FILLED = {"open", "partially_closed", "closed"}
_PAPER_KNOWN = {"not_ordered", "pending", "cancelled", "expired", "open", "partially_closed", "closed"}


def _finite(value: object) -> float | None:
    if type(value) not in (int, float) or not math.isfinite(value):
        return None
    return float(value)


def _unverified(reason: str) -> dict:
    return {"status": "unverified", "close_price": None, "limit_price": None, "reason": reason}


def _close_one(symbol: str, day: date, row: dict) -> dict:
    close, high = _finite(row.get("raw_close")), _finite(row.get("raw_high"))
    if close is None or close <= 0 or high is None or high <= 0:
        return _unverified("当日日线缺少原价收盘价或最高价")
    limit = observation_limit_price(symbol, day, _finite(row.get("raw_prev_close")), row, historical=True)
    if limit is None:
        return _unverified("缺少可靠的涨停价")
    if close > limit + _TOLERANCE or high > limit + _TOLERANCE or close > high + _TOLERANCE:
        return _unverified("日线价格自相矛盾")
    return {"status": "sealed" if abs(close - limit) <= _TOLERANCE else "not_sealed",
            "close_price": close, "limit_price": limit, "reason": None}


def close_results(daily: pl.DataFrame, symbols: Iterable[str], day: date, *, closed: bool) -> dict[str, dict]:
    """按当日日线判定收盘是否封板。日线缺失、价格缺失或自相矛盾、尚未收盘都是待核验, 不记为未封。"""
    symbols = list(symbols)
    if not closed:
        return {symbol: _unverified("当日尚未收盘") for symbol in symbols}
    if daily is None or daily.is_empty() or not {"symbol", "date"}.issubset(daily.columns):
        return {symbol: _unverified("缺少当日日线") for symbol in symbols}
    today = daily.with_columns(pl.col("date").cast(pl.Date, strict=False)).filter(pl.col("date") == day)
    rows: dict[str, list[dict]] = {}
    for row in today.iter_rows(named=True):
        rows.setdefault(row["symbol"], []).append(row)
    result = {}
    for symbol in symbols:
        found = rows.get(symbol, [])
        result[symbol] = (_unverified("缺少当日日线") if not found
                          else _unverified("当日日线重复") if len(found) > 1 else _close_one(symbol, day, found[0]))
    return result


def _paper(status: str, reason: str | None = None, net_return: float | None = None) -> dict:
    return {"status": status, "reason": reason, "net_return": net_return}


def paper_results(audit: dict | None, alerted_symbols: Iterable[str], *, has_account: bool) -> dict[str, dict]:
    """把按买入事件归因的审计结果汇总到股票。多张买单只标来源不唯一, 不挑其中一张。"""
    symbols = list(alerted_symbols)
    if not has_account:
        return {symbol: _paper("no_account", "未关联模拟账户") for symbol in symbols}
    if audit is None or audit.get("status") in {"unavailable", "error"} or audit.get("coverage", {}).get("truncated"):
        reason = (audit or {}).get("reason") or "未取得模拟盘核验结果"
        if audit and audit.get("coverage", {}).get("truncated"):
            reason = "买入候选事件过多, 明细被截断, 不能可靠归因"
        return {symbol: _paper("evidence_missing", reason) for symbol in symbols}
    result = {}
    for symbol in symbols:
        trades = [trade for trade in audit.get("trades", []) if trade.get("symbol") == symbol]
        if not trades:
            result[symbol] = _paper("no_signal")
            continue
        ordered = [trade for trade in trades if any(order.get("side") == "buy" for order in trade.get("orders", []))]
        buys = sum(order.get("side") == "buy" for trade in ordered for order in trade["orders"])
        if buys > 1:
            result[symbol] = _paper("ambiguous", "同一股票存在多张买单, 来源不唯一")
        elif buys == 1:
            trade = ordered[0]
            status = trade["status"] if trade.get("status") in _PAPER_KNOWN else "evidence_missing"
            result[symbol] = _paper(status, trade.get("reason"), trade.get("closed_net_return") if status == "closed" else None)
        elif all(trade.get("status") == "not_ordered" for trade in trades):
            result[symbol] = _paper("not_ordered", trades[0].get("reason"))
        else:
            missing = next(trade for trade in trades if trade.get("status") != "not_ordered")
            result[symbol] = _paper("evidence_missing", missing.get("reason"))
    return result


def apply_verification(report: dict, close: dict[str, dict], paper: dict[str, dict]) -> dict:
    """把核验结果并入机会记录报告并补汇总计数, 返回同一份报告。"""
    sealed = checked = filled = paper_checked = 0
    for row in report["rows"]:
        row["close"] = close.get(row["symbol"]) or _unverified("缺少当日日线")
        row["paper"] = paper.get(row["symbol"]) or _paper("no_signal")
        sealed += row["close"]["status"] == "sealed"
        checked += row["close"]["status"] != "unverified"
        filled += row["paper"]["status"] in _FILLED
        paper_checked += bool(row.get("alerted")) and row["paper"]["status"] in _PAPER_KNOWN
    report["summary"].update(close_sealed_count=sealed, close_checked_count=checked,
                             paper_filled_count=filled, paper_checked_count=paper_checked)
    report["verified"] = True
    return report
