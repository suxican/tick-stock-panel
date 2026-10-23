"""Bounded, read-only validation of recorded signals using a conservative proxy.

Observed minute endpoints are price proxies, not exchange fills or queue evidence.
No daily-price fallback, portfolio simulation, account mutation or remote fetching.
"""
from __future__ import annotations

import hashlib
import json
import math
from datetime import date, datetime, time, timedelta

import polars as pl
from pydantic import BaseModel, ConfigDict, Field

from app.market_time import CN_TZ
from app.services.first_board_attention import _validate_minutes
from app.services.first_board_portfolio import minimum_buy_qty
from app.services.huichun_calendar import BUNDLED_CALENDAR_PATH, load_market_calendar
from app.services.minute_adjust import minute_basis_is_raw
from app.strategy.paper import apply_slippage, buy_fee, sell_fee

GROUPS = ("S0F0", "S1F0", "S0F1", "S1F1", "unknown")
MAX_SIGNALS = 5000
MAX_DETAILS = 500
MINUTE_COLUMNS = {"symbol", "datetime", "open", "high", "low", "close", "volume", "amount"}
LIMITATIONS = [
    "验证对象仅为五分钟采样保存的首个可买候选; 不恢复采样间信号, can_buy包含当时账户预算门控。",
    "使用后续完整分钟收盘价代理及不利滑点, 不代表下一分钟开盘、真实成交或涨跌停排队撮合。",
    "独立等额交易忽略资金竞争和组合仓位, 不计算组合收益、年化或最大回撤, 不推导盈利结论。",
    "缺行情、原价证据、交易日历或公司行动现金流时保留不可验证状态; 仅闭合交易参与收益统计。",
    "费用为固定研究参数, 不自动重建历史税率或券商收费; 成交容量使用单根分钟成交量的比例假设。",
    "成交与退出信号时刻统一取分钟标签加1分钟的保守可用时刻; 分组取首次可买信号, 非信号分母只在总覆盖中展示。",
    "日线限价仅由已记录当日名称或日期匹配维表、上市日期和连续交易日前收重建; 历史风险标签缺失或冲突不以当前名称代替。",
]


class ExecutionOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, frozen=True, strict=True)
    amount: float = Field(default=10000., gt=0, le=100_000_000)
    commission_pct: float = Field(default=.0003, ge=0, le=.01)
    stamp_tax_pct: float = Field(default=.0005, ge=0, le=.05)
    slippage_bps: float = Field(default=5., ge=0, le=200)
    participation_rate: float = Field(default=.01, gt=0, le=.1)
    entry_window_minutes: int = Field(default=5, ge=3, le=30, strict=True)
    max_exit_sessions: int = Field(default=5, ge=1, le=10, strict=True)


def _number(value) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _stamp(value) -> datetime:
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None:
        raise ValueError("历史信号时间必须包含时区")
    return stamp.astimezone(CN_TZ)


def _digest(digest, value) -> None:
    digest.update(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                             allow_nan=False, default=str).encode())
    digest.update(b"\n")


def _frame_digest(digest, frame: pl.DataFrame) -> None:
    _digest(digest, {"schema": str(frame.schema), "polars": pl.__version__})
    if not frame.is_empty():
        digest.update(frame.select(sorted(frame.columns)).hash_rows(seed=0).to_numpy().tobytes())


def _trade(sample: dict, row: dict, revision: int) -> dict:
    candidates = [item for item in row.get("candidates", []) if item.get("can_buy") is True]
    context = row.get("attention_context")
    context = context if isinstance(context, dict) else {}
    group = context.get("group")
    stamp = _stamp(sample["observed_at"])
    identity = f"{revision}:{stamp.isoformat()}:{row['symbol']}"
    return {
        "signal_id": hashlib.sha256(identity.encode()).hexdigest()[:24],
        "symbol": row["symbol"], "name": row.get("name"), "date": str(stamp.date()), "revision": revision,
        "patterns": sorted({item["pattern"] for item in candidates if isinstance(item.get("pattern"), str)}),
        "group": group if group in GROUPS else "unknown", "signal_at": stamp.isoformat(),
        "order_at": stamp.isoformat(), "entry_bar_at": None, "entry_at": None,
        "exit_signal_bar_at": None, "exit_signal_at": None,
        "exit_order_at": None, "exit_bar_at": None, "exit_at": None, "qty": None,
        "entry_raw_price": None, "entry_price": None, "exit_raw_price": None, "exit_price": None,
        "buy_fee": None, "sell_fee": None, "entry_cost": None, "exit_proceeds": None,
        "gross_return": None, "net_return": None, "net_pnl": None,
        "status": "pending_entry", "reasons": [],
        "_config": sample.get("metadata", {}).get("config"), "_candidates": candidates,
        "_entry_factor": None, "_previous_close": None, "_exit_sessions": 0,
    }


def _sample_names(sample: dict, names: set) -> None:
    day = _stamp(sample["observed_at"]).date()
    for row in sample["rows"]:
        name = row.get("name")
        if isinstance(name, str) and name.strip():
            names.add((row["symbol"], day, name.strip()))


def _collect_signals(store, start: date, end: date, revision: int, digest) -> tuple[list[dict], dict, set]:
    coverage = {"sample_count": 0, "stock_days": 0, "eligible_stock_days": 0,
                "negative_stock_days": 0, "unknown_stock_days": 0, "signal_count": 0,
                "calendar_available": False, "minute_days_read": 0}
    flags: dict[tuple[date, str], bool | None] = {}
    selected: dict[tuple[date, str], dict] = {}
    row_count = 0
    names = set()
    for sample in store.iter_samples(start, end, revision):
        stamp = _stamp(sample["observed_at"])
        if not start <= stamp.date() <= end or sample["rule_revision"] != revision:
            raise ValueError("观察样本日期或规则版本不匹配")
        coverage["sample_count"] += 1
        _digest(digest, sample)
        _sample_names(sample, names)
        for row in sample["rows"]:
            row_count += 1
            if row_count > 1_000_000:
                raise ValueError("单次成交验证最多读取100万个股票时点, 请缩小区间")
            key = (stamp.date(), row["symbol"])
            flag = row.get("can_buy")
            if flag is not True and flag is not False and flag is not None:
                raise ValueError("历史can_buy必须为布尔或未知")
            previous = flags.get(key, False)
            flags[key] = True if previous is True or flag is True else None if previous is None or flag is None else False
            if flag is True and (key not in selected or stamp < _stamp(selected[key]["signal_at"])):
                selected[key] = _trade(sample, row, revision)
                if len(selected) > MAX_SIGNALS:
                    raise ValueError("单次成交验证最多5000个股票日信号, 请缩小区间")
    coverage.update(stock_days=len(flags), eligible_stock_days=sum(v is True for v in flags.values()),
                    negative_stock_days=sum(v is False for v in flags.values()),
                    unknown_stock_days=sum(v is None for v in flags.values()), signal_count=len(selected))
    return sorted(selected.values(), key=lambda row: (row["signal_at"], row["symbol"])), coverage, names


def _invalidate(trade: dict, reason: str) -> None:
    trade["status"] = "unverifiable"
    trade["reasons"].append(reason)


def _minute_rows(frame: pl.DataFrame, symbol: str, day: date, marker: bool) -> tuple[list[dict], str | None]:
    if frame.is_empty() or not MINUTE_COLUMNS.issubset(frame.columns):
        return [], "minute_data_missing"
    if not isinstance(frame.schema["datetime"], pl.Datetime):
        return [], "minute_time_invalid"
    dt = pl.col("datetime")
    if frame.schema["datetime"].time_zone:
        dt = dt.dt.convert_time_zone(CN_TZ.key).dt.replace_time_zone(None)
    part = frame.with_columns(dt.alias("datetime")).filter(
        (pl.col("symbol") == symbol) & (pl.col("datetime").dt.date() == day)).sort("datetime")
    if part.is_empty():
        return [], "minute_data_missing"
    if "minute_price_basis" not in part.columns and not marker:
        return [], "minute_basis_unknown"
    return part.to_dicts(), None


def _prefix_issue(rows: list[dict], endpoint: datetime) -> str | None:
    prefix = [row for row in rows if row["datetime"] <= endpoint]
    return _validate_minutes(prefix, (endpoint + timedelta(minutes=1)).replace(tzinfo=CN_TZ))


def _complete_after(order: datetime) -> datetime:
    # Even an order arriving at HH:MM:59 cannot use that minute or the next
    # endpoint. The first eligible label is HH:MM+2, across all supported grids.
    return order.replace(tzinfo=None, second=0, microsecond=0) + timedelta(minutes=2)


def _available_at(endpoint: datetime) -> datetime:
    return (endpoint + timedelta(minutes=1)).replace(tzinfo=CN_TZ)


def _continuous(stamp: datetime) -> bool:
    clock = stamp.time().replace(tzinfo=None)
    return time(9, 30) <= clock < time(11, 30) or time(13) <= clock < time(14, 57)


def _day_facts(row: dict | None) -> tuple[tuple[float, float, float, float, float] | None, str | None]:
    if not row:
        return None, "daily_evidence_missing"
    values = [_number(row.get(key)) for key in ("limit_up_price", "limit_down_price", "close", "raw_close", "raw_prev_close")]
    if any(value is None or value <= 0 or value >= 10000 for value in values):
        return None, "daily_price_basis_or_limits_unknown"
    up, down, adjusted, raw, previous = values
    up, down = up + .005, down - .005
    if not down < up:
        return None, "daily_limits_invalid"
    return (up, down, adjusted / raw, raw, previous), None


def _config_valid(trade: dict) -> bool:
    config = trade["_config"]
    if (not isinstance(config, dict) or type(config.get("revision")) is not int
            or config["revision"] != trade["revision"] or not trade["_candidates"]
            or type(config.get("exit_loss_pct")) not in (int, float)):
        return False
    try:
        deadline = time.fromisoformat(config["exit_time"])
        loss = _number(config["exit_loss_pct"])
    except (KeyError, TypeError, ValueError):
        return False
    return (loss is not None and 0 < loss <= .2 and deadline.second == 0 and deadline.microsecond == 0
            and deadline.tzinfo is None
            and (time(9, 30) <= deadline < time(11, 30) or time(13) <= deadline < time(14, 57)))


def _entry(trade: dict, rows: list[dict], facts, options: ExecutionOptions) -> None:
    if not _config_valid(trade):
        _invalidate(trade, "recorded_signal_config_missing")
        return
    up, down, factor, closing, _ = facts
    limits = [_number(item["evidence"].get("limit_up_price"))
              if isinstance(item.get("evidence"), dict) else None for item in trade["_candidates"]]
    if any(value is None or abs(value - up) > .011 for value in limits):
        _invalidate(trade, "recorded_limit_evidence_mismatch")
        return
    order = _stamp(trade["order_at"])
    earliest = _complete_after(order)
    deadline = (order + timedelta(minutes=options.entry_window_minutes)).replace(tzinfo=None)
    candidates = [row for row in rows if earliest <= row["datetime"]
                  and _available_at(row["datetime"]).replace(tzinfo=None) <= deadline
                  and _continuous(_available_at(row["datetime"]))]
    rejected = set()
    for row in candidates:
        if issue := _prefix_issue(rows, row["datetime"]):
            _invalidate(trade, "minute_window_invalid: " + issue)
            return
        raw = row["close"]
        price = apply_slippage(raw, "buy", options.slippage_bps)
        if row["low"] < down - .011 or row["high"] > up + .011:
            _invalidate(trade, "minute_prices_outside_daily_limits")
            return
        if raw >= up - .005 or price > up:
            rejected.add("entry_limit_blocked")
            continue
        qty = int(options.amount / price / 100) * 100
        while qty > 0 and qty * price + buy_fee(qty, price, options.commission_pct) > options.amount:
            qty -= 100
        if qty < minimum_buy_qty(trade["symbol"]):
            rejected.add("entry_amount_below_lot")
            continue
        if qty > row["volume"] * 100 * options.participation_rate:
            rejected.add("entry_volume_insufficient")
            continue
        fee = buy_fee(qty, price, options.commission_pct)
        trade.update(status="open_position", entry_bar_at=row["datetime"].replace(tzinfo=CN_TZ).isoformat(),
                     entry_at=_available_at(row["datetime"]).isoformat(),
                     qty=qty, entry_raw_price=raw, entry_price=price, buy_fee=fee,
                     entry_cost=qty * price + fee, _entry_factor=factor, _previous_close=closing)
        return
    # The unfilled tail must also be observed. An absent last bar is not proof
    # that no order could trade. Lunch/close shorten the valid order window.
    last = deadline.replace(second=0, microsecond=0) - timedelta(minutes=1)
    if time(11, 29) <= last.time() < time(13):
        last = last.replace(hour=11, minute=28)
    elif last.time() >= time(14, 56):
        last = last.replace(hour=14, minute=55)
    if last >= earliest and (issue := _prefix_issue(rows, last)):
        _invalidate(trade, "entry_window_missing: " + issue)
        return
    trade.update(status="no_fill", reasons=sorted(rejected) or ["entry_window_expired"])


def _exit(trade: dict, rows: list[dict], facts, options: ExecutionOptions) -> None:
    up, down, factor, closing, previous = facts
    if (not math.isclose(factor, trade["_entry_factor"], rel_tol=1e-8, abs_tol=1e-10)
            or abs(previous - trade["_previous_close"]) > .011):
        _invalidate(trade, "corporate_action_unresolved")
        return
    config = trade["_config"]
    deadline = time.fromisoformat(config["exit_time"])
    threshold = trade["entry_cost"] / trade["qty"] * (1 - config["exit_loss_pct"])
    for row in rows:
        endpoint = row["datetime"]
        available = _available_at(endpoint)
        # The opening auction is retained for integrity/VWAP but never used as
        # an executable continuous-session endpoint.
        if endpoint.time() <= time(9, 30) or not _continuous(available):
            continue
        if any(_number(row[key]) is None for key in ("open", "high", "low", "close", "volume", "amount")):
            _invalidate(trade, "minute_values_invalid")
            return
        if row["low"] < down - .011 or row["high"] > up + .011:
            _invalidate(trade, "minute_prices_outside_daily_limits")
            return
        if trade["exit_order_at"] is None and (available.time().replace(tzinfo=None) >= deadline or row["close"] <= threshold):
            if issue := _prefix_issue(rows, endpoint):
                _invalidate(trade, "minute_window_invalid: " + issue)
                return
            stamp = available.isoformat()
            trade.update(exit_signal_bar_at=endpoint.replace(tzinfo=CN_TZ).isoformat(),
                         exit_signal_at=stamp, exit_order_at=stamp)
        if trade["exit_order_at"] is None or endpoint < _complete_after(_stamp(trade["exit_order_at"])):
            continue
        raw = row["close"]
        price = apply_slippage(raw, "sell", options.slippage_bps)
        if raw <= down + .005 or price < down:
            if "exit_limit_blocked" not in trade["reasons"]:
                trade["reasons"].append("exit_limit_blocked")
            continue
        if trade["qty"] > row["volume"] * 100 * options.participation_rate:
            if "exit_volume_insufficient" not in trade["reasons"]:
                trade["reasons"].append("exit_volume_insufficient")
            continue
        # Validate only prefixes actually used by the trigger/fill (or the
        # final unfilled window below), rather than rechecking 240 prefixes.
        # This preserves causality without quadratic repeated validation.
        if issue := _prefix_issue(rows, endpoint):
            _invalidate(trade, "minute_window_invalid: " + issue)
            return
        fee = sell_fee(trade["qty"], price, options.commission_pct, options.stamp_tax_pct)
        proceeds = trade["qty"] * price - fee
        pnl = proceeds - trade["entry_cost"]
        trade.update(status="closed", exit_bar_at=endpoint.replace(tzinfo=CN_TZ).isoformat(), exit_at=available.isoformat(),
                     exit_raw_price=raw, exit_price=price, sell_fee=fee, exit_proceeds=proceeds,
                     gross_return=raw / trade["entry_raw_price"] - 1,
                     net_pnl=pnl, net_return=pnl / trade["entry_cost"])
        return
    # A complete observed day can leave a position unsold; incomplete history
    # cannot be interpreted as a locked market and skipped to a later day.
    if issue := _prefix_issue(rows, datetime.combine(rows[0]["datetime"].date(), time(14, 55))):
        _invalidate(trade, "exit_window_missing: " + issue)
        return
    trade["_previous_close"] = closing


def _summary(trades: list[dict]) -> dict:
    closed = [row for row in trades if row["status"] == "closed"]
    count = len(closed)
    wins = sum(row["net_return"] > 0 for row in closed)
    return {"signals": len(trades), **{state: sum(row["status"] == state for row in trades)
            for state in ("closed", "no_fill", "unverifiable", "open_position")}, "win_count": wins,
            "net_return_mean": sum(row["net_return"] for row in closed) / count if count else None,
            "gross_return_mean": sum(row["gross_return"] for row in closed) / count if count else None,
            "win_rate": wins / count if count else None,
            "net_pnl_sum": sum(row["net_pnl"] for row in closed) if count else None}


def run_execution_validation(repo, store, *, start: date, end: date, revision: int,
                             options: ExecutionOptions, through: date) -> dict:
    if (type(start) is not date or type(end) is not date or type(through) is not date
            or start > end or end > through or (end - start).days >= 92
            or type(revision) is not int or revision < 0):
        raise ValueError("成交验证需要有效规则版本及不超过92个自然日的已完成区间")
    options = ExecutionOptions.model_validate(options)
    digest = hashlib.sha256()
    _digest(digest, {"version": 1, "start": start, "end": end, "revision": revision,
                     "through": through, "options": options.model_dump()})
    trades, coverage, names = _collect_signals(store, start, end, revision, digest)
    load_end = min(through, end + timedelta(days=45))
    calendar_start = start - timedelta(days=32)
    try:
        calendar = load_market_calendar(BUNDLED_CALENDAR_PATH, start=calendar_start, end=load_end)
        if calendar != sorted(set(calendar)) or any(not calendar_start <= day <= load_end for day in calendar):
            raise ValueError("calendar sessions invalid")
        sessions = [day for day in calendar if day >= start]
        coverage["calendar_available"] = bool(sessions)
    except (OSError, ValueError, TypeError, KeyError):
        sessions = []
    _digest(digest, calendar if sessions else [])
    if not sessions:
        for trade in trades:
            _invalidate(trade, "calendar_unavailable")
    else:
        symbols = sorted({row["symbol"] for row in trades})
        try:
            # Exit-day names are historical dimension facts, not added signals.
            # A separate <=45-day read keeps both store ranges bounded to 92d.
            if symbols and load_end > end:
                rows_read = 0
                for sample in store.iter_samples(end + timedelta(days=1), load_end, revision):
                    day = _stamp(sample["observed_at"]).date()
                    if not end < day <= load_end or sample["rule_revision"] != revision:
                        raise ValueError("退出窗口观察样本日期或规则版本不匹配")
                    rows_read += len(sample["rows"])
                    if rows_read > 1_000_000:
                        raise ValueError("退出窗口历史名称超过100万个股票时点, 请缩小区间")
                    _sample_names(sample, names)
            name_history = pl.DataFrame(sorted(item for item in names if item[0] in symbols),
                                        schema={"symbol": pl.String, "date": pl.Date, "name": pl.String},
                                        orient="row")
            _frame_digest(digest, name_history)
            daily = repo.get_execution_daily_batch(symbols, start, load_end, sessions=calendar,
                                                    name_history=name_history) if symbols else pl.DataFrame()
            if not daily.is_empty():
                daily = daily.with_columns(pl.col("date").cast(pl.Date, strict=True)).sort(["symbol", "date"])
                if daily.select(pl.struct("symbol", "date").is_duplicated().any()).item():
                    raise ValueError("duplicate daily rows")
            facts = {(row["symbol"], row["date"]): row for row in daily.to_dicts()}
            _frame_digest(digest, daily)
        except (ValueError, TypeError, KeyError, pl.exceptions.PolarsError):
            facts = {}
        for trade in trades:
            if date.fromisoformat(trade["date"]) not in sessions:
                _invalidate(trade, "signal_day_not_in_calendar")
        marker = minute_basis_is_raw(repo.store.data_dir)
        _digest(digest, {"legacy_raw_marker": marker})
        for day in sessions:
            active = [row for row in trades if (row["status"] == "pending_entry" and row["date"] == str(day))
                      or (row["status"] == "open_position" and row["date"] < str(day)
                          and row["_exit_sessions"] < options.max_exit_sessions)]
            if not active:
                continue
            try:
                frame = repo.get_minute_by_dates(sorted({row["symbol"] for row in active}), [day], raw=True)
                _frame_digest(digest, frame)
            except (OSError, ValueError, TypeError, pl.exceptions.PolarsError):
                frame = pl.DataFrame()
            coverage["minute_days_read"] += 1
            for trade in active:
                daily_facts, issue = _day_facts(facts.get((trade["symbol"], day)))
                rows, minute_issue = _minute_rows(frame, trade["symbol"], day, marker)
                if issue or minute_issue:
                    _invalidate(trade, issue or minute_issue)
                    continue
                if trade["status"] == "pending_entry":
                    _entry(trade, rows, daily_facts, options)
                else:
                    trade["_exit_sessions"] += 1
                    _exit(trade, rows, daily_facts, options)
        for trade in trades:
            if trade["status"] == "pending_entry":
                _invalidate(trade, "entry_session_missing")
            elif trade["status"] == "open_position":
                trade["reasons"].append("exit_window_exhausted" if trade["_exit_sessions"] >= options.max_exit_sessions
                                        else "exit_observation_incomplete")
    summary = _summary(trades)
    return {"schema_version": 1, "mode": "recorded_execution",
            "status": "no_signals" if not trades else "modelled" if summary["closed"] else "insufficient_evidence",
            "model": {"id": "conservative-minute-close-v1", "label": "后续完整分钟收盘价代理验证",
                      "price_proxy": "later_complete_minute_close", "signal_source": "recorded_can_buy",
                      "options": options.model_dump()},
            "coverage": coverage, "summary": summary,
            "by_group": {group: _summary([row for row in trades if row["group"] == group]) for group in GROUPS},
            "trades": [{key: value for key, value in row.items() if not key.startswith("_")}
                       for row in trades[:MAX_DETAILS]],
            "total_trades": len(trades), "trades_truncated": len(trades) > MAX_DETAILS,
            "limitations": LIMITATIONS, "fingerprint": digest.hexdigest()}
