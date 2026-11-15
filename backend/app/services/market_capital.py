"""市场资金状态轴(总量 / 集中度 / 承接标记) — 纯函数模块。

与 regime_builder 的环境分(state)、market_phase 的情绪周期(phase)并存, 互不影响:
资金列只描述"成交是否活跃、是否集中", 不进入环境分、周期判定、回测过滤或仓位。

口径:
- 成交额单位为人民币元; amount_top5_share / amount_change_5d 为小数制;
  amount_multiple 无单位; 分位为 0–100。
- 有效成交额 = amount 非空且 > 0 (停牌零成交不计)。
- 窗口按输入行(实际交易日)计; 任一日只使用当日及之前的行, 分位参考窗口不含当日。
- 可比日: 两日有效股票数相差不超过 COUNT_TOLERANCE 才比较成交额。这是数量近似,
  不证明两日是同一批股票; 行情同步不完整的日子因此不会被判成"缩量"。
- 任一输入缺失则对应输出为 null, 不补零。

成交额只反映交易活跃度, 不等于资金流入或增量资金。
下方阈值是研究初始值, 未经校准; 调整只需改这里。
"""
from __future__ import annotations

import math
from statistics import median

import polars as pl

# ───────────────────────── 档位词汇 ─────────────────────────
VOLUME_EXPAND, VOLUME_FLAT, VOLUME_SHRINK = "expand", "flat", "shrink"
CONC_CONCENTRATED, CONC_NEUTRAL, CONC_DISPERSED = "concentrated", "neutral", "dispersed"
FLAG_VOLUME_STALL, FLAG_SHRINK_ADVANCE = "volume_stall", "shrink_advance"

# ───────────────────────── 阈值(研究初始值) ─────────────────────────
TOP_SHARE = 0.05            # 成交额最高的前 5% 个股
TOP_MIN_STOCKS = 100        # 有效股票少于此数不算集中度(小样本下前 5% 只有几只)
BASELINE_DAYS = 20          # 成交额倍数的基准窗口
BASELINE_MIN_COMPARABLE = 15
COUNT_TOLERANCE = 0.05      # 有效股票数相差不超过 5% 才可比
CHANGE_DAYS = 5
PCTL_WINDOW = 252           # 分位参考窗口(约一年), 应对成交额水位的结构性漂移
PCTL_MIN_SAMPLES = 60
PCTL_HIGH, PCTL_LOW = 75, 25

AGG_COLUMNS = ("amount_valid_count", "amount_top5_share")
DERIVED_COLUMNS = ("amount_multiple", "amount_change_5d", "amount_multiple_pctl",
                   "top5_share_pctl", "capital_volume", "capital_concentration", "capital_flag")
REQUIRED_COLUMNS = frozenset({"date", "total_amount", "median_pct", *AGG_COLUMNS})

_LABEL_COLUMNS = frozenset({"capital_volume", "capital_concentration", "capital_flag"})


# ───────────────────────── 日聚合 ─────────────────────────

def with_amount_rank(df: pl.DataFrame) -> pl.DataFrame:
    """追加 _amount_rank: 同一日内有效成交额的降序序数排名, 无效行为 null。"""
    if "_amount_rank" in df.columns:
        return df
    amount = pl.col("amount")
    valid = amount.is_not_null() & (amount > 0)
    return df.with_columns(
        pl.when(valid)
        .then(pl.when(valid).then(amount).rank("ordinal", descending=True).over("date"))
        .alias("_amount_rank")
    )


def capital_daily_aggs() -> list[pl.Expr]:
    """group_by("date").agg(...) 可直接拼接的聚合; 要求 df 含 amount 与 _amount_rank。"""
    amount, rank = pl.col("amount"), pl.col("_amount_rank")
    valid = rank.is_not_null()
    top_n = (valid.sum() * TOP_SHARE).ceil()
    return [
        valid.sum().alias("amount_valid_count"),
        amount.filter(valid).sum().alias("_amount_valid_sum"),
        amount.filter(valid & (rank <= top_n)).sum().alias("_amount_top5_sum"),
    ]


def finalize_capital_row(r: dict) -> dict:
    """把聚合行整理为持久化字段; 样本不足或无有效成交额时占比为 None。"""
    count = int(r.get("amount_valid_count") or 0)
    total = _number(r.get("_amount_valid_sum"))
    top = _number(r.get("_amount_top5_sum"))
    share = None
    if count >= TOP_MIN_STOCKS and total is not None and total > 0 and top is not None:
        share = round(top / total, 6)
    return {"amount_valid_count": count, "amount_top5_share": share}


# ───────────────────────── 序列标注 ─────────────────────────

def _number(value) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _percentiles(values: list[float | None]) -> list[float | None]:
    """每个值相对此前最多 PCTL_WINDOW 个非空值的中位秩分位(并列各算半个)。"""
    out: list[float | None] = []
    prior: list[float] = []
    for value in values:
        window = prior[-PCTL_WINDOW:]
        if value is None or len(window) < PCTL_MIN_SAMPLES:
            out.append(None)
        else:
            less = sum(v < value for v in window)
            equal = sum(v == value for v in window)
            out.append(round((less + 0.5 * equal) / len(window) * 100, 1))
        if value is not None:
            prior.append(value)
    return out


def _band(pctl: float | None, high: str, mid: str, low: str) -> str | None:
    if pctl is None:
        return None
    if pctl >= PCTL_HIGH:
        return high
    if pctl <= PCTL_LOW:
        return low
    return mid


def classify_capital_series(daily: pl.DataFrame) -> pl.DataFrame:
    """对完整日序标注资金状态, 追加(或覆盖) DERIVED_COLUMNS, 其余列原样保留。

    行数为交易日数(千级), 逐日循环开销可忽略; 与 classify_phase_series 一致。
    """
    missing = REQUIRED_COLUMNS - set(daily.columns)
    if missing:
        raise ValueError(f"classify_capital_series 缺少列: {sorted(missing)}")

    rows = daily.sort("date")
    n = rows.height
    totals = [_number(v) for v in rows["total_amount"].to_list()]
    counts = [_number(v) for v in rows["amount_valid_count"].to_list()]
    medians = [_number(v) for v in rows["median_pct"].to_list()]
    shares = [_number(v) for v in rows["amount_top5_share"].to_list()]

    def comparable(j: int, i: int) -> bool:
        ci, cj, tj = counts[i], counts[j], totals[j]
        if not ci or not cj or ci <= 0 or cj <= 0 or tj is None or tj <= 0:
            return False
        return abs(cj / ci - 1) <= COUNT_TOLERANCE

    multiples: list[float | None] = []
    changes: list[float | None] = []
    for i in range(n):
        multiple = change = None
        total = totals[i]
        if total is not None and total > 0:
            base = [totals[j] for j in range(max(0, i - BASELINE_DAYS), i) if comparable(j, i)]
            if len(base) >= BASELINE_MIN_COMPARABLE:
                multiple = round(total / median(base), 4)
            span = 2 * CHANGE_DAYS
            if i + 1 >= span and all(comparable(j, i) for j in range(i + 1 - span, i + 1)):
                earlier = sum(totals[i + 1 - span:i + 1 - CHANGE_DAYS])
                recent = sum(totals[i + 1 - CHANGE_DAYS:i + 1])
                change = round(recent / earlier - 1, 4)
        multiples.append(multiple)
        changes.append(change)

    multiple_pctl = _percentiles(multiples)
    share_pctl = _percentiles(shares)
    volume = [_band(p, VOLUME_EXPAND, VOLUME_FLAT, VOLUME_SHRINK) for p in multiple_pctl]
    concentration = [_band(p, CONC_CONCENTRATED, CONC_NEUTRAL, CONC_DISPERSED) for p in share_pctl]
    flags: list[str | None] = []
    for label, med in zip(volume, medians, strict=True):
        flag = None
        if med is not None:
            if label == VOLUME_EXPAND and med <= 0:
                flag = FLAG_VOLUME_STALL
            elif label == VOLUME_SHRINK and med > 0:
                flag = FLAG_SHRINK_ADVANCE
        flags.append(flag)

    derived = dict(zip(DERIVED_COLUMNS, (
        multiples, changes, multiple_pctl, share_pctl, volume, concentration, flags), strict=True))
    # 显式 dtype: 全空列也保持稳定类型, parquet 往返与 concat 不因 Null 类型出错。
    return rows.with_columns([
        pl.Series(name, values, dtype=pl.Utf8 if name in _LABEL_COLUMNS else pl.Float64)
        for name, values in derived.items()
    ])
