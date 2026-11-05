"""Evidence for a stock's unadjusted close, supplied by the dated daily-bar route."""

import polars as pl

FINALIZED = "daily_batch_finalized"
PENDING_REFRESH = "盘后日线核准和指标刷新待完成"
BAR_VALUES = ("open", "high", "low", "close", "volume", "amount")


def valid_official_bar(frame: pl.DataFrame) -> pl.Expr:
    if not set(BAR_VALUES).issubset(frame.columns):
        return pl.lit(False)
    valid = (
        pl.all_horizontal(pl.col(c).is_finite() & (pl.col(c) > 0) for c in BAR_VALUES)
        & (pl.col("high") >= pl.max_horizontal("open", "low", "close"))
        & (pl.col("low") <= pl.min_horizontal("open", "high", "close"))
    )
    if "quote_ts" in frame.columns:
        valid &= pl.col("quote_ts").is_null()
    return valid.fill_null(False)
