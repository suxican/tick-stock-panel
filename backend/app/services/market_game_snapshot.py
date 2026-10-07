"""Bounded, read-only post-close inputs for the game analysis rules.

Returns and proportions are fractions; money is CNY. ``ref_close`` and moving
averages use the target day's unadjusted price scale. Historical metadata is not
point-in-time certified and must never be used to claim a tradable backtest.
"""
from __future__ import annotations

import hashlib
import json
import math
from datetime import date, datetime, time, timedelta

import polars as pl

from app.indicators.pipeline import compute_indicators, compute_limit_signals
from app.market_time import CN_TZ, cn_now
from app.price_limits import MAIN_BOARD_ST_LIMIT_CHANGE_DATE, parse_listing_date
from app.services.index_const import CORE_INDEX_SYMBOLS
from app.services.market_game_calendar import load_game_calendar

_WINDOW = 280
_PRICE_COLUMNS = ("open", "high", "low", "close", "raw_close", "raw_high", "raw_low")
_BASE_COLUMNS = ("symbol", "date", *_PRICE_COLUMNS, "volume", "amount")
_METRICS = {
    "breadth_up": ("上涨占比", "ratio"), "above_ma20": ("站上20日线占比", "ratio"),
    "median_return": ("个股涨幅中位数", "ratio"), "limit_up": ("涨停家数", "count"),
    "limit_down": ("跌停家数", "count"), "broken_limit": ("日线触板未封家数", "count"),
    "seal_rate": ("日线封板率", "ratio"), "max_boards": ("连续涨停高度", "count"),
    "promotion_rate": ("昨日涨停晋级率", "ratio"),
    "previous_limit_premium": ("昨日涨停等权涨幅", "ratio"),
    "amount": ("样本成交额", "yuan"), "amount_ratio": ("成交额较前5日均值", "number"),
    "index_return": ("核心指数平均日涨幅", "ratio"),
    "index_ret5": ("核心指数平均5日涨幅", "ratio"),
    "index_ret20": ("核心指数平均20日涨幅", "ratio"),
    "index_above_ma20": ("核心指数站上20日线占比", "ratio"),
    "stock_count": ("有效股票样本数", "count"), "coverage": ("有效行情审计覆盖率", "ratio"),
    "limit_up_ratio": ("样本涨停占比", "ratio"), "limit_down_ratio": ("样本跌停占比", "ratio"),
}


def _clean(value):
    if isinstance(value, dict):
        return {key: _clean(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_clean(item) for item in value]
    if isinstance(value, float):
        return round(value, 10) if math.isfinite(value) else None
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def _columns(repo, table: str) -> set[str]:
    # All table identifiers are module-owned constants, never user input.
    try:
        return {row[0] for row in repo.execute_all(f"DESCRIBE {table}")}
    except Exception:
        return set()


def _read(repo, table: str, columns: list[str], start: date, end: date) -> pl.DataFrame:
    available = _columns(repo, table)
    selected = [name for name in columns if name in available]
    if not {"date", "symbol"} <= set(selected):
        return pl.DataFrame()
    rows = repo.execute_all(
        f"SELECT {','.join(selected)} FROM {table} WHERE date >= ? AND date <= ? ORDER BY symbol,date",
        [start, end],
    )
    schema = {name: pl.String if name == "symbol" else pl.Date if name == "date" else pl.Float64 for name in selected}
    return pl.DataFrame(rows, schema=schema, orient="row", strict=False)


def _generation(repo) -> tuple[str, str | None]:
    # The public repository getter initializes/recover-writes the marker. This
    # consumer must not repair user data; only inspect the established marker.
    try:
        payload = json.loads((repo.store.data_dir / ".matrix_generation_stock.json").read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("state", "ready") != "ready":
            return "unavailable", None
        token = payload.get("generation")
        return ("ready", token) if isinstance(token, str) and token else ("unavailable", None)
    except FileNotFoundError:
        return "missing", None
    except (OSError, ValueError, TypeError):
        return "unavailable", None


def _align_raw(frame: pl.DataFrame, raw: pl.DataFrame) -> pl.DataFrame:
    """Require one matching raw OHLC record; a missing join is never a batch null."""
    required = {"symbol", "date", "close", "high", "low"}
    if not required <= set(raw.columns) or not {f"raw_{name}" for name in ("close", "high", "low")} <= set(frame.columns):
        return frame.head(0)
    raw = raw.filter(~pl.struct("symbol", "date").is_duplicated())
    if "quote_ts" not in raw.columns:
        raw = raw.with_columns(pl.lit(None, dtype=pl.Float64).alias("quote_ts"))
    raw = raw.select("symbol", "date", "close", "high", "low", "quote_ts").rename({
        name: f"_source_{name}" for name in ("close", "high", "low", "quote_ts")
    })
    joined = frame.join(raw, on=["symbol", "date"], how="inner")
    match = pl.all_horizontal([
        pl.col(f"_source_{name}").is_finite() & (pl.col(f"_source_{name}") > 0)
        & ((pl.col(f"_source_{name}") - pl.col(f"raw_{name}")).abs()
           <= pl.max_horizontal(pl.col(f"raw_{name}").abs() * 1e-8, pl.lit(1e-6)))
        for name in ("close", "high", "low")
    ])
    return joined.filter(match.fill_null(False))


def _current_sectors(repo) -> dict[str, list[str]]:
    """Current observation only; callers must never use this for a dated replay."""
    from app.services.ext_data import ExtConfigStore
    from app.services.market_overview_builder import (
        _dimension_field,
        _dimension_values,
        _read_ext_rows,
        _symbol_keys,
    )

    members: dict[str, set[str]] = {}
    base = repo.store.data_dir / "ext_data"
    # load_all migrates legacy files on an empty store; analysis must not.
    if not base.exists() or not any(base.iterdir()):
        return {}
    # Reuse the standard dimension reader, avoiding the global mapping cache
    # whose key does not include this repository's data directory.
    for config in ExtConfigStore(repo.store.data_dir).load_all():
        if config.market_level:
            continue
        field = _dimension_field(config, "concept") or _dimension_field(config, "industry")
        if not field:
            continue
        if config.mode == "timeseries":
            # The shared display reader scans all history. Select one latest
            # observation partition here, while reusing its field/code mapping.
            root = base / config.id / "timeseries"
            partitions = sorted(
                path for path in root.glob("date=*")
                if parse_listing_date(path.name[5:]) is not None
                and parse_listing_date(path.name[5:]) <= cn_now().date()
                and (path / "part.parquet").is_file()
            )
            if not partitions:
                continue
            rows = pl.read_parquet(partitions[-1] / "part.parquet").to_dicts()
        else:
            rows = _read_ext_rows(repo.store.data_dir, config, field)
        for row in rows:
            values = _dimension_values(row.get(field))
            for symbol in _symbol_keys(row, config):
                members.setdefault(symbol, set()).update(values)
    return {symbol: sorted(values) for symbol, values in members.items()}


def _instruments(repo) -> pl.DataFrame:
    try:
        original = repo.get_instruments()
        rows = original.to_dicts() if original is not None else []
    except Exception:
        rows = []
    data = []
    for row in rows:
        if not row.get("symbol") or row.get("asset_type") not in (None, "stock"):
            continue
        data.append({
            "symbol": row["symbol"], "name": row.get("name"),
            "listing_date": parse_listing_date(row.get("listing_date")),
            "metadata_date": parse_listing_date(row.get("as_of")),
            "status": str(row.get("status") or ""),
        })
    return pl.DataFrame(data, schema={
        "symbol": pl.String, "name": pl.String, "listing_date": pl.Date,
        "metadata_date": pl.Date, "status": pl.String,
    }).unique("symbol", keep="last")


def _prepare(frame: pl.DataFrame, days: list[date], instruments: pl.DataFrame, cutoff: datetime) -> pl.DataFrame:
    if not set(_BASE_COLUMNS) <= set(frame.columns):
        return pl.DataFrame()
    valid = pl.all_horizontal([
        pl.col(column).is_not_null() & pl.col(column).is_finite() & (pl.col(column) > 0)
        for column in (*_PRICE_COLUMNS, "volume", "amount")
    ])
    valid &= (pl.col("high") >= pl.max_horizontal("open", "close", "low"))
    valid &= (pl.col("low") <= pl.min_horizontal("open", "close", "high"))
    valid &= (pl.col("raw_high") >= pl.col("raw_close")) & (pl.col("raw_low") <= pl.col("raw_close"))
    valid &= ~pl.struct("symbol", "date").is_duplicated()
    for column in ("quote_ts", "_source_quote_ts"):
        if column not in frame.columns:
            continue
        stamp = pl.from_epoch(pl.col(column), time_unit="ms").dt.replace_time_zone("UTC").dt.convert_time_zone("Asia/Shanghai")
        valid &= pl.col(column).is_null() | (
            (stamp.dt.date() == pl.col("date")) & (stamp.dt.hour() >= 15)
            & (pl.col(column) <= cutoff.timestamp() * 1000)
        )
    frame = frame.filter(valid.fill_null(False)).sort("symbol", "date")
    if frame.is_empty():
        return frame
    calendar = pl.DataFrame({"date": days, "_day": range(len(days))})
    frame = frame.join(calendar, on="date", how="inner").with_columns(
        (pl.col("_day").diff().over("symbol") != 1).fill_null(True).cast(pl.Int64).alias("_gap")
    ).with_columns(pl.col("_gap").cum_sum().over("symbol").alias("_segment"))
    frame = frame.with_columns(
        pl.col("symbol").alias("_real_symbol"),
        pl.concat_str("symbol", pl.lit("@"), pl.col("_segment")).alias("symbol"),
    )
    # Neutralize later uniform adjustment rescaling before computing indicators.
    scale = pl.col("raw_close").last().over("symbol") / pl.col("close").last().over("symbol")
    frame = frame.with_columns([(pl.col(c) * scale).round(10).alias(c) for c in ("open", "high", "low", "close")])
    frame = compute_indicators(frame, needed={"ma5", "ma20", "atr_14", "momentum_5d", "momentum_20d", "change_pct"})
    frame = frame.with_columns(
        pl.col("date").cum_count().over("symbol").alias("history_days"),
        (pl.col("amount") / pl.col("amount").shift(1).rolling_mean(5).over("symbol")).alias("amount_ratio"),
        (1 - pl.col("close") / pl.col("high").rolling_max(5).over("symbol")).alias("drawdown5"),
        pl.col("symbol").alias("_segment_key"),
    ).with_columns(pl.col("_real_symbol").alias("symbol"))
    # The existing limit helper supplies date-aware exchange rounding and
    # corporate-action reference prices. Unknown metadata is gated below.
    frame = compute_limit_signals(frame, instruments.select("symbol", "name", "listing_date"), needed={
        "signal_limit_up", "signal_limit_down", "signal_broken_limit_up", "consecutive_limit_ups",
    })
    frame = frame.join(instruments, on="symbol", how="left")
    known_limit = (
        (pl.col("history_days") > 1) & pl.col("name").is_not_null()
        & pl.col("listing_date").is_not_null()
        & ((pl.col("date") - pl.col("listing_date")).dt.total_days() >= 30)
        & ((pl.col("date") >= MAIN_BOARD_ST_LIMIT_CHANGE_DATE)
           | (pl.col("metadata_date") <= pl.col("date")))
    ).fill_null(False)
    frame = frame.with_columns([
        pl.when(known_limit).then(pl.col(c)).otherwise(None).alias(c)
        for c in ("signal_limit_up", "signal_limit_down", "signal_broken_limit_up")
    ])
    frame = frame.with_columns(
        (~pl.col("signal_limit_up").fill_null(False)).cast(pl.UInt32).cum_sum().over("_segment_key").alias("_run")
    ).with_columns(
        pl.col("signal_limit_up").fill_null(False).cast(pl.UInt32).cum_sum().over("_segment_key", "_run").alias("boards"),
        pl.col("signal_limit_up").shift(1).over("_segment_key").alias("_previous_up"),
    )
    return frame


def _indices(repo, days: list[date], cutoff: datetime) -> dict[date, dict]:
    start, end = days[0], days[-1]
    frame = _read(repo, "kline_index_daily", ["symbol", "date", "close", "quote_ts"], start, end)
    if frame.is_empty() or "close" not in frame.columns:
        return {}
    valid = (pl.col("symbol").is_in(CORE_INDEX_SYMBOLS) & pl.col("close").is_finite()
             & (pl.col("close") > 0) & ~pl.struct("symbol", "date").is_duplicated())
    if "quote_ts" in frame.columns:
        stamp = pl.from_epoch(pl.col("quote_ts"), time_unit="ms").dt.replace_time_zone("UTC").dt.convert_time_zone("Asia/Shanghai")
        valid &= pl.col("quote_ts").is_null() | (
            (stamp.dt.date() == pl.col("date")) & (stamp.dt.hour() >= 15)
            & (pl.col("quote_ts") <= cutoff.timestamp() * 1000)
        )
    frame = frame.filter(valid.fill_null(False)).with_columns(
        (pl.col("quote_ts").is_not_null() if "quote_ts" in frame.columns else pl.lit(False)).alias("_completion_known")
    ).join(
        pl.DataFrame({"date": days, "_day": range(len(days))}), on="date", how="inner",
    ).sort("symbol", "date").with_columns(
        (pl.col("_day").diff().over("symbol") != 1).fill_null(True).cast(pl.Int64).alias("_gap")
    ).with_columns(pl.col("_gap").cum_sum().over("symbol").alias("_segment"))
    frame = frame.with_columns(
        (pl.col("close") / pl.col("close").shift(1).over("symbol", "_segment") - 1).alias("index_return"),
        (pl.col("close") / pl.col("close").shift(5).over("symbol", "_segment") - 1).alias("index_ret5"),
        (pl.col("close") / pl.col("close").shift(20).over("symbol", "_segment") - 1).alias("index_ret20"),
        (pl.col("close") > pl.col("close").rolling_mean(20).over("symbol", "_segment")).cast(pl.Float64).alias("index_above_ma20"),
    ).group_by("date").agg(
        *[pl.col(c).mean() for c in ("index_return", "index_ret5", "index_ret20", "index_above_ma20")],
        pl.col("symbol").n_unique().alias("_index_count"),
        pl.col("_completion_known").sum().alias("_completion_known"),
    )
    return {row.pop("date"): row for row in frame.to_dicts()}


def _newer_index_close(repo, target: date, through: date, cutoff: datetime) -> bool:
    if through <= target:
        return False
    frame = _read(repo, "kline_index_daily", ["symbol", "date", "close", "quote_ts"], target + timedelta(days=1), through)
    if frame.is_empty() or "close" not in frame.columns:
        return False
    valid = pl.col("symbol").is_in(CORE_INDEX_SYMBOLS) & pl.col("close").is_finite() & (pl.col("close") > 0)
    if "quote_ts" in frame.columns:
        stamp = pl.from_epoch(pl.col("quote_ts"), time_unit="ms").dt.replace_time_zone("UTC").dt.convert_time_zone("Asia/Shanghai")
        valid &= pl.col("quote_ts").is_null() | (
            (stamp.dt.date() == pl.col("date")) & (stamp.dt.hour() >= 15)
            & (pl.col("quote_ts") <= cutoff.timestamp() * 1000)
        )
    return not frame.filter(valid.fill_null(False)).is_empty()


def _aggregate(frame: pl.DataFrame, indices: dict, days: list[date], denominators: dict[date, int]) -> list[dict]:
    grouped = frame.group_by("date").agg(
        pl.col("change_pct").filter(pl.col("change_pct").is_not_null()).gt(0).mean().alias("breadth_up"),
        (pl.col("close") > pl.col("ma20")).mean().alias("above_ma20"),
        pl.col("change_pct").median().alias("median_return"), pl.col("amount").sum().alias("amount"),
        pl.col("signal_limit_up").count().alias("_limit_known"), pl.len().alias("_count"),
        pl.col("signal_limit_up").sum().alias("limit_up"), pl.col("signal_limit_down").sum().alias("limit_down"),
        pl.col("signal_broken_limit_up").sum().alias("broken_limit"), pl.col("boards").max().alias("max_boards"),
        pl.col("_previous_up").sum().alias("_pool"),
        (pl.col("_previous_up") & pl.col("signal_limit_up")).sum().alias("_promoted"),
        pl.col("change_pct").filter(pl.col("_previous_up")).mean().alias("previous_limit_premium"),
    ) if not frame.is_empty() else pl.DataFrame({"date": pl.Series([], dtype=pl.Date)})
    calendar = pl.DataFrame({"date": days})
    if grouped.width == 1:
        return [{"date": day.isoformat(), **{key: None for key in _METRICS},
                 "stock_count": 0, "coverage": 0.0 if denominators.get(day) else None} for day in days]
    grouped = calendar.join(grouped, on="date", how="left").sort("date").with_columns(
        (pl.col("amount") / pl.col("amount").shift(1).rolling_mean(5)).alias("amount_ratio")
    )
    rows = []
    for row in grouped.to_dicts():
        item = {key: row.get(key) for key in _METRICS}
        count = row["_count"] or 0
        denominator = denominators.get(row["date"], 0)
        item.update({"stock_count": count, "coverage": count / denominator if denominator else None})
        if not count or row["_limit_known"] < count * 0.95:
            for key in ("limit_up", "limit_down", "broken_limit", "max_boards", "previous_limit_premium"):
                item[key] = None
        else:
            item["limit_up_ratio"] = row["limit_up"] / count
            item["limit_down_ratio"] = row["limit_down"] / count
            total = row["limit_up"] + row["broken_limit"]
            item["seal_rate"] = row["limit_up"] / total if total else None
            item["promotion_rate"] = row["_promoted"] / row["_pool"] if row["_pool"] >= 10 else None
        item.update({key: value for key, value in indices.get(row["date"], {}).items() if not key.startswith("_")})
        rows.append({"date": row["date"].isoformat(), **item})
    return rows


def build_snapshot(repo, as_of: date | None = None) -> dict:
    """Read at most 280 sessions, with no network, refresh, or writes.

    Explicit dates are research replays at 15:00 Beijing and never fall back.
    Latest mode observes today's metadata now, while retaining the actual price
    date. It is not represented as knowledge available on that price date.
    """
    now = cn_now()
    generation_before = _generation(repo)
    if as_of is not None and (type(as_of) is not date or as_of > now.date()):
        raise ValueError("日期格式无效或不能分析未来日期")
    if as_of == now.date() and now.time() < time(15, 10):
        raise ValueError("当日尚未完成收盘数据确认, 请在15:10后生成")
    through = as_of or (now.date() if now.time() >= time(15, 10) else now.date() - timedelta(days=1))
    try:
        raw_days = repo.execute_all(
            f"SELECT DISTINCT date FROM kline_enriched WHERE date <= ? ORDER BY date DESC LIMIT {_WINDOW}", [through],
        )
    except Exception as exc:
        raise ValueError("暂无可用日线数据, 请先完成盘后同步") from exc
    days = sorted({parse_listing_date(row[0]) for row in raw_days} - {None})
    if not days:
        raise ValueError("暂无可用日线数据, 请先完成盘后同步")
    target = as_of or days[-1]
    if target not in days:
        raise ValueError("指定日期没有日线数据, 不使用其他日期替代")
    cutoff = datetime.combine(target, time(15), tzinfo=CN_TZ) if as_of else now
    calendar = load_game_calendar(repo.store.data_dir, start=days[0], cutoff=cutoff, through=through)
    observed_days = days
    if calendar["verified"]:
        expected = [date.fromisoformat(day) for day in calendar["sessions"] if day <= target.isoformat()]
        days = expected[-_WINDOW:]
    # Without independent coverage, retain observed-date research only; candidate
    # eligibility below fails closed. Never substitute a weekday approximation.
    if not days:
        days = observed_days[-_WINDOW:]
    frame = _read(repo, "kline_enriched", [*_BASE_COLUMNS, "quote_ts"], days[0], target)
    raw = _read(repo, "kline_daily", ["symbol", "date", "close", "high", "low", "quote_ts"], days[0], target)
    aligned = _align_raw(frame, raw)
    mismatched_rows = frame.height - aligned.height
    instruments = _instruments(repo)
    prepared = _prepare(aligned, days, instruments, cutoff)
    limitations = [
        "规则尚未经样本外验证; 输出为条件化研究计划。",
        "本地历史日线缺少首次公开时间, available_at未知; 因子完整性与历史交易资格未获独立验证。",
        "覆盖率分母采用当前股票维表按上市日期过滤, 是数据审计口径; 历史存续股票全集与停牌历史未获PIT验证。",
        "股票复权数据读取前后核对发布代次; 原始行情、指数与因子尚无跨表原子发布证明。",
        "涨跌停基于日线原始价和统一限价规则; 不含盘口封单、真实成交排队和完整盘中炸板过程。",
        *calendar["limitations"],
    ]
    if as_of:
        limitations.append("历史回看不使用当前板块成分, 不生成当时可执行个股; 历史ST状态未核验时对应限价统计缺失。")
        sectors = {}
    else:
        limitations.append("板块及股票名称采用本次观察时的当前快照, 不代表行情日期当时已知; 板块历史强度未验证。")
        try:
            sectors = _current_sectors(repo)
        except Exception:
            sectors = {}
            limitations.append("当前板块映射不可用, 不生成缺少板块证据的候选。")
    indices = _indices(repo, days, cutoff)
    observed_counts = dict(frame.group_by("date").agg(pl.col("symbol").n_unique()).iter_rows()) if {"date", "symbol"} <= set(frame.columns) else {}
    denominators = {
        day: max(instruments.filter(pl.col("listing_date").is_null() | (pl.col("listing_date") <= day)).height, observed_counts.get(day, 0))
        for day in days
    }
    history = _aggregate(prepared, indices, days, denominators)
    current = prepared.filter(pl.col("date") == target) if not prepared.is_empty() else pl.DataFrame()
    denominator = denominators.get(target, 0)
    coverage = current.height / denominator if denominator else None
    valid_dates = set(prepared["date"].to_list()) if not prepared.is_empty() else set()
    calendar["missing_sessions"] = [day.isoformat() for day in days if day not in valid_dates] if calendar["verified"] else []
    calendar["unexpected_sessions"] = [day.isoformat() for day in observed_days if day >= days[0] and day not in set(days)] if calendar["verified"] else []
    stale = bool(not as_of and calendar["verified"] and calendar["expected_latest_session"]
                 and target.isoformat() < calendar["expected_latest_session"])
    behind_index = not as_of and _newer_index_close(repo, target, through, cutoff)
    generation_after = _generation(repo)
    generation_verified = generation_before[0] == "ready" and generation_before == generation_after
    generation_unstable = generation_before != generation_after or "unavailable" in {generation_before[0], generation_after[0]}
    quality_reasons = []
    if mismatched_rows:
        quality_reasons.append("raw_enriched_mismatch")
        limitations.append(f"{mismatched_rows}条复权行情缺少唯一匹配的原始收高低价, 已排除。")
    if calendar["missing_sessions"]:
        quality_reasons.append("market_sessions_missing")
        limitations.append("历史市场交易日有行情缺口; 已保留空行并中断连续窗口, 不将缺口压缩为连续交易日。")
    if calendar["unexpected_sessions"]:
        quality_reasons.append("unexpected_market_sessions")
        limitations.append("本地部分行情日期不在独立交易日历内, 已排除对应记录。")
    if not calendar["verified"] or not calendar["future_verified"]:
        quality_reasons.append("calendar_unverified")
    if not generation_verified:
        quality_reasons.append("generation_changed" if generation_unstable else "generation_unverified")
        limitations.append("复权行情发布代次缺失、正在发布或读取期间变化; 本次不生成候选。")
    completion_unknown = (not current.is_empty() and ("_source_quote_ts" not in current.columns or current["_source_quote_ts"].null_count() > 0))
    completion_unknown |= indices.get(target, {}).get("_completion_known", 0) < indices.get(target, {}).get("_index_count", 0)
    if completion_unknown:
        quality_reasons.append("completion_unverified")
        limitations.append("部分日线没有收盘时间证据; 可能是正常批次, 但缺少独立来源与完成批次标记, 保留研究画像并降级。")
    if current.is_empty():
        quality_reasons.append("current_data_missing")
    if coverage is None or coverage < 0.95:
        quality_reasons.append("coverage_insufficient")
    if len(days) < 25:
        quality_reasons.append("history_insufficient")
    if indices.get(target, {}).get("_index_count", 0) < len(CORE_INDEX_SYMBOLS):
        quality_reasons.append("index_coverage_insufficient")
    if behind_index:
        quality_reasons.append("stocks_behind_index")
    quality = "ready"
    if current.is_empty() or stale or behind_index or generation_unstable or (coverage is not None and coverage < 0.5):
        quality = "unavailable"
    elif (quality_reasons or len(days) < 25 or coverage is None or coverage < 0.95
          or indices.get(target, {}).get("_index_count", 0) < len(CORE_INDEX_SYMBOLS)):
        quality = "limited"
    if stale:
        quality_reasons.append("latest_session_missing")
        limitations.append("股票行情未覆盖独立日历要求的最新完整交易日, 即使指数同样滞后也不生成当前候选。")
    if behind_index:
        limitations.append("核心指数已有更晚收盘记录, 股票日线未同步到最新交易日, 暂不生成当前候选。")
    if coverage is not None and coverage < 0.95:
        limitations.append("目标日有效行情覆盖不足, 已剔除重复、异常、停牌或非收盘记录。")
    if not indices:
        limitations.append("核心指数日线不可用, 指数趋势指标缺失。")
    elif indices.get(target, {}).get("_index_count", 0) < len(CORE_INDEX_SYMBOLS):
        limitations.append("核心指数覆盖不足四只, 指数指标仅代表有数据的样本。")
    stocks = []
    for row in current.sort("symbol").to_dicts() if not current.is_empty() else []:
        symbol = row["symbol"]
        names = sectors.get(symbol) or sectors.get(symbol.split(".")[0]) or []
        scale = row["raw_close"] / row["close"]
        span = row["raw_high"] - row["raw_low"]
        is_st = "ST" in str(row.get("name") or "").upper()
        listing_days = (target - row["listing_date"]).days + 1 if row.get("listing_date") else None
        reasons = []
        if as_of:
            reasons.append("历史交易资格未核验")
        if quality == "unavailable":
            reasons.append("市场数据不可用或过期")
        if not calendar["verified"] or not calendar["future_verified"]:
            reasons.append("独立日历及未来观察窗口未核验")
        if not generation_verified:
            reasons.append("复权数据发布代次未核验")
        if not names:
            reasons.append("缺少当前板块映射")
        if not row.get("name") or listing_days is None or listing_days < 60:
            reasons.append("名称或上市历史不足")
        if is_st or "退" in str(row.get("name") or ""):
            reasons.append("风险警示或退市标记")
        if row["history_days"] < 21:
            reasons.append("连续有效历史少于21个观测交易日")
        if row.get("signal_limit_up") is None or row.get("signal_limit_down") is None:
            reasons.append("涨跌停状态无法确认")
        if span <= 0:
            reasons.append("一字价格无法确认可成交性")
        if row.get("status", "").casefold() in {"delisted", "suspended", "inactive"}:
            reasons.append("交易状态不允许")
        stocks.append({
            "symbol": symbol, "name": row.get("name") or symbol, "ref_close": row["raw_close"],
            "raw_low": row["raw_low"], "ma5": row["ma5"] * scale if row.get("ma5") else None,
            "ma20": row["ma20"] * scale if row.get("ma20") else None,
            "ret5": row.get("momentum_5d"), "ret20": row.get("momentum_20d"),
            "change_pct": row.get("change_pct"), "amount": row["amount"], "amount_ratio": row.get("amount_ratio"),
            "atr_pct": row["atr_14"] / row["close"] if row.get("atr_14") is not None else None,
            "close_location": (row["raw_close"] - row["raw_low"]) / span if span > 0 else None,
            "drawdown5": row.get("drawdown5"), "limit_up": row.get("signal_limit_up"),
            "limit_down": row.get("signal_limit_down"), "one_price": span <= 0,
            "listing_days": listing_days, "history_days": row["history_days"], "is_st": is_st,
            "sector": names[0] if names else None, "sectors": names,
            "eligible": not reasons, "exclusion_reasons": reasons,
        })
    metrics = next((row for row in history if row["date"] == target.isoformat()), {})
    previous = next((row for row in reversed(history) if row["date"] < target.isoformat()), {})
    result = _clean({
        "as_of": target, "cutoff": cutoff, "quality": quality, "research_only": True,
        "quality_reasons": quality_reasons, "calendar": calendar,
        "data_generation": {"before": generation_before[1], "after": generation_after[1], "verified": generation_verified},
        "metadata_scope": "historical_unverified" if as_of else "current_observation",
        "coverage": coverage, "history_days": len(days),
        "metrics": {key: metrics.get(key) for key in _METRICS},
        "previous": {key: previous.get(key) for key in _METRICS}, "history": history,
        "stocks": stocks, "limitations": limitations,
        "evidence": [{
            "id": key, "label": label, "value": metrics.get(key), "unit": unit,
            "source": "本地核心指数日线" if key.startswith("index_") else "本地标准化日线",
            "observed_at": datetime.combine(target, time(15), tzinfo=CN_TZ).isoformat(),
            "available_at": None,
        } for key, (label, unit) in _METRICS.items()] + ([{
            "id": "metadata", "label": "当前板块与名称快照", "value": None,
            "unit": "", "source": "当前股票维表与扩展维度快照; 仅供本次观察",
            "observed_at": cutoff.isoformat(), "available_at": cutoff.isoformat(),
        }, {
            "id": "current_sector_memberships", "label": "当前板块映射覆盖股票数",
            "value": sum(bool(stock["sectors"]) for stock in stocks), "unit": "count",
            "source": "当前扩展维度快照; 仅供本次观察", "observed_at": cutoff.isoformat(),
            "available_at": cutoff.isoformat(),
        }] if not as_of else []),
    })
    result["input_version"] = hashlib.sha256(json.dumps(result, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()[:24]
    return result
