"""首板实验规则: 完成态日线候选与当前快照评估共用的纯函数。

阈值是可检验的工程假设, 不代表课程给出了这些精确参数。所有比例为小数,
换手率沿用 enriched 的百分数值。该模块不判断行情时效、不推送、不模拟成交。
"""
from __future__ import annotations

import math
import re
from collections import Counter
from datetime import date
from typing import Literal, Self

import polars as pl
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.price_limits import (
    GEM_REGISTRATION_DATE,
    MAIN_BOARD_LIMIT,
    is_risk_warning_name,
    numpy_limit_price,
    polars_is_risk_warning_name,
    polars_limit_price,
    polars_price_limit_pct,
    price_limit_pct,
)

Pattern = Literal["platform", "trend", "oversold"]
PATTERN_LABELS = {"platform": "平台突破", "trend": "趋势加速", "oversold": "超跌反弹"}
_UNIVERSE_PATTERNS = {
    "main_board_non_st": r"^(60\d{4}\.SH|00\d{4}\.SZ)$",
    "hs_a_non_st": r"^(60\d{4}\.SH|00\d{4}\.SZ|30[01]\d{3}\.SZ|68[89]\d{3}\.SH)$",
}


class FirstBoardRules(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    universe: Literal["main_board_non_st", "hs_a_non_st"] = "hs_a_non_st"
    lookback_days: int = Field(default=10, ge=1, le=60)
    enabled_patterns: list[Pattern] = Field(default_factory=lambda: list(PATTERN_LABELS))
    min_history_days: int = Field(default=60, ge=20, le=250)
    platform_window: int = Field(default=20, ge=5, le=60)
    platform_max_range: float = Field(default=0.18, gt=0, le=1)
    platform_near_high: float = Field(default=0.08, ge=0, le=0.5)
    trend_window: int = Field(default=20, ge=10, le=60)
    trend_min_return: float = Field(default=0.03, ge=0, le=1)
    trend_max_return: float = Field(default=0.40, gt=0, le=2)
    oversold_window: int = Field(default=60, ge=20, le=120)
    oversold_min_drawdown: float = Field(default=0.25, gt=0, lt=1)
    approaching_distance: float = Field(default=0.03, ge=0, le=0.10)
    min_change_pct: float = Field(default=0.05, ge=0, le=0.20)
    min_turnover_rate: float = Field(default=2.0, ge=0, le=100)
    max_turnover_rate: float = Field(default=30.0, gt=0, le=100)
    min_amount: float = Field(default=50_000_000.0, ge=0, le=1e12)

    @model_validator(mode="after")
    def validate_ranges(self) -> Self:
        if self.trend_min_return > self.trend_max_return:
            raise ValueError("趋势涨幅下限不能大于上限")
        if self.min_turnover_rate > self.max_turnover_rate:
            raise ValueError("换手率下限不能大于上限")
        if len(set(self.enabled_patterns)) != len(self.enabled_patterns):
            raise ValueError("启用模式不能重复")
        return self


def required_history_bars(rules: FirstBoardRules) -> int:
    """包含当前行的加载长度; 额外一行提供历史涨停的前收盘分母。"""
    windows = {"platform": rules.platform_window, "trend": rules.trend_window,
               "oversold": rules.oversold_window}
    return max([rules.min_history_days, rules.lookback_days + 1,
                *[windows[pattern] for pattern in rules.enabled_patterns]]) + 1


_REQUIRED = {"symbol", "date", "name", "close", "high", "low", "raw_close"}
_FEATURE_COLUMNS = [
    "reference_date", "reference_price", "reference_adjusted_close", "_eligible",
    "platform_high", "platform_range", "platform_high_distance", "trend_return",
    "trend_mean", "recent_high", "oversold_drawdown", "history_bars", "first_board_count",
]


def _empty_candidates() -> pl.DataFrame:
    return pl.DataFrame(schema={
        "symbol": pl.String, "name": pl.String, "pattern": pl.String,
        "pattern_label": pl.String, "reference_date": pl.Date,
        "reference_price": pl.Float64, "reference_adjusted_close": pl.Float64,
        "breakout_price": pl.Float64, "state": pl.String,
    })


def _features(history: pl.DataFrame, rules: FirstBoardRules) -> pl.DataFrame:
    if history.is_empty() or not _REQUIRED.issubset(history.columns):
        return pl.DataFrame()
    df = history.with_columns(pl.col("date").cast(pl.Date, strict=False)).filter(
        pl.col("date").is_not_null())
    if df.select(pl.struct("symbol", "date").is_duplicated().any()).item():
        raise ValueError("首板历史存在重复股票交易日, 无法可靠判定")
    df = df.sort(["symbol", "date"])
    valid_prices = [
        pl.col(col).is_not_null() & pl.col(col).is_finite() & (pl.col(col) > 0)
        for col in ("close", "high", "low", "raw_close")
    ]
    valid_prices.extend([
        pl.col("high") >= pl.col("low"),
        pl.col("close").is_between(pl.col("low"), pl.col("high")),
    ])
    # Some historical strategy contexts omit open. When present it must obey
    # the same adjusted OHLC bounds, not turn a corrupt bar into a tight platform.
    if "open" in df.columns:
        valid_prices.append(pl.col("open").is_finite()
                            & pl.col("open").is_between(pl.col("low"), pl.col("high")))
    df = df.with_columns(
        pl.col("date").rank("dense").alias("_market_index"),
        pl.col("date").cum_count().over("symbol").alias("history_bars"),
        pl.when(pl.col("close").is_finite() & pl.col("raw_close").is_finite()
                & (pl.col("close") > 0) & (pl.col("raw_close") > 0))
        .then(pl.col("close") / pl.col("raw_close")).otherwise(None).alias("_adjustment"),
        pl.all_horizontal(valid_prices).alias("_valid"),
    )
    # 将昨日复权收盘还原到该日原价尺度, 除权日不会误用昨日未除权原价。
    previous = pl.col("close").shift(1).over("symbol")
    df = df.with_columns(
        pl.when(previous.is_finite() & (previous > 0))
        .then(previous / pl.col("_adjustment")).otherwise(None).alias("_reference_raw"),
        # 全局 helper 尚未覆盖创业板注册制前10%的历史规则, 首板统一补足。
        pl.when(pl.col("symbol").str.contains(r"^30[01]\d{3}\.SZ$")
                & (pl.col("date") < pl.lit(GEM_REGISTRATION_DATE)))
        .then(MAIN_BOARD_LIMIT)
        .otherwise(polars_price_limit_pct(pl.col("symbol"), pl.col("date"),
                                         polars_is_risk_warning_name(pl.col("name"))))
        .alias("_limit_pct"),
    ).with_columns(
        polars_limit_price(pl.col("_reference_raw"), pl.col("_limit_pct"), up=True)
        .alias("_limit_price"),
    ).with_columns(
        pl.when(pl.col("_valid") & pl.col("_reference_raw").is_finite()
                & (pl.col("_reference_raw") > 0))
        .then(pl.col("raw_close") >= pl.col("_limit_price") - 0.005)
        .otherwise(None).cast(pl.Int32).alias("_closed_limit"),
    )
    window = required_history_bars(rules) - 1
    df = df.with_columns(
        pl.col("_valid").cast(pl.Int32).rolling_sum(window, min_samples=window)
        .over("symbol").alias("_valid_count"),
        (pl.col("_market_index") - pl.col("_market_index").shift(window - 1).over("symbol"))
        .alias("_date_span"),
        pl.col("_closed_limit").rolling_sum(rules.lookback_days, min_samples=rules.lookback_days)
        .over("symbol").alias("first_board_count"),
        pl.col("high").rolling_max(rules.platform_window, min_samples=rules.platform_window)
        .over("symbol").alias("platform_high"),
        pl.col("low").rolling_min(rules.platform_window, min_samples=rules.platform_window)
        .over("symbol").alias("_platform_low"),
        pl.col("close").rolling_mean(rules.trend_window, min_samples=rules.trend_window)
        .over("symbol").alias("trend_mean"),
        (pl.col("close") / pl.col("close").shift(rules.trend_window - 1).over("symbol") - 1)
        .alias("trend_return"),
        pl.col("high").rolling_max(5, min_samples=5).over("symbol").alias("recent_high"),
        pl.col("high").rolling_max(rules.oversold_window, min_samples=rules.oversold_window)
        .over("symbol").alias("_oversold_high"),
    ).with_columns(
        (pl.col("platform_high") / pl.col("_platform_low") - 1).alias("platform_range"),
        (1 - pl.col("close") / pl.col("platform_high")).alias("platform_high_distance"),
        (1 - pl.col("close") / pl.col("_oversold_high")).alias("oversold_drawdown"),
        pl.col("date").alias("reference_date"),
        pl.col("raw_close").alias("reference_price"),
        pl.col("close").alias("reference_adjusted_close"),
    )
    in_universe = pl.col("symbol").str.contains(_UNIVERSE_PATTERNS[rules.universe])
    return df.with_columns((
        in_universe & pl.col("name").is_not_null() & (pl.col("name").str.len_chars() > 0)
        & ~pl.col("name").str.contains("(?i)ST|退")
        & (pl.col("history_bars") >= rules.min_history_days)
        & (pl.col("_valid_count") == window) & (pl.col("_date_span") == window - 1)
        & (pl.col("first_board_count") == 0)
    ).fill_null(False).alias("_eligible"))


def _pattern_condition(pattern: Pattern, rules: FirstBoardRules) -> pl.Expr:
    if pattern == "platform":
        return ((pl.col("platform_range") <= rules.platform_max_range)
                & (pl.col("platform_high_distance") <= rules.platform_near_high))
    if pattern == "trend":
        return (pl.col("trend_return").is_between(rules.trend_min_return, rules.trend_max_return)
                & (pl.col("reference_adjusted_close") > pl.col("trend_mean")))
    return pl.col("oversold_drawdown") >= rules.oversold_min_drawdown


def _pattern_rows(df: pl.DataFrame, rules: FirstBoardRules, pattern: Pattern) -> pl.DataFrame:
    return df.filter(pl.col("_eligible") & _pattern_condition(pattern, rules)).with_columns(
        pl.lit(pattern).alias("pattern"), pl.lit(PATTERN_LABELS[pattern]).alias("pattern_label"),
        pl.col("platform_high" if pattern == "platform" else "recent_high").alias("breakout_price"),
        pl.lit("watch").alias("state"),
    )


def build_candidates(history: pl.DataFrame, as_of: date, rules: FirstBoardRules) -> pl.DataFrame:
    """仅使用 T 前完成态日线; 输入日期集合须由仓库提供真实交易日。

    每只股票必须覆盖整个所需市场日期窗口; 不以停牌前旧行替代缺失日。
    name 是截至该日可用名称; 服务应在 enriched 无名称时由仓库批量补齐。
    """
    if history.is_empty() or "date" not in history.columns or not rules.enabled_patterns:
        return _empty_candidates()
    past = history.filter(pl.col("date").cast(pl.Date, strict=False) < as_of)
    df = _features(past, rules)
    if df.is_empty():
        return _empty_candidates()
    latest = df.filter(pl.col("date") == df["date"].max())
    rows = [_pattern_rows(latest, rules, pattern) for pattern in rules.enabled_patterns]
    result = pl.concat(rows, how="diagonal_relaxed")
    return result.drop([c for c in result.columns if c.startswith("_")]).sort(["symbol", "pattern"])


def filter_pattern_history(history: pl.DataFrame, params: dict, pattern: Pattern) -> pl.DataFrame:
    """统一策略库的日线观察池; 向量化逐日对齐 T-1 特征, 非盘中成交信号。"""
    rules = FirstBoardRules.model_validate(params)
    df = _features(history, rules)
    if df.is_empty():
        return history.head(0)
    # 整段历史一次批量计算, 回测亦不得用 T 日完成K决定 T 日候选。
    df = df.with_columns([pl.col(col).shift(1).over("symbol").alias(col) for col in _FEATURE_COLUMNS])
    # 当前停牌/缺日后复牌不沿用旧日候选。
    df = df.with_columns((pl.col("_eligible") & (
        pl.col("_market_index") - pl.col("_market_index").shift(1).over("symbol") == 1
    ) & ~pl.col("name").str.contains("(?i)ST|退")).alias("_eligible"))
    if pattern not in rules.enabled_patterns:
        return history.head(0)
    result = _pattern_rows(df, rules, pattern)
    return result.drop([c for c in result.columns if c.startswith("_")])


def _finite(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def first_board_limit_pct(symbol: str, trade_date: date) -> float:
    """首板共享日期规则: 创业板改革前10%, 其余复用全局规则。"""
    if symbol.endswith(".SZ") and symbol.startswith(("300", "301")) and trade_date < GEM_REGISTRATION_DATE:
        return MAIN_BOARD_LIMIT
    return price_limit_pct(symbol, trade_date)


def first_board_limit_price(symbol: str, trade_date: date, previous: float | None, *,
                            explicit: float | None = None) -> float | None:
    """观察与研究共用原价涨停边界, 防止旧创业板20%缓存污染改革前结果。"""
    import numpy as np

    explicit = _finite(explicit)
    if explicit is not None and explicit >= 10000:
        return None
    legacy_gem = (symbol.endswith(".SZ") and symbol.startswith(("300", "301"))
                  and trade_date < GEM_REGISTRATION_DATE)
    if explicit is not None and explicit > 0 and not legacy_gem:
        return explicit
    previous = _finite(previous)
    if previous is None or previous <= 0:
        return None
    return float(numpy_limit_price(
        np.array([previous]), np.array([first_board_limit_pct(symbol, trade_date)]), up=True)[0])


def evaluate_candidates(
    candidates: pl.DataFrame, current: pl.DataFrame, rules: FirstBoardRules, *, as_of: date,
) -> list[dict]:
    """评估 enriched 当日快照。prev_close 为复权价; raw_prev_close 才是原价。

    日内高点只证明当日曾触板; broken 不证明事件顺序/回封可买。服务负责秒级
    时效、事件状态持久化与环境门控。sealed 永远不等价于买入或可成交。
    """
    if candidates.is_empty():
        return []
    universe = re.compile(_UNIVERSE_PATTERNS[rules.universe])
    if "date" in current.columns:
        current = current.with_columns(pl.col("date").cast(pl.Date, strict=False))
    quote_rows = current.to_dicts() if "symbol" in current.columns else []
    quotes = {str(row["symbol"]): row for row in quote_rows}
    duplicates = {symbol for symbol, count in Counter(str(row["symbol"]) for row in quote_rows).items()
                  if count > 1}
    result = []
    for candidate in candidates.to_dicts():
        symbol = str(candidate["symbol"])
        row = {key: candidate.get(key) for key in (
            "symbol", "name", "pattern", "pattern_label", "reference_price", "breakout_price")}
        evidence = {key: candidate.get(key) for key in (
            "platform_range", "trend_return", "oversold_drawdown", "first_board_count", "history_bars")}
        evidence["reference_date"] = str(candidate.get("reference_date") or "")
        evidence["experimental"] = True
        row.update(state="invalid", price=None, change_pct=None, distance_to_limit_pct=None,
                   turnover_rate=None, reasons=[], evidence=evidence)
        if not universe.fullmatch(symbol):
            row["reasons"] = ["股票不在当前首板范围内"]
            result.append(row)
            continue
        quote = quotes.get(symbol)
        if not quote or symbol in duplicates or quote.get("date") != as_of:
            row["reasons"] = ["缺少唯一的当日行情"]
            result.append(row)
            continue
        price, adjusted = _finite(quote.get("raw_close")), _finite(quote.get("close"))
        factor = adjusted / price if price and price > 0 and adjusted and adjusted > 0 else None
        reference = _finite(quote.get("raw_prev_close"))
        if reference is None and factor:
            previous = _finite(quote.get("prev_close"))
            reference = (previous if previous is not None else candidate["reference_adjusted_close"]) / factor
        turnover, amount = _finite(quote.get("turnover_rate")), _finite(quote.get("amount"))
        row.update(price=price, turnover_rate=turnover)
        if (not price or price <= 0 or not reference or reference <= 0 or factor is None
                or is_risk_warning_name(quote.get("name") or candidate.get("name"))):
            row["reasons"] = ["原价、复权尺度或昨收基准无效, 或当日为风险警示股票"]
            result.append(row)
            continue
        limit_price = first_board_limit_price(symbol, as_of, reference, explicit=quote.get("limit_up"))
        if limit_price is None:
            row["reasons"] = ["当日无涨跌幅限制, 首板规则不适用"]
            result.append(row)
            continue
        high = _finite(quote.get("raw_high"))
        change, distance = price / reference - 1, (limit_price - price) / limit_price
        evidence.update(limit_up_price=limit_price, raw_prev_close=reference,
                        breakout_price_raw=candidate["breakout_price"] / factor, amount=amount)
        row.update(change_pct=change, distance_to_limit_pct=distance)
        if (price > limit_price + 0.005
                or (high is not None and (high + 0.005 < price or high > limit_price + 0.005))):
            row["reasons"] = ["当日原价或日内最高价与涨停边界不一致"]
        elif turnover is not None and turnover > rules.max_turnover_rate:
            row["reasons"] = ["换手率超过实验上限"]
        elif price >= limit_price - 0.005:
            row.update(state="sealed", reasons=["当前价格触及涨停, 仅观察, 不能据此确认封单或成交"])
        elif high is not None and high >= limit_price - 0.005:
            row.update(state="broken", reasons=["日内曾触及涨停, 当前已打开; 回封顺序尚需事件验证"])
        else:
            reasons = []
            if adjusted <= candidate["breakout_price"]:
                reasons.append("尚未突破历史形态参考高点")
            if change < rules.min_change_pct:
                reasons.append("涨幅未达到实验门槛")
            if distance > rules.approaching_distance:
                reasons.append("距离涨停仍超过实验门槛")
            if turnover is None or turnover < rules.min_turnover_rate:
                reasons.append("换手率缺失或未达到实验门槛")
            if amount is None or amount < rules.min_amount:
                reasons.append("成交额缺失或未达到实验门槛")
            if high is None:
                reasons.append("缺少原价日内高点, 无法排除已炸板")
            row.update(state="watch" if reasons else "approaching", reasons=reasons or [
                "历史形态突破、流动性与临近涨停门槛通过; 需复核实时环境和成交条件"])
        result.append(row)
    return result


def pattern_strategy_meta(pattern: Pattern) -> dict:
    """三张策略卡与工作台共享参数默认值; 卡片明确只是日线候选。"""
    defaults = FirstBoardRules().model_dump()
    labels = {
        "lookback_days": "首板排除窗口(交易日)", "min_history_days": "最少历史交易日",
        "platform_window": "平台观察窗口", "platform_max_range": "平台最大振幅(比例)",
        "platform_near_high": "平台距高点上限(比例)", "trend_window": "趋势观察窗口",
        "trend_min_return": "趋势涨幅下限(比例)", "trend_max_return": "趋势涨幅上限(比例)",
        "oversold_window": "超跌观察窗口", "oversold_min_drawdown": "超跌最小回撤(比例)",
        "approaching_distance": "距涨停上限(比例)", "min_change_pct": "最低涨幅(比例)",
        "min_turnover_rate": "最低换手率(%)", "max_turnover_rate": "最高换手率(%)",
        "min_amount": "最低成交额(元)",
    }
    params = []
    for name, value in defaults.items():
        if isinstance(value, (int, float)):
            field = FirstBoardRules.model_fields[name]
            minimum = next((getattr(m, "ge", getattr(m, "gt", None)) for m in field.metadata
                            if hasattr(m, "ge") or hasattr(m, "gt")), None)
            maximum = next((getattr(m, "le", getattr(m, "lt", None)) for m in field.metadata
                            if hasattr(m, "le") or hasattr(m, "lt")), None)
            params.append({"id": name, "label": labels[name], "type": "int" if isinstance(value, int) else "float",
                           "default": value, "min": minimum, "max": maximum})
    return {"id": f"first_board_{pattern}", "name": f"首板·{PATTERN_LABELS[pattern]}观察池",
            "description": "沪深主板、创业板、科创板非ST的T-1日线首板候选; 参数为实验假设, 非盘中买入信号或可成交胜率",
            "tags": ["首板", "实验", "观察池"], "asset_types": ["stock"], "timeframes": ["1d"],
            "params": params, "scoring": {}, "order_by": "symbol", "descending": False, "limit": 1000}
