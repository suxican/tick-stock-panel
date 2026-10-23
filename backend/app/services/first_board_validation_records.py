"""Read/write contract for saved first-board validation reports, without migration."""
from __future__ import annotations

import json
import re
from datetime import date, datetime
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, FiniteFloat, model_validator

from app.backtest.first_board_execution import ExecutionOptions


def _date(value: str) -> str:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("invalid calendar date")
    date.fromisoformat(value)
    return value


def _timestamp(value: str) -> str:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp requires timezone")
    return value


Day = Annotated[str, AfterValidator(_date)]
Timestamp = Annotated[str, AfterValidator(_timestamp)]
Count = Annotated[int, Field(ge=0)]
Identifier = Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]
Value = FiniteFloat | None


class _Contract(BaseModel):
    model_config = ConfigDict(strict=True, extra="allow", allow_inf_nan=False)


class _Request(_Contract):
    start: Day
    end: Day
    revision: Count
    account_id: str | None
    options: ExecutionOptions | None
    through: Day

    @model_validator(mode="after")
    def dates(self):
        if self.start > self.end or self.through < self.end or (date.fromisoformat(self.end) - date.fromisoformat(self.start)).days >= 92:
            raise ValueError("invalid report window")
        return self


class _ReplaySummary(_Contract):
    signals: Count
    closed: Count
    no_fill: Count
    unverifiable: Count
    open_position: Count
    win_count: Count
    net_return_mean: Value
    gross_return_mean: Value
    win_rate: Value
    net_pnl_sum: Value


class _ReplayCoverage(_Contract):
    sample_count: Count
    stock_days: Count
    eligible_stock_days: Count
    negative_stock_days: Count
    unknown_stock_days: Count
    signal_count: Count
    calendar_available: bool
    minute_days_read: Count


class _ReplayTrade(_Contract):
    signal_id: str
    symbol: str
    name: str | None
    date: Day
    revision: Count
    patterns: list[str]
    group: Literal["S0F0", "S1F0", "S0F1", "S1F1", "unknown"]
    signal_at: Timestamp
    order_at: Timestamp | None
    entry_at: Timestamp | None
    exit_signal_at: Timestamp | None
    exit_order_at: Timestamp | None
    exit_at: Timestamp | None
    entry_bar_at: Timestamp | None = None
    exit_signal_bar_at: Timestamp | None = None
    exit_bar_at: Timestamp | None = None
    qty: Value
    entry_raw_price: Value
    entry_price: Value
    exit_raw_price: Value
    exit_price: Value
    buy_fee: Value
    sell_fee: Value
    entry_cost: Value
    exit_proceeds: Value
    gross_return: Value
    net_return: Value
    net_pnl: Value
    status: Literal["no_fill", "unverifiable", "open_position", "closed"]
    reasons: list[str]


class _ReplayModel(_Contract):
    id: str
    label: str
    price_proxy: Literal["later_complete_minute_close"]
    signal_source: Literal["recorded_can_buy"]
    options: ExecutionOptions


class _BaseResult(_Contract):
    id: Identifier
    created_at: Timestamp
    request: _Request
    limitations: list[str]


class _ReplayResult(_BaseResult):
    kind: Literal["replay"]
    schema_version: Literal[1]
    mode: Literal["recorded_execution"]
    status: Literal["modelled", "insufficient_evidence", "no_signals"]
    model: _ReplayModel
    coverage: _ReplayCoverage
    summary: _ReplaySummary
    by_group: dict[str, _ReplaySummary]
    trades: list[_ReplayTrade]
    total_trades: Count
    trades_truncated: bool
    fingerprint: str


class _PaperCoverage(_Contract):
    signal_count: Count
    displayed_count: Count
    detail_limit: Count
    ledger_order_count: Count
    ledger_fill_count: Count
    attributable_count: Count
    unknown_count: Count
    legacy_fee_default_count: Count
    truncated: bool


class _PaperSummary(_Contract):
    submitted_signals: Count
    filled_signals: Count
    closed_signals: Count
    open_signals: Count
    partially_closed_signals: Count
    unfilled_signals: Count
    unsubmitted_signals: Count
    unknown_signals: Count
    realized_net_pnl: Value
    closed_net_pnl: Value
    closed_win_rate: Value
    closed_mean_net_return: Value


class _PaperTrade(_Contract):
    signal_id: str
    symbol: str
    signal_at: Timestamp | None
    revision: Count
    status: Literal["not_ordered", "pending", "cancelled", "expired", "open", "partially_closed", "closed", "evidence_missing"]
    reason: str
    orders: list[dict[str, Any]]
    fills: list[dict[str, Any]]
    exits: list[dict[str, Any]]
    remaining_qty: Value
    realized_net_pnl: Value
    closed_net_return: Value


class _PaperResult(_BaseResult):
    kind: Literal["paper"]
    status: Literal["ready", "incomplete", "unavailable", "error"]
    reason: str
    account_id: str | None
    coverage: _PaperCoverage
    summary: _PaperSummary
    trades: list[_PaperTrade]
    fingerprint: str | None


class _Run(_Contract):
    id: Identifier
    created_at: Timestamp
    kind: Literal["replay", "paper"]
    result: Annotated[_ReplayResult | _PaperResult, Field(discriminator="kind")]


def _check_record_times(value: object) -> None:
    """Paper trace records retain their existing schema; validate known dates too."""
    if isinstance(value, list):
        for item in value:
            _check_record_times(item)
    elif isinstance(value, dict):
        for key, item in value.items():
            if item is not None and key in {"created_at", "filled_at", "ts"}:
                if not isinstance(item, str):
                    raise ValueError("invalid trace timestamp")
                _timestamp(item)
            elif item is not None and key == "date":
                if not isinstance(item, str):
                    raise ValueError("invalid trace date")
                _date(item)
            _check_record_times(item)


def validate_trade_validation_run(value: dict) -> None:
    """Validate without rewriting historical payloads or inventing missing fields."""
    try:
        # Also rejects nonfinite values in additive fields and retained trace records.
        json.dumps(value, allow_nan=False)
        run = _Run.model_validate(value)
        result = run.result
        if run.id != result.id or run.kind != result.kind or run.created_at != result.created_at:
            raise ValueError("report identity mismatch")
        if isinstance(result, _ReplayResult):
            if result.request.account_id is not None or result.request.options != result.model.options:
                raise ValueError("replay parameters mismatch")
        elif result.request.options is not None or result.request.account_id != result.account_id or result.request.through != result.request.end:
            raise ValueError("paper parameters mismatch")
        for trade in result.trades:
            if isinstance(trade, _PaperTrade):
                _check_record_times([trade.orders, trade.fills, trade.exits])
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError("首板交易验证记录结构、日期或数值无效") from exc
