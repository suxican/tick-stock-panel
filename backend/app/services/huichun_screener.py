"""Read-only adapter from the original A0 audit to strategy candidate rows."""

from collections import OrderedDict
from contextlib import suppress
from dataclasses import fields
from datetime import date, timedelta
from threading import Lock

import polars as pl

from app.market_time import cn_now
from app.services.huichun_audit import FACTOR_SCHEMA, _table_exists
from app.services.huichun_data import HuichunDataReader, screening_stock
from app.services.huichun_mode import HuichunRules
from app.strategy.huichun_a0 import A0Params, audit_a0

_CACHE_LIMIT = 8
_CACHE: OrderedDict[tuple, pl.DataFrame] = OrderedDict()
_CACHE_LOCK = Lock()
_KEY_SCHEMA = {"symbol": pl.String, "date": pl.Date}


def _clear_cache() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()


def _listing_dates(repo) -> dict[str, date]:
    if not _table_exists(repo, "instruments"):
        return {}
    available = {row[0] for row in repo.execute_all("DESCRIBE instruments")}
    if "listing_date" not in available:
        return {}
    result = {}
    for symbol, listing in repo.execute_all("SELECT symbol,listing_date FROM instruments"):
        with suppress(ValueError, TypeError):
            result[symbol] = date.fromisoformat(str(listing)[:10])
    return result


def _refresh_inputs(repo, signature: list) -> set[str]:
    """The strategy's lazy repository may predate a newly synchronized table."""
    present = {item[0].split("/", 1)[0] for item in signature}
    for table in ("kline_daily", "adj_factor", "instruments"):
        if table in present:
            glob = (repo.store.data_dir / table / "**" / "*.parquet").as_posix()
            if not repo.refresh_view(table, glob.replace("'", "''")):
                raise ValueError("回春数据读取视图更新失败，请检查日线或复权数据后重试")
    return present


def filter_huichun_a0(repo, frame: pl.DataFrame, params: dict) -> pl.DataFrame:
    """Keep A0 shape matches at the input symbol/date keys, preserving input columns.

    The input's prices/indicators are never used to recompute A0: the dedicated
    mode's complete raw history, causal factors and gap segmentation are reused.
    Unclosed/future dates are excluded. Like the dedicated mode's candidates,
    these shape matches do not certify official daily-batch publication,
    historical eligibility or factor completeness.
    """
    if frame.is_empty():
        return frame.head(0)
    if not {"symbol", "date"}.issubset(frame.columns):
        raise ValueError("回春 A0 策略需要股票代码和日线日期")
    rules = HuichunRules(**{f.name: params[f.name] for f in fields(A0Params) if f.name in params})
    a0_params = A0Params(**rules.model_dump())
    now = cn_now()
    cutoff = now.date() if now.hour >= 15 else now.date() - timedelta(days=1)
    selected = frame.with_columns(pl.col("date").cast(pl.Date, strict=True)).filter(
        pl.col("date") <= cutoff,
    )
    symbols = sorted(s for s in selected["symbol"].drop_nulls().unique() if screening_stock(s))
    if not symbols:
        return frame.head(0)
    selected = selected.filter(pl.col("symbol").is_in(symbols))
    end = selected["date"].max()
    reader = HuichunDataReader(repo)
    before = reader.input_signature(end)
    key = (
        str(repo.store.data_dir), tuple(symbols), end, a0_params,
        tuple(tuple(item) for item in before),
    )
    with _CACHE_LOCK:
        hits = _CACHE.get(key)
        if hits is not None:
            _CACHE.move_to_end(key)
    if hits is None:
        present = _refresh_inputs(repo, before)
        # A removed optional dataset can leave an old DuckDB view behind. Never
        # query that view or reuse its previous rows when no backing files remain.
        calendar, source = reader.calendar(end) if "kline_daily" in present else ([], "")
        matches = []
        listings = _listing_dates(repo) if "instruments" in present else {}
        for offset in range(0, len(symbols), 128) if calendar else ():
            batch = symbols[offset:offset + 128]
            raw = reader.raw(batch, end, start=calendar[0])
            factors = (reader.factors(batch, end) if "adj_factor" in present
                       else pl.DataFrame(schema=FACTOR_SCHEMA))
            prepared, _issues = reader.prepare_batch(
                raw, factors, calendar=calendar, end=end, listings=listings,
                expected_symbols=batch, calendar_verified=source == "official_exchange_calendar",
            )
            if prepared.frame.is_empty():
                continue
            audit = audit_a0(
                prepared.frame, start=prepared.frame["date"].min(), end=end, params=a0_params,
            )
            matches.append(audit.signals.select("symbol", pl.col("r_date").alias("date")))
        hits = pl.concat(matches).unique() if matches else pl.DataFrame(schema=_KEY_SCHEMA)
        if reader.input_signature(end) != before:
            raise ValueError("回春日线或复权数据在筛选期间发生变化，请重新运行")
        with _CACHE_LOCK:
            _CACHE[key] = hits
            _CACHE.move_to_end(key)
            while len(_CACHE) > _CACHE_LIMIT:
                _CACHE.popitem(last=False)
    return selected.join(hits, on=["symbol", "date"], how="semi", maintain_order="left")
