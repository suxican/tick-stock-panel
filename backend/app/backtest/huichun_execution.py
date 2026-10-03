"""Pure research execution rules for Huichun; prices are unadjusted CNY.

No provider, repository or account I/O. Minute inputs must describe the exact
left-closed interval, with volume in shares and amount in CNY. The daily proxy
may reuse fee helpers, but cannot claim to have run the minute matcher.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Literal

Side = Literal["buy", "sell"]
_CENT = Decimal("0.01")
_COMMISSION_RATE = Decimal("0.0003")
_MIN_COMMISSION = Decimal("5")
STAMP_TAX_VERIFIED_THROUGH = date(2026, 9, 30)


def _decimal(value: float, name: str, *, positive: bool = False) -> Decimal:
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if not result.is_finite() or result < 0 or (positive and result == 0):
        raise ValueError(f"{name} must be finite and {'positive' if positive else 'nonnegative'}")
    return result


def _side(side: Side) -> None:
    if side not in {"buy", "sell"}:
        raise ValueError("side must be buy or sell")


def stamp_tax_rate(day: date) -> float:
    """Sell-side rate in the verified fee table; reject unsupported dates.

    Sources: MOF 2008-09-19 announcement (76432), and MOF/STA announcement
    2023 No. 39 effective 2023-08-28. No tax policy is extrapolated beyond the
    frozen research end date.
    """
    if type(day) is not date or not date(2008, 9, 19) <= day <= STAMP_TAX_VERIFIED_THROUGH:
        raise ValueError("Trade date outside verified stamp-tax table")
    return 0.001 if day < date(2023, 8, 28) else 0.0005


@dataclass(frozen=True)
class Fees:
    commission: float
    stamp_tax: float

    @property
    def total(self) -> float:
        return float(Decimal(str(self.commission)) + Decimal(str(self.stamp_tax)))


def fee_breakdown(notional: float, day: date, side: Side, already_notional: float = 0.0) -> Fees:
    """Incremental costs for one parent order on one day, rounded to cents.

    Pass the same parent's already filled gross amount *on this day*. The
    difference of aggregate charges applies the minimum once and avoids
    overcharging each partial fill. A new day starts at zero.
    """
    _side(side)
    rate = Decimal(str(stamp_tax_rate(day)))
    amount = _decimal(notional, "notional")
    prior = _decimal(already_notional, "already_notional")
    if amount == 0:
        return Fees(0.0, 0.0)

    def commission(gross: Decimal) -> Decimal:
        return (
            max(_MIN_COMMISSION, gross * _COMMISSION_RATE).quantize(_CENT, ROUND_HALF_UP)
            if gross
            else Decimal(0)
        )

    tax = Decimal(0)
    if side == "sell":
        tax = ((prior + amount) * rate).quantize(_CENT, ROUND_HALF_UP)
        tax -= (prior * rate).quantize(_CENT, ROUND_HALF_UP)
    return Fees(float(commission(prior + amount) - commission(prior)), float(tax))


def order_fees(notional: float, day: date, side: Side, already_notional: float = 0.0) -> float:
    return fee_breakdown(notional, day, side, already_notional).total


def round_to_tick(price: float, tick: float, side: Side) -> float:
    """Round an already slipped price up for buys or down for sells."""
    _side(side)
    value = _decimal(price, "price", positive=True)
    step = _decimal(tick, "tick", positive=True)
    rounding = ROUND_CEILING if side == "buy" else ROUND_FLOOR
    return float((value / step).to_integral_value(rounding=rounding) * step)


def _calendar(days: Sequence[date]) -> tuple[date, ...]:
    result = tuple(days)
    if not result or any(type(day) is not date for day in result):
        raise ValueError("A non-empty market calendar is required")
    if list(result) != sorted(set(result)):
        raise ValueError("Market calendar must be sorted and unique")
    return result


def _timestamp(value: datetime) -> None:
    if not isinstance(value, datetime) or value.tzinfo is not None:
        raise ValueError("Timestamps must be naive Beijing wall-clock datetimes")


def _mainboard(symbol: str) -> None:
    if (
        not (
            (symbol.endswith(".SH") and symbol[:3] in {"600", "601", "603", "605"})
            or (symbol.endswith(".SZ") and symbol[:3] in {"000", "001", "002", "003"})
        )
        or len(symbol) != 9
        or not symbol[:6].isdigit()
    ):
        raise ValueError(
            "This execution model supports ordinary Shanghai/Shenzhen mainboard A shares"
        )


@dataclass(frozen=True)
class Candidate:
    version: str
    symbol: str
    cycle_id: str
    signal_at: datetime
    data_cutoff: datetime
    ranking_amount: float
    ranking_cutoff: datetime
    nav_before_signal: float
    nav_date: date
    # Submission-time recheck, supplied separately from signal qualification.
    eligible: bool | None

    @property
    def key(self) -> tuple[str, str, str]:
        return self.version, self.symbol, self.cycle_id


def first_buy_window(candidate: Candidate, calendar: Sequence[date]) -> datetime | None:
    days = _calendar(calendar)
    _timestamp(candidate.signal_at)
    day = candidate.signal_at.date()
    if day not in days:
        raise ValueError("Signal date is absent from market calendar")
    if candidate.version not in {"A0", "A1", "B1"}:
        raise ValueError("Unknown Huichun version")
    expected_time = time(10, 30) if candidate.version == "B1" else time(15)
    if candidate.signal_at.time() != expected_time:
        raise ValueError("Signal time differs from the frozen version specification")
    if candidate.version == "B1":
        return datetime.combine(day, time(10, 31))
    index = days.index(day) + 1
    return datetime.combine(days[index], time(9, 31)) if index < len(days) else None


@dataclass(frozen=True)
class Order:
    parent_id: str
    symbol: str
    side: Side
    submitted_at: datetime
    first_window: datetime
    budget: float = 0.0
    shares: int = 0


@dataclass(frozen=True)
class SubmissionDecision:
    candidate: Candidate
    reason: str
    budget: float = 0.0


@dataclass(frozen=True)
class BuyPlan:
    orders: tuple[Order, ...]
    decisions: tuple[SubmissionDecision, ...]
    consumed: frozenset[tuple[str, str, str]]
    complete: bool


def allocate_buy_window(
    candidates: Sequence[Candidate],
    *,
    window_start: datetime,
    calendar: Sequence[date],
    cash: float,
    held_symbols: frozenset[str] = frozenset(),
    consumed: frozenset[tuple[str, str, str]] = frozenset(),
    reserved_cash: float = 0.0,
    max_positions: int = 5,
    target_weight: float = 0.2,
) -> BuyPlan:
    """Freeze selection and budgets before reading any execution minute.

    ``held_symbols`` includes pending sells and partially exited positions.
    ``cash`` excludes any proceeds from this window's not-yet-completed sells.
    Every new signal consumes its cycle even when submission fails. The caller
    carries returned consumed keys forward and never backfills the plan after
    matching. Ranking inputs must be precomputed from the previous 20 valid
    daily bars available strictly before the signal, not from execution bars.
    """
    days = _calendar(calendar)
    _timestamp(window_start)
    remaining = max(Decimal(0), _decimal(cash, "cash") - _decimal(reserved_cash, "reserved_cash"))
    weight = _decimal(target_weight, "target_weight", positive=True)
    if weight > 1 or type(max_positions) is not int or max_positions < 1:
        raise ValueError("Invalid position allocation parameters")
    if len({candidate.version for candidate in candidates}) > 1:
        raise ValueError("Each experimental version requires an independent account")
    if len({candidate.key for candidate in candidates}) != len(candidates):
        raise ValueError("Duplicate candidate cycle in one window")
    for candidate in candidates:
        _mainboard(candidate.symbol)
        _timestamp(candidate.data_cutoff)
        _timestamp(candidate.ranking_cutoff)
        if first_buy_window(candidate, days) != window_start:
            raise ValueError("Candidate submitted outside its sole buy window")
        if (
            candidate.data_cutoff > candidate.signal_at
            or candidate.ranking_cutoff >= candidate.signal_at
        ):
            raise ValueError("Submission inputs contain data unavailable before the signal")
        index = days.index(candidate.signal_at.date())
        if index == 0 or candidate.nav_date != days[index - 1]:
            raise ValueError("Budget NAV must be from the market session before the signal day")
        if candidate.eligible is not None and type(candidate.eligible) is not bool:
            raise ValueError("Submission eligibility must be true, false or unknown")
        _decimal(candidate.ranking_amount, "ranking_amount")
        _decimal(candidate.nav_before_signal, "nav_before_signal")
    slots = max(0, max_positions - len(held_symbols))
    used = set(consumed)
    planned_symbols = set(held_symbols)
    orders, decisions = [], []
    complete = True
    for candidate in sorted(candidates, key=lambda c: (-c.ranking_amount, c.symbol)):
        reason = ""
        if candidate.key in used:
            reason = "cycle_consumed"
        else:
            used.add(candidate.key)
            if candidate.eligible is None:
                reason, complete = "eligibility_unknown", False
            elif not candidate.eligible:
                reason = "ineligible_at_submission"
            elif candidate.symbol in planned_symbols:
                reason = "already_held"
            elif slots == 0:
                reason = "positions_full"
            elif remaining <= 0 or candidate.nav_before_signal == 0:
                reason = "no_cash"
        if reason:
            decisions.append(SubmissionDecision(candidate, reason))
            continue
        budget = min(_decimal(candidate.nav_before_signal, "NAV") * weight, remaining)
        budget = budget.quantize(_CENT, rounding=ROUND_FLOOR)
        if budget <= 0:
            decisions.append(SubmissionDecision(candidate, "no_cash"))
            continue
        parent_id = ":".join(candidate.key)
        orders.append(
            Order(parent_id, candidate.symbol, "buy", window_start, window_start, float(budget))
        )
        decisions.append(SubmissionDecision(candidate, "submitted", float(budget)))
        remaining -= budget
        planned_symbols.add(candidate.symbol)
        slots -= 1
    return BuyPlan(tuple(orders), tuple(decisions), frozenset(used), complete)


@dataclass(frozen=True)
class MinuteBar:
    symbol: str
    start: datetime
    low: float
    high: float
    volume_shares: int
    amount: float
    tick: float
    limit_down: float | None
    limit_up: float | None
    status: Literal["complete", "suspended", "unknown"] = "complete"


@dataclass(frozen=True)
class Fill:
    parent_id: str
    symbol: str
    side: Side
    window_start: datetime
    shares: int
    price: float
    notional: float
    fees: Fees

    @property
    def completed_at(self) -> datetime:
        return self.window_start + timedelta(minutes=1)


@dataclass(frozen=True)
class MatchResult:
    fill: Fill | None
    reason: str
    complete: bool = True


def match_minute(
    order: Order,
    bar: MinuteBar | None,
    *,
    calendar: Sequence[date],
    available_cash: float | None = None,
    entry_date: date | None = None,
    already_notional: float = 0.0,
    slippage_bps: float = 5.0,
) -> MatchResult:
    """Match once per order/minute; no selection, reallocation or data fallback.

    For pending sells, ``order.shares`` is the *remaining* holding. The caller
    passes prior notional for the same parent/day, carries the exit intent to
    every later eligible minute, and flags the whole run incomplete on unknown
    execution. Buy remainders expire after this single minute.
    """
    _side(order.side)
    _mainboard(order.symbol)
    _timestamp(order.submitted_at)
    _timestamp(order.first_window)
    if order.submitted_at > order.first_window:
        raise ValueError("Order was submitted after its execution window")
    days = _calendar(calendar)
    if bar is None or bar.status == "unknown":
        return MatchResult(None, "execution_unknown", False)
    if bar.status not in {"complete", "suspended"}:
        raise ValueError("Unknown minute status")
    _timestamp(bar.start)
    if bar.start.second or bar.start.microsecond or bar.symbol != order.symbol:
        raise ValueError("Minute interval or symbol does not match the order")
    if order.side == "buy" and bar.start != order.first_window:
        return MatchResult(None, "outside_buy_window")
    if bar.start < order.first_window:
        return MatchResult(None, "before_exit_window")
    day = bar.start.date()
    if day not in days:
        return MatchResult(None, "market_closed")
    minute = bar.start.time()
    if not (time(9, 31) <= minute < time(11, 30) or time(13) <= minute < time(14, 57)):
        return MatchResult(None, "outside_continuous_auction")
    if order.side == "sell":
        if entry_date is None:
            raise ValueError("Sell matching requires the actual entry date for T+1")
        if day <= entry_date:
            return MatchResult(None, "t_plus_one")
        if type(order.shares) is not int or order.shares <= 0:
            raise ValueError("Sell shares must be a positive integer")
    if bar.status == "suspended":
        return MatchResult(None, "suspended")
    if bar.limit_down is None or bar.limit_up is None:
        return MatchResult(None, "price_limits_unknown", False)
    low = _decimal(bar.low, "minute low", positive=True)
    high = _decimal(bar.high, "minute high", positive=True)
    lower = _decimal(bar.limit_down, "limit down", positive=True)
    upper = _decimal(bar.limit_up, "limit up", positive=True)
    amount = _decimal(bar.amount, "minute amount")
    volume = _decimal(bar.volume_shares, "minute volume in shares")
    tick = _decimal(bar.tick, "tick", positive=True)
    if volume != volume.to_integral_value() or low > high or lower > upper:
        raise ValueError("Invalid minute data or price boundaries")
    if volume == 0 or amount == 0:
        if volume != 0 or amount != 0:
            return MatchResult(None, "inconsistent_minute_data", False)
        return MatchResult(None, "no_trades")
    if order.side == "buy" and high >= upper:
        return MatchResult(None, "buy_limit_touched")
    if order.side == "sell" and low <= lower:
        return MatchResult(None, "sell_limit_touched")
    vwap = amount / volume
    if not low <= vwap <= high:
        return MatchResult(None, "inconsistent_minute_data", False)
    slip = _decimal(slippage_bps, "slippage_bps") / Decimal(10_000)
    if slip >= 1:
        raise ValueError("Slippage must be below 100 percent")
    slipped = vwap * (1 + slip if order.side == "buy" else 1 - slip)
    rounding = ROUND_CEILING if order.side == "buy" else ROUND_FLOOR
    price = (slipped / tick).to_integral_value(rounding=rounding) * tick
    if not low <= price <= high:
        return MatchResult(None, "slipped_price_outside_bar")
    if not lower <= price <= upper:
        return MatchResult(None, "slipped_price_outside_limits")
    capacity = int((volume * Decimal("0.01")).to_integral_value(rounding=ROUND_FLOOR))
    if order.side == "sell":
        shares = order.shares if order.shares <= capacity else (capacity // 100) * 100
    else:
        budget = _decimal(order.budget, "budget")
        cash = budget if available_cash is None else _decimal(available_cash, "available_cash")
        budget = min(budget, cash)
        max_lots = min(capacity // 100, int(budget // (price * 100)))
        lo, hi = 0, max_lots
        while lo < hi:
            mid = (lo + hi + 1) // 2
            gross = price * mid * 100
            costs = Decimal(str(order_fees(float(gross), day, "buy", already_notional)))
            if gross + costs <= budget:
                lo = mid
            else:
                hi = mid - 1
        shares = lo * 100
    if shares <= 0:
        return MatchResult(None, "capacity_below_lot" if capacity < 100 else "budget_below_lot")
    gross = float(price * shares)
    fees = fee_breakdown(gross, day, order.side, already_notional)
    fill = Fill(
        order.parent_id, order.symbol, order.side, bar.start, shares, float(price), gross, fees
    )
    return MatchResult(fill, "filled")


@dataclass(frozen=True)
class ExitIntent:
    parent_id: str
    triggered_at: datetime
    first_window: datetime | None
    reason: str


@dataclass(frozen=True)
class DividendReceivable:
    action_id: str
    amount: float
    pay_date: date


@dataclass(frozen=True)
class Position:
    symbol: str
    shares: int
    entry_date: date
    entry_notional: float
    entry_fees: float
    exit_intent: ExitIntent | None = None
    dividends_received: float = 0.0
    receivables: tuple[DividendReceivable, ...] = ()
    applied_actions: frozenset[str] = frozenset()
    sale_proceeds_net: float = 0.0

    def __post_init__(self) -> None:
        _mainboard(self.symbol)
        if type(self.shares) is not int or self.shares < 0 or type(self.entry_date) is not date:
            raise ValueError("Position requires an entry date and nonnegative integer shares")
        _decimal(self.entry_notional, "entry notional", positive=True)
        _decimal(self.entry_fees, "entry fees")
        _decimal(self.dividends_received, "dividends received")

    @property
    def dividend_receivable(self) -> float:
        return float(sum((Decimal(str(item.amount)) for item in self.receivables), Decimal(0)))


def position_from_buy(fill: Fill) -> Position:
    if fill.side != "buy" or fill.shares <= 0 or fill.shares % 100:
        raise ValueError("Opening position requires a valid mainboard buy fill")
    return Position(
        fill.symbol, fill.shares, fill.window_start.date(), fill.notional, fill.fees.total
    )


def schedule_exit(
    position: Position,
    day: date,
    calendar: Sequence[date],
    *,
    raw_close: float | None = None,
    adjusted_close: float | None = None,
    ma60: float | None = None,
    risk: bool = False,
    holding_days: int = 10,
) -> Position:
    """Called only after this day's close. An existing exit is never canceled."""
    if position.shares == 0 or position.exit_intent is not None:
        return position
    days = _calendar(calendar)
    if day not in days or position.entry_date not in days or day < position.entry_date:
        raise ValueError("Position dates are outside the supplied market calendar")
    if type(holding_days) is not int or holding_days < 1:
        raise ValueError("holding_days must be a positive integer")
    held = days.index(day) - days.index(position.entry_date) + 1
    reason = ""
    if risk:
        if raw_close is None or adjusted_close is None or ma60 is None:
            raise ValueError("Risk exit requires complete close and MA60 inputs")
        mark = _decimal(raw_close, "raw close", positive=True)
        adjusted = _decimal(adjusted_close, "adjusted close", positive=True)
        average = _decimal(ma60, "MA60", positive=True)
        initial = _decimal(position.entry_notional, "entry notional", positive=True)
        value = position.shares * mark + Decimal(str(position.dividends_received))
        value += Decimal(str(position.dividend_receivable))
        if value <= initial * Decimal("0.93"):
            reason = "stop_loss"
        elif adjusted < average:
            reason = "below_ma60"
        elif value >= initial * Decimal("1.15"):
            reason = "take_profit"
    if not reason and held >= holding_days:
        reason = "holding_period"
    if not reason:
        return position
    index = days.index(day) + 1
    first = datetime.combine(days[index], time(9, 31)) if index < len(days) else None
    intent = ExitIntent(
        f"sell:{position.symbol}:{position.entry_date}",
        datetime.combine(day, time(15)),
        first,
        reason,
    )
    return replace(position, exit_intent=intent)


def exit_order(position: Position) -> Order | None:
    """Recreate the pending parent's remaining quantity without resetting its intent."""
    intent = position.exit_intent
    if not position.shares or intent is None or intent.first_window is None:
        return None
    return Order(
        intent.parent_id,
        position.symbol,
        "sell",
        intent.triggered_at,
        intent.first_window,
        shares=position.shares,
    )


def apply_sell_fill(position: Position, fill: Fill) -> Position:
    intent = position.exit_intent
    if (
        fill.side != "sell"
        or fill.symbol != position.symbol
        or fill.shares <= 0
        or fill.shares > position.shares
        or fill.window_start.date() <= position.entry_date
        or intent is None
        or intent.first_window is None
        or fill.parent_id != intent.parent_id
        or fill.window_start < intent.first_window
    ):
        raise ValueError("Fill cannot be applied to this position")
    proceeds = Decimal(str(position.sale_proceeds_net)) + Decimal(str(fill.notional))
    proceeds -= Decimal(str(fill.fees.total))
    return replace(
        position, shares=position.shares - fill.shares, sale_proceeds_net=float(proceeds)
    )


@dataclass(frozen=True)
class CorporateAction:
    action_id: str
    symbol: str
    effective_date: date
    known_at: date
    share_multiplier: float = 1.0
    cash_per_share: float = 0.0
    pay_date: date | None = None
    complete: bool = True


@dataclass(frozen=True)
class ActionResult:
    position: Position
    complete: bool
    reason: str


def apply_corporate_action(
    position: Position, action: CorporateAction, *, day: date
) -> ActionResult:
    """Apply a verified entitlement before ex-date trading, using pre-action shares.

    Cash per share is gross of dividend tax. Share multipliers must produce an
    integer holding; absent fractional-share settlement is incomplete. This
    layer never changes execution prices to adjusted prices. Date-only source
    knowledge is usable on the next date, so an ex-date release without an
    earlier timestamp cannot be assumed known before that day's trading.
    """
    if action.action_id in position.applied_actions:
        return ActionResult(position, True, "already_applied")
    if action.symbol != position.symbol or day != action.effective_date:
        raise ValueError("Corporate action symbol or effective date does not match")
    if not action.action_id or type(action.known_at) is not date or type(day) is not date:
        raise ValueError(
            "Corporate action requires a stable ID and explicit effective/knowledge dates"
        )
    if not action.complete or action.known_at >= day:
        return ActionResult(position, False, "corporate_action_unknown")
    if position.entry_date >= day or position.shares == 0:
        return ActionResult(position, True, "no_entitlement")
    multiplier = _decimal(action.share_multiplier, "share multiplier", positive=True)
    cash_per_share = _decimal(action.cash_per_share, "cash per share")
    shares = position.shares * multiplier
    if shares != shares.to_integral_value():
        return ActionResult(position, False, "fractional_share_settlement_unknown")
    receivables = position.receivables
    if cash_per_share:
        if action.pay_date is None or action.pay_date < day:
            return ActionResult(position, False, "dividend_payment_date_unknown")
        amount = (position.shares * cash_per_share).quantize(_CENT, ROUND_HALF_UP)
        receivables += (DividendReceivable(action.action_id, float(amount), action.pay_date),)
    return ActionResult(
        replace(
            position,
            shares=int(shares),
            receivables=receivables,
            applied_actions=position.applied_actions | {action.action_id},
        ),
        True,
        "applied",
    )


def settle_dividends(position: Position, *, day: date) -> tuple[Position, float]:
    """Return newly available cash; keep earned receivables even after shares sell."""
    amount = sum(
        (Decimal(str(item.amount)) for item in position.receivables if item.pay_date <= day),
        Decimal(0),
    )
    pending = tuple(item for item in position.receivables if item.pay_date > day)
    return replace(
        position,
        dividends_received=float(Decimal(str(position.dividends_received)) + amount),
        receivables=pending,
    ), float(amount)


@dataclass(frozen=True)
class Valuation:
    value: float | None
    is_open: bool
    complete: bool
    reason: str


def position_value(
    position: Position,
    *,
    raw_mark: float | None,
    delisted: bool = False,
    known_residual_per_share: float | None = None,
) -> Valuation:
    """Value shares plus receivables; paid dividends already belong to cash.

    A last known suspended-day raw mark may be supplied and must be labeled
    stale by the report. Unknown marks are incomplete. Delisting with no known
    realizable residual uses zero but leaves the position explicitly open.
    """
    reason = "marked"
    if position.shares == 0:
        market = Decimal(0)
    elif delisted:
        residual = 0.0 if known_residual_per_share is None else known_residual_per_share
        market = position.shares * _decimal(residual, "known residual")
        reason = (
            "delisted_zero_residual" if known_residual_per_share is None else "delisted_residual"
        )
    elif raw_mark is None:
        return Valuation(None, True, False, "valuation_unknown")
    else:
        market = position.shares * _decimal(raw_mark, "raw mark", positive=True)
    value = market + Decimal(str(position.dividend_receivable))
    return Valuation(float(value), position.shares > 0, True, reason)


def closed_return(position: Position) -> float | None:
    """Net return only for actually closed holdings; pending dividends remain assets."""
    if position.shares:
        return None
    spent = _decimal(position.entry_notional, "entry notional", positive=True)
    spent += _decimal(position.entry_fees, "entry fees")
    earned = Decimal(str(position.sale_proceeds_net)) + Decimal(str(position.dividends_received))
    earned += Decimal(str(position.dividend_receivable))
    return float((earned - spent) / spent)
