"""Independent stock-day returns under an explicitly ideal daily-bar model.

``raw_daily.open/high/low/close`` must be unadjusted exchange prices.
``candidates.limit_up_price`` is the exact limit, not the runtime threshold that
already includes a 0.005 tolerance. No repository access, fills or account writes.
"""
from __future__ import annotations

import math
from collections import Counter
from datetime import date
from itertools import pairwise

import polars as pl

PRICE_EPSILON = 1e-8

LIMITATIONS = [
    "理想模型：触及涨停价即假设买入，一字涨停也假设成交，不验证封单排队或成交容量。",
    "次一市场交易日必须退出；有有效日线时假设可以卖出，包含现实中可能卖不出的跌停。",
    "未触发止损时使用事后已知的次日最高价；这是理想卖点，不是可执行的择时信号。",
    "次日盘中触发止损时按止损优先约定；日线无法确定最高价与止损的先后，另列最高价收益上界。",
    "次日缺行情、停牌或价格不可比时排除，不顺延，也不虚构必卖成交。",
    "费用按每股比例计双边佣金和卖出印花税，不含最低佣金，理想模型滑点为零。",
    "同股同日多个形态合并；收益为独立股票日样本的均值，不复利，不代表组合收益。",
    "输入须已核验原价和跨日可比性；除权、拆并股或其他质量问题由 exclude_reason 明示后排除。",
]

REASONS = {
    "source_excluded": "输入已标记不可用于本模型",
    "signal_not_in_calendar": "信号日期不在给定市场交易日历中",
    "invalid_limit_price": "涨停价须为有限、正数且小于10000的分币价格，不能使用含容差的判定阈值",
    "entry_bar_missing": "缺少信号当日原价日线",
    "entry_prices_invalid": "信号当日原价 OHLC 无效或相互矛盾",
    "entry_price_outside_bar": "信号当日最低价高于入场涨停价，日线未证明该价发生交易",
    "entry_high_above_limit": "信号当日最高价高于精确涨停价超过一分钱，输入证据矛盾",
    "entry_suspended": "信号当日成交量为零，不能确认触板交易",
    "entry_volume_invalid": "信号当日成交量缺失或无效",
    "not_touched": "信号当日最高价未触及精确涨停价",
    "next_session_missing": "日历未提供信号后的下一市场交易日",
    "nextday_missing": "缺少下一市场交易日日线，不顺延退出",
    "exit_prices_invalid": "次日原价 OHLC 无效或相互矛盾",
    "exit_suspended": "下一市场交易日停牌，不虚构退出",
    "exit_volume_invalid": "下一市场交易日成交量缺失或无效",
    "model_values_invalid": "价格或费用超出可计算范围",
}


def _keys(frame: pl.DataFrame, label: str) -> pl.DataFrame:
    if not {"symbol", "date"}.issubset(frame.columns):
        raise ValueError(f"{label}缺少 symbol/date")
    if frame.schema["date"] == pl.String:
        frame = frame.with_columns(pl.col("date").str.to_date("%Y-%m-%d", strict=False))
    elif frame.schema["date"] != pl.Date:
        raise ValueError(f"{label}日期必须为交易日期，不能使用时间戳或整数")
    if frame.schema["symbol"] != pl.String:
        raise ValueError(f"{label}股票代码必须为字符串")
    if frame.filter(pl.col("date").is_null() | pl.col("symbol").is_null()
                    | (pl.col("symbol").str.strip_chars() == "")).height:
        raise ValueError(f"{label}存在无效股票代码或交易日期")
    return frame


def _numeric(frame: pl.DataFrame, column: str) -> pl.Expr:
    if column not in frame.columns or frame.schema[column] == pl.Boolean:
        return pl.lit(None, dtype=pl.Float64).alias(column)
    return pl.col(column).cast(pl.Float64, strict=False).alias(column)


def _reason(frame: pl.DataFrame) -> pl.Expr:
    return (pl.col("exclude_reason").cast(pl.String, strict=False).fill_null("").str.strip_chars()
            if "exclude_reason" in frame.columns else pl.lit(""))


def _prices_valid(prefix: str) -> pl.Expr:
    prices = [pl.col(f"{prefix}_{key}") for key in ("open", "high", "low", "close")]
    opening, high, low, close = prices
    return (pl.all_horizontal([value.is_finite() & (value > 0) for value in prices])
            & (high >= low) & opening.is_between(low, high) & close.is_between(low, high)).fill_null(False)


def _bars(raw_daily: pl.DataFrame) -> pl.DataFrame:
    if raw_daily.is_empty():
        return pl.DataFrame(schema={"symbol": pl.String, "date": pl.Date,
                                    **{key: pl.Float64 for key in ("open", "high", "low", "close", "volume")},
                                    "source_reason": pl.String, "present": pl.Boolean})
    raw_daily = _keys(raw_daily, "原价日线")
    if raw_daily.select(pl.struct("symbol", "date").is_duplicated().any()).item():
        raise ValueError("原价日线存在重复股票交易日")
    return raw_daily.select("symbol", "date", *[
        _numeric(raw_daily, key) for key in ("open", "high", "low", "close", "volume")
    ], _reason(raw_daily).alias("source_reason"), pl.lit(True).alias("present"))


def _json_safe(value):
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def evaluate_first_board_ideal(
    candidates: pl.DataFrame, raw_daily: pl.DataFrame, trading_days: list[date], *,
    stop_loss_pct: float = .05, commission_pct: float = .0003, stamp_tax_pct: float = .0005,
) -> dict:
    """Evaluate limit-price entry on T and an ideal mandatory exit on T+1.

    An optional ``exclude_reason`` on either input takes priority over price
    calculations for that stock-day. Calendar order is authoritative; no bar
    search may substitute a later trading day for a missing T+1 bar.
    """
    for key, value in {"stop_loss_pct": stop_loss_pct, "commission_pct": commission_pct,
                       "stamp_tax_pct": stamp_tax_pct}.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"{key}必须为有限小数比例")
    if not 0 < stop_loss_pct < 1 or min(commission_pct, stamp_tax_pct) < 0 or commission_pct + stamp_tax_pct >= 1:
        raise ValueError("止损比例须在0和1之间，费用非负且卖出总费率小于1")
    if any(type(day) is not date for day in trading_days) or any(a >= b for a, b in pairwise(trading_days)):
        raise ValueError("市场日历必须为严格递增且不重复的交易日期")
    report = {
        "model": "first_board_ideal_daily_v1", "ideal": True, "price_basis": "raw",
        "parameters": {"stop_loss_pct": stop_loss_pct, "commission_pct": commission_pct,
                       "stamp_tax_pct": stamp_tax_pct, "slippage_bps": 0},
        "limitations": list(LIMITATIONS), "trades": [], "exclusions": [],
        "summary": {"signal_rows": candidates.height, "signals": 0, "touched": 0,
                    "not_touched": 0, "trades": 0, "excluded": 0, "data_excluded": 0,
                    "reason_counts": {}, "mean_net_return": None, "median_net_return": None,
                    "mean_gross_return": None, "win_rate": None, "ambiguous_count": 0,
                    "mean_high_upper_bound_net_return": None},
    }
    if candidates.is_empty():
        return report
    candidates = _keys(candidates, "候选")
    if "pattern" not in candidates.columns or candidates.schema["pattern"] != pl.String:
        raise ValueError("候选缺少字符串形态 pattern")
    if candidates.filter(pl.col("pattern").is_null() | (pl.col("pattern").str.strip_chars() == "")).height:
        raise ValueError("候选形态不能为空")
    if candidates.select(pl.struct("symbol", "date", "pattern").is_duplicated().any()).item():
        raise ValueError("候选存在重复股票、日期和形态")
    candidates = candidates.with_columns(_numeric(candidates, "limit_up_price"),
                                        _reason(candidates).alias("source_reason"))
    if candidates.group_by("symbol", "date").agg(pl.col("limit_up_price").n_unique().alias("n")).filter(pl.col("n") > 1).height:
        raise ValueError("同股同日多个形态的精确涨停价不一致")
    if "name" not in candidates.columns:
        candidates = candidates.with_columns(pl.lit(None, dtype=pl.String).alias("name"))
    pool = candidates.group_by("symbol", "date").agg(
        pl.col("pattern").unique().sort().alias("patterns"), pl.col("name").first(),
        pl.col("limit_up_price").first().alias("entry_price"),
        pl.col("source_reason").filter(pl.col("source_reason") != "").unique().sort().str.join("；"),
    )
    calendar = pl.DataFrame({"date": trading_days, "next_trading_day": [*trading_days[1:], None]
                            if trading_days else []}, schema={"date": pl.Date, "next_trading_day": pl.Date})
    pool = pool.join(calendar.with_columns(pl.lit(True).alias("in_calendar")), on="date", how="left")
    bars = _bars(raw_daily)
    for prefix, day_column in (("entry", "date"), ("exit", "next_trading_day")):
        renamed = bars.rename({key: f"{prefix}_{key}" for key in bars.columns if key not in {"symbol", "date"}})
        pool = pool.join(renamed, left_on=["symbol", day_column], right_on=["symbol", "date"], how="left")
    pool = pool.with_columns(_prices_valid("entry").alias("entry_valid"), _prices_valid("exit").alias("exit_valid"),
                            (pl.col("entry_price") * (1 - stop_loss_pct)).round(10).alias("stop_price"),
                            (pl.col("entry_price").is_finite() & (pl.col("entry_price") > 0)
                             & (pl.col("entry_price") < 10000)
                             & ((pl.col("entry_price") - pl.col("entry_price").round(2)).abs() <= PRICE_EPSILON))
                            .fill_null(False).alias("limit_valid"))
    entry_quality = ((pl.col("source_reason") == "") & (pl.col("entry_source_reason").fill_null("") == "")
                     & pl.col("in_calendar").fill_null(False) & pl.col("limit_valid")
                     & pl.col("entry_present").fill_null(False)
                     & pl.col("entry_valid") & (pl.col("entry_low") <= pl.col("entry_price") + PRICE_EPSILON)
                     & (pl.col("entry_high") <= pl.col("entry_price") + .01 + PRICE_EPSILON)
                     & pl.col("entry_volume").is_finite() & (pl.col("entry_volume") > 0))
    pool = pool.with_columns(pl.when(entry_quality).then(pl.col("entry_high") >= pl.col("entry_price") - PRICE_EPSILON)
                            .otherwise(None).alias("touched"))
    reason = (pl.when(pl.col("source_reason") != "").then(pl.lit("source_excluded"))
              .when(pl.col("entry_source_reason").fill_null("") != "").then(pl.lit("source_excluded"))
              .when(~pl.col("in_calendar").fill_null(False)).then(pl.lit("signal_not_in_calendar"))
              .when(~pl.col("limit_valid")).then(pl.lit("invalid_limit_price"))
              .when(~pl.col("entry_present").fill_null(False)).then(pl.lit("entry_bar_missing"))
              .when(~pl.col("entry_valid")).then(pl.lit("entry_prices_invalid"))
              .when(pl.col("entry_low") > pl.col("entry_price") + PRICE_EPSILON).then(pl.lit("entry_price_outside_bar"))
              .when(pl.col("entry_high") > pl.col("entry_price") + .01 + PRICE_EPSILON).then(pl.lit("entry_high_above_limit"))
              .when(pl.col("entry_volume") == 0).then(pl.lit("entry_suspended"))
              .when(~(pl.col("entry_volume").is_finite() & (pl.col("entry_volume") > 0)).fill_null(False)).then(pl.lit("entry_volume_invalid"))
              .when(~pl.col("touched")).then(pl.lit("not_touched"))
              .when(pl.col("next_trading_day").is_null()).then(pl.lit("next_session_missing"))
              .when(pl.col("exit_source_reason").fill_null("") != "").then(pl.lit("source_excluded"))
              .when(~pl.col("exit_present").fill_null(False)).then(pl.lit("nextday_missing"))
              .when(~pl.col("exit_valid")).then(pl.lit("exit_prices_invalid"))
              .when(pl.col("exit_volume") == 0).then(pl.lit("exit_suspended"))
              .when(~(pl.col("exit_volume").is_finite() & (pl.col("exit_volume") > 0)).fill_null(False)).then(pl.lit("exit_volume_invalid"))
              .otherwise(pl.lit(None, dtype=pl.String)))
    pool = pool.with_columns(reason.alias("reason_code"))
    valid = pool.filter(pl.col("reason_code").is_null()).with_columns(
        pl.when(pl.col("exit_open") <= pl.col("stop_price")).then(pl.lit("gap_stop"))
        .when(pl.col("exit_low") <= pl.col("stop_price")).then(pl.lit("intraday_stop"))
        .otherwise(pl.lit("next_day_high")).alias("exit_reason"),
    ).with_columns(
        pl.when(pl.col("exit_reason") == "gap_stop").then(pl.col("exit_open"))
        .when(pl.col("exit_reason") == "intraday_stop").then(pl.col("stop_price"))
        .otherwise(pl.col("exit_high")).alias("exit_price"),
        (pl.col("entry_price") * (1 + commission_pct)).alias("entry_cost"),
    ).with_columns(
        (pl.col("exit_price") * (1 - commission_pct - stamp_tax_pct)).alias("exit_proceeds"),
        (pl.col("exit_price") / pl.col("entry_price") - 1).alias("gross_return"),
        (pl.col("exit_price") * (1 - commission_pct - stamp_tax_pct) / pl.col("entry_cost") - 1).alias("net_return"),
        (pl.col("exit_high") * (1 - commission_pct - stamp_tax_pct) / pl.col("entry_cost") - 1).alias("high_upper_bound_net_return"),
    )
    finite = pl.all_horizontal([pl.col(key).is_finite() for key in
                               ("entry_cost", "exit_proceeds", "gross_return", "net_return", "high_upper_bound_net_return")])
    invalid_math = valid.filter(~finite).select(pool.columns).with_columns(pl.lit("model_values_invalid").alias("reason_code"))
    excluded = pl.concat([pool.filter(pl.col("reason_code").is_not_null()), invalid_math])
    valid = valid.filter(finite)
    for row in excluded.sort("date", "symbol").iter_rows(named=True):
        code = row["reason_code"]
        source = row["source_reason"] or row["entry_source_reason"] or row["exit_source_reason"]
        report["exclusions"].append({
            "symbol": row["symbol"], "name": row["name"], "date": row["date"], "patterns": row["patterns"],
            "required_exit_date": row["next_trading_day"], "touched": row["touched"],
            "entry_price": row["entry_price"] if row["touched"] else None, "exit_price": None,
            "reason_code": code, "reason": source if code == "source_excluded" else REASONS[code],
            "category": "not_triggered" if code == "not_touched" else "data_unavailable", "ideal": True,
        })
    for row in valid.sort("date", "symbol").iter_rows(named=True):
        record = {key: row[key] for key in ("symbol", "name", "date", "patterns", "entry_price", "stop_price",
                                           "exit_price", "exit_reason", "entry_cost", "exit_proceeds",
                                           "gross_return", "net_return", "high_upper_bound_net_return")}
        record.update(entry_date=row["date"], exit_date=row["next_trading_day"], ideal=True,
                      daily_sequence_ambiguous=row["exit_reason"] == "intraday_stop",
                      one_price_entry=len({row[f"entry_{key}"] for key in ("open", "high", "low", "close")}) == 1,
                      next_open=row["exit_open"], next_high=row["exit_high"], next_low=row["exit_low"], next_close=row["exit_close"],
                      buy_commission=row["entry_price"] * commission_pct,
                      sell_commission=row["exit_price"] * commission_pct, stamp_tax=row["exit_price"] * stamp_tax_pct)
        report["trades"].append(record)
    summary = report["summary"]
    summary.update(signals=pool.height, touched=pool.filter(pl.col("touched") == True).height,  # noqa: E712
                   not_touched=excluded.filter(pl.col("reason_code") == "not_touched").height,
                   trades=valid.height, excluded=excluded.height,
                   data_excluded=excluded.filter(pl.col("reason_code") != "not_touched").height,
                   reason_counts=dict(sorted(Counter(excluded["reason_code"].to_list()).items())),
                   ambiguous_count=valid.filter(pl.col("exit_reason") == "intraday_stop").height)
    if valid.height:
        summary.update(mean_net_return=valid["net_return"].mean(), median_net_return=valid["net_return"].median(),
                       mean_gross_return=valid["gross_return"].mean(), win_rate=(valid["net_return"] > 0).mean(),
                       mean_high_upper_bound_net_return=valid["high_upper_bound_net_return"].mean())
    return _json_safe(report)
