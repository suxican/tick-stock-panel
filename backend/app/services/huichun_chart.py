"""Read-only, dated A0 charts using the same preparation and indicators as scans."""

from __future__ import annotations

import math
from contextlib import suppress
from datetime import date

import polars as pl
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.services.huichun_audit import _table_exists
from app.services.huichun_calendar import load_market_calendar
from app.services.huichun_mode import (
    DATA_POLICY_VERSION,
    HuichunModeService,
    HuichunRules,
    _now,
    _screening_stock,
)
from app.strategy.huichun_a0 import A0Params, audit_a0


class HuichunChartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: str = Field(pattern=r"^\d{6}\.(SH|SZ)$")
    as_of: date
    rules: HuichunRules
    rule_revision: int = Field(ge=0, strict=True)
    start_date: date | None = None
    end_date: date | None = None

    @model_validator(mode="after")
    def valid_context(self):
        if not _screening_stock(self.symbol):
            raise ValueError("回春图表仅支持沪深 A 股")
        end = self.end_date or self.as_of
        if end > self.as_of or (self.start_date and self.start_date > end):
            raise ValueError("展示窗口必须位于观察截止日以内且开始日期不晚于结束日期")
        return self


def _chart_calendar(service: HuichunModeService, as_of: date) -> tuple[list[date], str]:
    """Keep the calendar local: a chart must not mutate a concurrent scan's context."""
    source = "observed_market_daily_dates"
    if not _table_exists(service.repo, "kline_daily"):
        return [], source
    observed = [row[0] for row in service.repo.execute_all(
        "SELECT DISTINCT date FROM kline_daily WHERE date <= ? ORDER BY date", [as_of],
    )]
    if not observed or service._calendar_path is None:
        return observed, source
    try:
        calendar = load_market_calendar(service._calendar_path, start=observed[0], end=as_of)
    except (OSError, ValueError):
        return observed, source
    if set(observed) - set(calendar):
        raise ValueError("日线日期与独立交易日历冲突，请检查数据")
    return calendar, "official_exchange_calendar"


def build_huichun_chart(service: HuichunModeService, request: HuichunChartRequest) -> dict:
    """Calculate through the frozen cutoff, then crop without reseeding any EMA.

    OHLC and all price-valued indicators receive the same final normalization.
    The anchor is the last safe bar, which may precede the requested cutoff;
    metadata exposes that date rather than claiming a missing cutoff price.
    There is no live-candle, enriched-indicator or remote-provider fallback.
    """
    now = _now()
    if request.as_of > now.date() or (request.as_of == now.date() and now.hour < 15):
        raise ValueError("图表截止日必须是已收盘日期")
    before = service._input_signature()
    calendar, calendar_source = _chart_calendar(service, request.as_of)
    name = request.symbol
    listings = {}
    if _table_exists(service.repo, "instruments"):
        columns = {row[0] for row in service.repo.execute_all("DESCRIBE instruments")}
        listing_expr = "listing_date" if "listing_date" in columns else "NULL"
        instruments = service.repo.execute_all(
            f"SELECT name,{listing_expr} FROM instruments WHERE symbol = ?", [request.symbol],
        )
        if instruments:
            instrument_name, listing = instruments[0]
            name = instrument_name or name
            with suppress(ValueError, TypeError):
                listings[request.symbol] = date.fromisoformat(str(listing)[:10])
    metadata = {
        "as_of": str(request.as_of),
        "macd": [10, 20, 9],
        "price_basis": "as_of_adjusted",
        "price_basis_date": None,
        "latest_valid_date": None,
        "factor_status": "no_events_unverified",
        "invalid_from": None,
        "calendar_source": calendar_source,
        "rule_revision": request.rule_revision,
        "rules": request.rules.model_dump(),
        "data_policy_version": DATA_POLICY_VERSION,
        "data_status": "no_valid_data",
        "missing_observed_days": None,
        "segment_count": None,
    }
    response = {
        "symbol": request.symbol, "name": name, "stock_info": {"name": name},
        "rows": [], "source": "huichun_a0", "huichun": metadata,
    }
    if calendar:
        raw = service._raw([request.symbol], request.as_of)
        factors = service._factors([request.symbol], request.as_of)
        prepared, issues = service._prepare_scan_batch(
            raw, factors, calendar=calendar, end=request.as_of, listings=listings,
            expected_symbols=[request.symbol],
            calendar_verified=calendar_source == "official_exchange_calendar",
        )
        if request.symbol in issues:
            invalid_from = issues[request.symbol]
            metadata.update(
                factor_status="quarantined" if invalid_from is not None else "invalid_undated",
                invalid_from=str(invalid_from) if invalid_from is not None else None,
            )
        elif not prepared.coverage.is_empty():
            metadata["factor_status"] = prepared.coverage.row(0, named=True)["factor_status"]
        if not prepared.coverage.is_empty():
            coverage = prepared.coverage.row(0, named=True)
            metadata.update(
                missing_observed_days=coverage["missing_observed_days"],
                segment_count=coverage["segment_count"],
            )
        if not prepared.frame.is_empty():
            audited = audit_a0(
                prepared.frame, start=prepared.frame["date"].min(), end=request.as_of,
                params=A0Params(**request.rules.model_dump()),
            )
            indicators = audited.indicators
            prices = raw.select(
                "symbol", "date", "volume", "amount",
                *[pl.col(c).alias(f"raw_{c}") for c in ("open", "high", "low", "close")],
            )
            chart = indicators.join(prices, on=["symbol", "date"], how="inner").sort("date")
            last = chart.row(-1, named=True)
            scale = last["raw_close"] / last["close"]
            if not math.isfinite(scale) or scale <= 0:
                raise ValueError("无法建立可靠的图表复权基准")
            chart = chart.with_columns(
                (pl.col("close") / pl.col("raw_close") * scale).alias("_ohlc_scale"),
            ).select(
                "symbol", "date", "segment_id", "volume", "amount",
                *[(pl.col(f"raw_{c}") * pl.col("_ohlc_scale")).alias(c)
                  for c in ("open", "high", "low", "close")],
                (pl.col("ma60") * scale).alias("ma60"),
                (pl.col("ma60_lag") * scale).alias("ma60_lag"),
                (pl.col("dif") * scale).alias("macd_dif"),
                (pl.col("dea") * scale).alias("macd_dea"),
                (pl.col("q") * (2 * scale)).alias("macd_hist"),
                "zero_distance", "gap_distance", "golden_cross", "dead_cross",
            )
            end = request.end_date or request.as_of
            chart = chart.filter(pl.col("date") <= end)
            if request.start_date:
                chart = chart.filter(pl.col("date") >= request.start_date)
            response["rows"] = chart.with_columns(pl.col("date").cast(pl.String)).to_dicts()
            metadata.update(
                price_basis_date=str(last["date"]), latest_valid_date=str(last["date"]),
                data_status="end_reached" if last["date"] == request.as_of else "truncated",
            )
    if before != service._input_signature():
        raise ValueError("计算期间行情已更新，请重新打开图表以使用一致数据")
    return response
