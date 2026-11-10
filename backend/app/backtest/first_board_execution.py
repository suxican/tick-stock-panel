"""Bounded, read-only validation of recorded signals using a conservative proxy.

Observed minute endpoints are price proxies, not exchange fills or queue evidence.
No daily-price fallback, portfolio simulation, account mutation or remote fetching.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from collections.abc import Iterable
from datetime import date, datetime, time, timedelta
from itertools import groupby, pairwise
from operator import itemgetter

import polars as pl
from pydantic import BaseModel, ConfigDict, Field

from app.market_time import CN_TZ
from app.services.first_board_attention import _validate_minutes
from app.services.first_board_portfolio import minimum_buy_qty
from app.services.first_board_priority import RULE_VERSION as PRIORITY_VERSION
from app.services.first_board_priority import SUPPORTED_VERSIONS as PRIORITY_VERSIONS
from app.services.huichun_calendar import BUNDLED_CALENDAR_PATH, load_market_calendar
from app.services.minute_adjust import minute_basis_is_raw
from app.strategy.paper import apply_slippage, buy_fee, sell_fee

GROUPS = ("S0F0", "S1F0", "S0F1", "S1F1", "unknown")
PHASE_GROUPS = ("ice", "ignite", "rally", "climax", "ebb", "repair", "unknown")
MAINLINE_GROUPS = ("hit", "miss", "unknown")
MAX_SIGNALS = 5000
MAX_DETAILS = 500
MINUTE_COLUMNS = {"symbol", "datetime", "open", "high", "low", "close", "volume", "amount"}
LIMITATIONS = [
    "成交明细与原优先分对照使用五分钟采样的首个可买候选；均线与 MACD 对照单独使用当日首个买入候选事件。两者均包含当时账户预算门控。",
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


def _frozen_context(candidates: list[dict]) -> tuple[str, str]:
    """信号当时归档的情绪阶段与主线归属; 旧样本缺失或各形态不一致时为 unknown, 不用今天的数据补。"""
    phases, mainlines = set(), set()
    for candidate in candidates:
        evidence = candidate.get("evidence")
        evidence = evidence if isinstance(evidence, dict) else {}
        environment = evidence.get("environment")
        phase = environment.get("phase") if isinstance(environment, dict) else None
        phases.add(phase if isinstance(phase, str) and phase in PHASE_GROUPS else "unknown")
        mainline = candidate.get("mainline")
        mainline = mainline if isinstance(mainline, dict) else evidence.get("mainline")
        hit = mainline.get("hit") if isinstance(mainline, dict) else None
        mainlines.add("hit" if hit is True else "miss" if hit is False else "unknown")
    return (phases.pop() if len(phases) == 1 else "unknown",
            mainlines.pop() if len(mainlines) == 1 else "unknown")


def _trade(sample: dict, row: dict, revision: int, candidates: list[dict], *, shadow: bool) -> dict:
    context = row.get("attention_context")
    context = context if isinstance(context, dict) else {}
    group = context.get("group")
    stamp = _stamp(sample["observed_at"])
    identity = f"{revision}:{stamp.isoformat()}:{row['symbol']}"
    scored_candidates = [c for c in candidates if isinstance(c.get("priority_score"), dict)
                         and c["priority_score"].get("version") in PRIORITY_VERSIONS
                         and c["priority_score"].get("status") in ({"shadow"} if shadow else {"ready", "partial"})]
    base_scores, full_scores = [], []
    hot_known = False
    role = "unknown"
    priority_candidate = None
    for candidate in scored_candidates:
        score = candidate["priority_score"]
        total, known = score.get("score"), score.get("known_max")
        parts = score.get("parts")
        pop = parts.get("popularity") if isinstance(parts, dict) else None
        if (type(total) not in (int, float) or type(known) not in (int, float)
                or not 0 <= total <= known <= 100 or not isinstance(pop, dict) or "points" not in pop):
            continue
        hot = pop["points"]
        if hot is not None and (type(hot) not in (int, float) or not 0 <= hot <= min(10, total)):
            continue
        highest = max(full_scores, default=-1)
        if total > highest:
            role = score.get("role", "unknown")
            priority_candidate = candidate
        elif total == highest and str(candidate.get("pattern")) < str(priority_candidate.get("pattern")):
            # A stock may satisfy several patterns. Select its representative
            # without consulting technical coverage, support, or future outcomes.
            priority_candidate = candidate
        base_scores.append(round(total - (hot or 0), 1))
        full_scores.append(total)
        hot_known |= hot is not None
    # All shapes for this symbol come from one quote. A partially archived
    # batch can still supply its change; conflicting observations cannot.
    changes = {_number(c.get("change_pct")) for c in candidates} - {None}
    phase, mainline = _frozen_context(candidates)
    return {
        "signal_id": hashlib.sha256(identity.encode()).hexdigest()[:24],
        "symbol": row["symbol"], "name": row.get("name"), "date": str(stamp.date()), "revision": revision,
        "patterns": sorted({item["pattern"] for item in candidates if isinstance(item.get("pattern"), str)}),
        "group": group if group in GROUPS else "unknown", "phase": phase, "mainline": mainline,
        "signal_at": stamp.isoformat(),
        "order_at": stamp.isoformat(), "entry_bar_at": None, "entry_at": None,
        "exit_signal_bar_at": None, "exit_signal_at": None,
        "exit_order_at": None, "exit_bar_at": None, "exit_at": None, "qty": None,
        "entry_raw_price": None, "entry_price": None, "exit_raw_price": None, "exit_price": None,
        "buy_fee": None, "sell_fee": None, "entry_cost": None, "exit_proceeds": None,
        "gross_return": None, "net_return": None, "net_pnl": None,
        "status": "pending_entry", "reasons": [],
        "_config": sample.get("metadata", {}).get("config"), "_candidates": candidates,
        "_shadow": shadow, "_role": role, "_base_score": max(base_scores, default=None),
        "_full_score": max(full_scores, default=None), "_hot_known": hot_known,
        "_priority_candidate": priority_candidate,
        "_priority_version": priority_candidate["priority_score"]["version"] if priority_candidate else None,
        "_baseline": next(iter(changes)) if len(changes) == 1 else None,
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
    selected: dict[tuple[date, str, bool], dict] = {}
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
            shadow = flag is False
            if flag is None:
                continue
            if shadow:
                candidates = [c for c in row.get("candidates", []) if c.get("sector_shadow_eligible") is True
                              and c.get("can_buy") is False and (c.get("priority_score") or {}).get("status") == "shadow"]
            else:
                candidates = [c for c in row.get("candidates", []) if c.get("can_buy") is True]
            signal_key = (*key, shadow)
            if (flag is True or candidates) and (signal_key not in selected
                    or stamp < _stamp(selected[signal_key]["signal_at"])):
                selected[signal_key] = _trade(sample, row, revision, candidates, shadow=shadow)
                if len(selected) > MAX_SIGNALS:
                    raise ValueError("单次成交验证最多5000个股票日信号, 请缩小区间")
    coverage.update(stock_days=len(flags), eligible_stock_days=sum(v is True for v in flags.values()),
                    negative_stock_days=sum(v is False for v in flags.values()),
                    unknown_stock_days=sum(v is None for v in flags.values()),
                    signal_count=sum(not t["_shadow"] for t in selected.values()))
    return sorted(selected.values(), key=lambda row: (row["signal_at"], row["symbol"])), coverage, names


def _invalidate(trade: dict, reason: str) -> None:
    trade["status"] = "unverifiable"
    trade["reasons"].append(reason)


def _first_alert_trades(events: Iterable[dict], start: date, end: date, revision: int, digest=None) -> list[dict]:
    """Freeze the earliest actual alert, including same-snapshot shapes and legacy unknowns."""
    earliest: dict[tuple[str, str], list[dict]] = {}
    for event in events:
        if event.get("revision") != revision or event.get("event_type") != "buy_candidate":
            continue
        ts = event.get("ts")
        if type(ts) is not int:
            raise ValueError("买入候选事件缺少有效时刻")
        try:
            at = datetime.fromtimestamp(ts / 1000, CN_TZ)
        except (ValueError, OverflowError, OSError) as exc:
            raise ValueError("买入候选事件时刻无效") from exc
        if not start <= at.date() <= end or str(at.date()) != event["date"] or not _continuous(at):
            raise ValueError("买入候选事件时刻与记录交易日或观察时段不符")
        key = (event["date"], event["symbol"])
        prior = earliest.get(key)
        if prior is None or ts < prior[0]["ts"]:
            earliest[key] = [event]
            if len(earliest) > MAX_SIGNALS:
                raise ValueError("技术对照最多5000个股票日信号，请缩小区间")
        elif ts == prior[0]["ts"]:
            prior.append(event)

    trades = []
    for key in sorted(earliest):
        batch = sorted(earliest[key], key=lambda event: (event.get("pattern", ""), event["id"]))
        event = batch[0]
        if digest is not None:
            _digest(digest, batch)
        stamp = datetime.fromtimestamp(event["ts"] / 1000, CN_TZ)
        sample = {"observed_at": stamp.isoformat(), "metadata": {"config": event.get("config")}}
        candidates = [{**item, "priority_score": item["evidence"].get("priority_score")} for item in batch]
        trades.append(_trade(sample, event, revision, candidates, shadow=False))
    return trades


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
    # 旧版配置没有止盈与涨停持有字段, 缺省即未启用。
    profit = config.get("exit_profit_pct")
    if profit is not None and (type(profit) not in (int, float) or not 0 < profit <= .5):
        return False
    if type(config.get("exit_hold_limit_up", False)) is not bool:
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
    cost = trade["entry_cost"] / trade["qty"]
    threshold = cost * (1 - config["exit_loss_pct"])
    target = cost * (1 + config["exit_profit_pct"]) if config.get("exit_profit_pct") is not None else None
    hold_limit_up = config.get("exit_hold_limit_up") is True
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
        # 与盘中提示同口径: 止损优先; 启用涨停持有时, 收于涨停的分钟暂缓定时与止盈退出。
        planned = ((available.time().replace(tzinfo=None) >= deadline
                    or (target is not None and row["close"] >= target))
                   and not (hold_limit_up and row["close"] >= up - .005))
        if trade["exit_order_at"] is None and (planned or row["close"] <= threshold):
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


def _board_outcomes(trade: dict, daily: dict | None, up: float) -> None:
    # The approaching signal requires the whole day's high so far to be below limit.
    # A same-day pre-close snapshot cannot establish the final high/close.
    if not daily:
        return
    stamp = _number(daily.get("quote_ts"))
    day = date.fromisoformat(trade["date"])
    close_ms = datetime.combine(day, time(15), CN_TZ).timestamp() * 1000
    high, close = _number(daily.get("raw_high")), _number(daily.get("raw_close"))
    if (stamp is None or not close_ms <= stamp < close_ms + 9 * 3600_000 or high is None or close is None
            or not 0 < close <= high <= up + .005):
        return
    if not trade["_candidates"] or any(c.get("state") != "approaching"
            or (limit := _number(c.get("evidence", {}).get("limit_up_price"))) is None
            or abs(limit - up) > .005 for c in trade["_candidates"]):
        return
    trade["_touched"], trade["_sealed"] = high >= up - .005, close >= up - .005


def _rank_outcomes(rows: list[dict]) -> dict:
    known = [r for r in rows if r.get("_sealed") is not None]
    closed = [r["net_return"] for r in rows if r["status"] == "closed"]
    return {**_summary(rows), "outcomes": len(known), "touched": sum(r["_touched"] for r in known),
            "sealed": sum(r["_sealed"] for r in known), "broken": sum(r["_touched"] and not r["_sealed"] for r in known),
            "filled": sum(r.get("entry_at") is not None for r in rows), "worst_net_return": min(closed, default=None)}


def _newest_version(items: list, trade=lambda item: item) -> list:
    """升级当日可能混有新旧优先分; 名次只在同一版本内比较, 旧版本计为未评分。"""
    versions = {trade(item).get("_priority_version") for item in items} - {None}
    if len(versions) <= 1:
        return items
    newest = max(versions, key=PRIORITY_VERSIONS.index)
    return [item for item in items if trade(item).get("_priority_version") == newest]


def _compared_version(groups: Iterable[list], trade=lambda item: item) -> str:
    versions = {trade(item).get("_priority_version") for items in groups for item in items} - {None}
    return max(versions, key=PRIORITY_VERSIONS.index, default=PRIORITY_VERSION)


def _priority_day(items: Iterable[dict], key: str) -> list[dict]:
    # Arrival time consumes the available daily slots. A score can only order
    # candidates on the same observation, with the symbol resolving equal scores.
    return sorted(items, key=lambda r: (r["signal_at"], r[key] is None, -(r[key] or 0), r["symbol"]))


def _priority_comparison(trades: list[dict], shadows: list[dict]) -> dict:
    days = defaultdict(list)
    for trade in trades:
        if trade["_full_score"] is not None:
            days[trade["date"]].append(trade)
    days = {day: _newest_version(items) for day, items in days.items()}
    dates = sorted(days)
    cutoff = dates[max(1, int(len(dates) * .7))] if len(dates) >= 2 else None
    groups = []
    for variant, key in (("baseline", "_baseline"), ("sector", "_base_score"), ("popularity", "_full_score")):
        ranked = {day: _priority_day(items, key) for day, items in days.items()}
        for n in (3, 5):
            for window in ("all", "holdout"):
                picked = [r for day in dates if window == "all" or (cutoff and day >= cutoff)
                          for r in ranked[day][:n]]
                subsets = defaultdict(list)
                for r in picked:
                    market = "growth" if r["symbol"].startswith(("30", "688", "689")) else "main"
                    subsets[market].append(r)
                    subsets["hot_known" if r["_hot_known"] else "hot_unknown"].append(r)
                    if r.get("_role") in ("leader", "follower"):
                        subsets[r["_role"]].append(r)
                groups.append({"variant": variant, "top_n": n, "window": window, **_rank_outcomes(picked),
                               "segments": {k: _rank_outcomes(v) for k, v in subsets.items()}})
    scored = sum(map(len, days.values()))
    return {"version": _compared_version(days.values()), "scored": scored, "unscored": len(trades) - scored,
            "popularity_known": sum(r["_hot_known"] for items in days.values() for r in items),
            "holdout_start": cutoff, "groups": groups, "shadow": _rank_outcomes(shadows)}


TECHNICAL_VERSION = "ma-macd-shadow-v1"
SCORE_TOLERANCE = 2.
TECHNICAL_VARIANTS = ("baseline", "ma", "macd", "combined")
TECHNICAL_PATTERNS = ("platform", "trend", "oversold")


def _technical_signals(trade: dict) -> tuple[bool | None, bool | None]:
    """Read only the winning baseline pattern's contemporaneous, frozen votes."""
    candidate = trade["_priority_candidate"]
    if not candidate:
        return None, None
    facts = candidate.get("evidence")
    shadow = facts.get("technical_shadow") if isinstance(facts, dict) else None
    previous = trade.get("_reference_session")
    if (not isinstance(shadow, dict) or shadow.get("version") != TECHNICAL_VERSION
            or candidate.get("pattern") not in TECHNICAL_PATTERNS
            or shadow.get("pattern") != candidate["pattern"]
            or not previous or previous >= trade["date"] or shadow.get("reference_date") != previous
            or (facts.get("reference_date") is not None and facts["reference_date"] != previous)):
        return None, None
    ma, macd = shadow.get("ma_support"), shadow.get("macd_support")
    if shadow.get("status") == "ready" and type(ma) is bool and type(macd) is bool:
        return ma, macd
    if shadow.get("status") == "partial" and (
        (type(ma) is bool and macd is None) or (ma is None and type(macd) is bool)
    ):
        return ma, macd
    return None, None


def _technical_day(items: list[tuple[dict, bool, bool]]) -> tuple[dict[str, list[dict]], dict[int, set[str]]]:
    # Keep earlier events' places and each pattern's original slots. Anchor a
    # band at its highest baseline score so pairwise near-ties cannot chain.
    items = sorted(items, key=lambda item: (item[0]["signal_at"], -item[0]["_full_score"], item[0]["symbol"]))
    baseline = [trade for trade, _, _ in items]
    rankings = {variant: list(baseline) for variant in TECHNICAL_VARIANTS}
    pattern_slots = defaultdict(list)
    for index, trade in enumerate(baseline):
        pattern_slots[trade["signal_at"], trade["_priority_candidate"]["pattern"]].append(index)
    bands = []
    for slots in pattern_slots.values():
        band = []
        for slot in slots:
            if band and baseline[band[0]]["_full_score"] - baseline[slot]["_full_score"] > SCORE_TOLERANCE + 1e-9:
                bands.append(band)
                band = []
            band.append(slot)
        bands.append(band)
    opportunities = {n: {baseline[band[0]]["_priority_candidate"]["pattern"] for band in bands
                         if band[0] < n <= band[-1]} for n in (3, 5)}
    votes = {"ma": [ma for _, ma, _ in items], "macd": [macd for _, _, macd in items],
             "combined": [ma + macd for _, ma, macd in items]}
    for band in bands:
        if len(band) < 2:
            continue
        for variant, support in votes.items():
            # Stable sort preserves the original score and symbol order on tied votes.
            ordered = sorted(band, key=lambda i: -support[i])
            for slot, source in zip(band, ordered, strict=True):
                rankings[variant][slot] = baseline[source]
    return rankings, opportunities


def _archived_popularity(candidate: dict) -> dict:
    score = candidate.get("priority_score") or {}
    part = score.get("parts", {}).get("popularity", {})
    radar = score.get("radar")
    pop = radar.get("popularity") if isinstance(radar, dict) else None
    pop = pop if isinstance(pop, dict) else {}
    rank, gain, status = pop.get("rank"), pop.get("rank_gain"), pop.get("status")
    rank = rank if status == "ready" and type(rank) is int and 1 <= rank <= 100 else None
    points = part.get("points")
    maximum = part.get("known_max")
    return {"rank": rank, "rank_gain": gain if rank and type(gain) is int and abs(gain) < 100 else None,
            "status": status if rank or status == "unranked" else None, "points": points,
            "known_max": maximum if type(maximum) in (int, float) and (points or 0) <= maximum <= 10 else None}


def rank_first_alerts(events: Iterable[dict], *, day: date, revision: int, reference_session: date | None) -> dict:
    """Full compact one-day result. Service filters top seats and pages after ranking."""
    if (type(day) is not date or type(revision) is not int or revision < 0
            or (reference_session is not None and (type(reference_session) is not date or reference_session >= day))):
        raise ValueError("首次提示排名需要有效交易日、规则版本与早于当日的参考交易日")
    alerts = _first_alert_trades(events, day, day, revision)
    reference = str(reference_session) if reference_session else None
    rows = {}
    complete = []
    for trade in alerts:
        trade["_reference_session"] = reference
        ma, macd = _technical_signals(trade)
        if ma is not None and macd is not None:
            complete.append((trade, ma, macd))
        candidate = trade["_priority_candidate"] or {}
        rows[trade["symbol"]] = {
            "symbol": trade["symbol"], "name": trade["name"], "signal_at": trade["signal_at"],
            "patterns": trade["patterns"], "pattern": candidate.get("pattern"),
            "change_pct": trade["_baseline"], "base_score": trade["_base_score"], "priority_score": trade["_full_score"],
            "popularity": _archived_popularity(candidate), "popularity_known": trade["_hot_known"],
            "ma_support": ma, "macd_support": macd, "ranks": {},
        }
    # Individual evidence coverage differs for legacy alerts. Never convert
    # missing scores to bottom places; compare technical variants on common rows.
    scored = _newest_version([t for t in alerts if t["_full_score"] is not None])
    ranked = {variant: _priority_day((t for t in pool if t[key] is not None), key)
              for variant, key, pool in (("change", "_baseline", alerts), ("base", "_base_score", scored),
                                         ("priority", "_full_score", scored))}
    technical, _ = _technical_day(_newest_version(complete, itemgetter(0)))
    ranked.update({("technical_baseline" if variant == "baseline" else variant): items
                   for variant, items in technical.items()})
    for variant, items in ranked.items():
        before = 0
        for _, batch in groupby(items, key=itemgetter("signal_at")):
            group = list(batch)
            for position, trade in enumerate(group, 1):
                rows[trade["symbol"]]["ranks"][variant] = {
                    "batch": position, "batch_total": len(group), "day": before + position,
                }
            before += len(group)
    def display_order(row: dict) -> tuple:
        ranks = row["ranks"]
        # Old alerts often have no score. Display their strongest recorded
        # move first within that batch, while leaving their score seats empty.
        first = ranks.get("priority") or ranks.get("change") or {}
        return row["signal_at"], "priority" not in ranks, first.get("batch", math.inf), row["symbol"]

    return {"source": "first_buy_candidate", "date": str(day), "reference_date": reference, "revision": revision,
            "total": len(rows), "coverage": {"batches": len({r["signal_at"] for r in alerts}),
                "change_known": len(ranked["change"]), "priority_known": len(ranked["priority"]),
                "popularity_known": sum(t["_hot_known"] for t in alerts), "technical_known": len(complete)},
            "rows": sorted(rows.values(), key=display_order)}


def _technical_outcomes(day_picks: list[tuple[list[dict], set[str], set[str]]], pattern: str | None = None) -> dict:
    picks = []
    replacements = changed_days = opportunity_days = 0
    for selection, base_symbols, opportunities in day_picks:
        subset = selection if pattern is None else [r for r in selection if r["_priority_candidate"]["pattern"] == pattern]
        swaps = sum(r["symbol"] not in base_symbols for r in subset)
        replacements += swaps
        changed_days += swaps > 0
        opportunity_days += bool(opportunities) if pattern is None else pattern in opportunities
        picks.extend(subset)
    t1 = [r["net_return"] for r in picks if r["status"] == "closed" and r["_exit_sessions"] == 1]
    return {**_rank_outcomes(picks), "changed_days": changed_days, "replacements": replacements,
            "opportunity_days": opportunity_days, "t1_closed": len(t1),
            "t1_net_return_mean": sum(t1) / len(t1) if t1 else None}


def _technical_comparison(trades: list[dict]) -> dict:
    """Ablate frozen MA and MACD votes in one common complete-case cohort."""
    days = defaultdict(list)
    priority_known = ma_known = macd_known = 0
    for trade in trades:
        priority_known += trade["_priority_candidate"] is not None
        ma, macd = _technical_signals(trade)
        ma_known += ma is not None
        macd_known += macd is not None
        if ma is not None and macd is not None:
            days[trade["date"]].append((trade, ma, macd))
    days = {day: _newest_version(items, itemgetter(0)) for day, items in days.items()}
    dates = sorted(days)
    cutoff = dates[max(1, int(len(dates) * .7))] if len(dates) >= 2 else None
    ranked = {day: _technical_day(items) for day, items in days.items()}
    groups = []
    for n in (3, 5):
        for window, selected_dates in (("all", dates), ("holdout", [d for d in dates if cutoff and d >= cutoff])):
            for variant in TECHNICAL_VARIANTS:
                day_picks = []
                for day in selected_dates:
                    rankings, opportunities = ranked[day]
                    base_symbols = {row["symbol"] for row in rankings["baseline"][:n]}
                    day_picks.append((rankings[variant][:n], base_symbols, opportunities[n]))
                groups.append({"variant": variant, "window": window, "top_n": n, **_technical_outcomes(day_picks),
                               "by_pattern": {p: _technical_outcomes(day_picks, p) for p in TECHNICAL_PATTERNS}})
    scored = sum(map(len, days.values()))
    return {"version": TECHNICAL_VERSION, "baseline": _compared_version(days.values(), itemgetter(0)),
            "score_tolerance": SCORE_TOLERANCE,
            "eligible": len(trades), "priority_known": priority_known, "ma_known": ma_known,
            "macd_known": macd_known, "scored": scored, "unscored": len(trades) - scored,
            "holdout_start": cutoff, "groups": groups}


def run_execution_validation(repo, store, *, start: date, end: date, revision: int,
                             options: ExecutionOptions, through: date, events: Iterable[dict] = ()) -> dict:
    if (type(start) is not date or type(end) is not date or type(through) is not date
            or start > end or end > through or (end - start).days >= 92
            or type(revision) is not int or revision < 0):
        raise ValueError("成交验证需要有效规则版本及不超过92个自然日的已完成区间")
    options = ExecutionOptions.model_validate(options)
    digest = hashlib.sha256()
    _digest(digest, {"version": 3, "start": start, "end": end, "revision": revision,
                     "through": through, "options": options.model_dump()})
    trades, coverage, names = _collect_signals(store, start, end, revision, digest)
    alerts = _first_alert_trades(events, start, end, revision, digest)
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
        previous = {str(day): str(prior) for prior, day in pairwise(calendar)}
        simulated = list(trades)
        for alert in alerts:
            alert["_reference_session"] = previous.get(alert["date"])
            if all(value is not None for value in _technical_signals(alert)):
                simulated.append(alert)
                if isinstance(alert["name"], str) and alert["name"].strip():
                    names.add((alert["symbol"], date.fromisoformat(alert["date"]), alert["name"].strip()))
        symbols = sorted({row["symbol"] for row in simulated})
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
        for trade in simulated:
            if date.fromisoformat(trade["date"]) not in sessions:
                _invalidate(trade, "signal_day_not_in_calendar")
        marker = minute_basis_is_raw(repo.store.data_dir)
        _digest(digest, {"legacy_raw_marker": marker})
        for day in sessions:
            active = [row for row in simulated if (row["status"] == "pending_entry" and row["date"] == str(day))
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
            # The anchor and the actual alert may request the same stock-day.
            # Share its bars but simulate each order at its own signal time.
            for symbol, orders in groupby(sorted(active, key=itemgetter("symbol")), key=itemgetter("symbol")):
                daily_row = facts.get((symbol, day))
                daily_facts, issue = _day_facts(daily_row)
                rows, minute_issue = _minute_rows(frame, symbol, day, marker)
                for trade in orders:
                    if trade["status"] == "pending_entry" and daily_facts is not None:
                        _board_outcomes(trade, daily_row, daily_facts[0])
                    if issue or minute_issue:
                        _invalidate(trade, issue or minute_issue)
                    elif trade["status"] == "pending_entry":
                        _entry(trade, rows, daily_facts, options)
                    else:
                        trade["_exit_sessions"] += 1
                        _exit(trade, rows, daily_facts, options)
        for trade in simulated:
            if trade["status"] == "pending_entry":
                _invalidate(trade, "entry_session_missing")
            elif trade["status"] == "open_position":
                trade["reasons"].append("exit_window_exhausted" if trade["_exit_sessions"] >= options.max_exit_sessions
                                        else "exit_observation_incomplete")
    shadows = [trade for trade in trades if trade["_shadow"]]
    trades = [trade for trade in trades if not trade["_shadow"]]
    summary = _summary(trades)
    return {"schema_version": 1, "mode": "recorded_execution",
            "status": "no_signals" if not trades else "modelled" if summary["closed"] else "insufficient_evidence",
            "model": {"id": "conservative-minute-close-v1", "label": "后续完整分钟收盘价代理验证",
                      "price_proxy": "later_complete_minute_close", "signal_source": "recorded_can_buy",
                      "options": options.model_dump()},
            "coverage": coverage, "summary": summary,
            "priority_comparison": _priority_comparison(trades, shadows),
            "technical_comparison": _technical_comparison(alerts),
            "by_group": {group: _summary([row for row in trades if row["group"] == group]) for group in GROUPS},
            "by_phase": {group: _summary([row for row in trades if row["phase"] == group])
                         for group in PHASE_GROUPS},
            "by_mainline": {group: _summary([row for row in trades if row["mainline"] == group])
                            for group in MAINLINE_GROUPS},
            "trades": [{key: value for key, value in row.items() if not key.startswith("_")}
                       for row in trades[:MAX_DETAILS]],
            "total_trades": len(trades), "trades_truncated": len(trades) > MAX_DETAILS,
            "limitations": LIMITATIONS, "fingerprint": digest.hexdigest()}
