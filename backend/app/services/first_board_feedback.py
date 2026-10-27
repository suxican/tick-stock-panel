"""Yesterday's actual first boards, independent of the strategy candidate pool.

Preparation uses a generation-checked repository range and local index sessions.
Only the existing first-board worker calls this module; HTTP and quote callbacks
never scan history. Returns and ratios are fractions, prices are raw yuan.
"""
from __future__ import annotations

import logging
import time
from contextlib import suppress
from datetime import date, datetime, timedelta

import polars as pl

from app.market_time import CN_TZ
from app.services.first_board_attention_data import AttentionData
from app.services.index_const import CORE_INDEX_SYMBOLS
from app.strategy.first_board import _UNIVERSE_PATTERNS

logger = logging.getLogger(__name__)
MEMBERSHIP_NOTE = "行业和概念使用当前成员的等权涨幅，并非板块指数或当日涨停原因；概念可能重复归属。"
_HISTORY_COLUMNS = ["symbol", "date", "raw_close", "limit_up_price"]


def _number(frame: pl.DataFrame, column: str) -> pl.Expr:
    if column not in frame.columns or frame.schema[column] == pl.Boolean:
        return pl.lit(None, dtype=pl.Float64)
    return pl.col(column).cast(pl.Float64, strict=False)


def _positive(expr: pl.Expr) -> pl.Expr:
    return (expr.is_finite() & (expr > 0)).fill_null(False)


def _summary(rows: list[dict]) -> dict:
    count = len(rows)
    valid = [row for row in rows if row["status"] == "valid"]
    result = {
        "sample_count": count, "quote_count": len(valid),
        "coverage": len(valid) / count if count else None,
        "mean_change_pct": None, "median_change_pct": None, "up_count": 0,
        "up_ratio": None, "limit_up_count": 0, "limit_up_ratio": None,
        "limit_known_count": 0, "loss_count": 0, "loss_ratio": None,
        "mean_open_premium": None, "mean_open_change": None, "open_count": 0,
        "small_sample": count < 10,
    }
    if not valid:
        return result
    frame = pl.DataFrame(valid)
    result.update(frame.select(
        pl.col("change_pct").mean().alias("mean_change_pct"),
        pl.col("change_pct").median().alias("median_change_pct"),
        (pl.col("change_pct") > 0).sum().alias("up_count"),
        (pl.col("change_pct") <= -.05 + 1e-12).sum().alias("loss_count"),
        pl.col("at_limit").cast(pl.Int64).sum().alias("limit_up_count"),
        pl.col("at_limit").count().alias("limit_known_count"),
        pl.col("open_premium").cast(pl.Float64).mean().alias("mean_open_premium"),
        pl.col("open_change").cast(pl.Float64).mean().alias("mean_open_change"),
        pl.col("open_premium").count().alias("open_count"),
    ).row(0, named=True))
    result["up_ratio"] = result["up_count"] / len(valid)
    result["loss_ratio"] = result["loss_count"] / len(valid)
    # Unknown/missing limits are not failed promotions. Keep the frozen denominator.
    if result["limit_known_count"] == count:
        result["limit_up_ratio"] = result["limit_up_count"] / count
    return result


def empty_feedback(message: str = "等待昨日首板样本与行情", *, status: str = "waiting") -> dict:
    return {"status": status, "message": message, "as_of": None, "sample_date": None,
            "observed_at": None, "quote_mode": "snapshot", "membership_note": MEMBERSHIP_NOTE,
            "history_excluded_count": 0,
            "groups": {key: _summary([]) for key in ("all", "main", "growth")},
            "rows": [], "sectors": []}


def expire_feedback(result: dict, *, now: datetime, max_quote_age_seconds: float = 30) -> None:
    """Invalidate a published live observation without losing its dated evidence."""
    if result.get("quote_mode") != "live" or result.get("status") not in {"ready", "partial"}:
        return
    stamp = result.get("observed_at")
    try:
        observed = datetime.fromisoformat(stamp)
        expired = not 0 <= (now - observed).total_seconds() <= max_quote_age_seconds
        for row in result.get("rows", []):
            if row.get("status") == "valid":
                quote = datetime.fromisoformat(row["quote_time"])
                expired |= not 0 <= (now - quote).total_seconds() <= max_quote_age_seconds
    except (ValueError, TypeError, KeyError):
        expired = True
    if expired:
        result.update(status="stale", message="实时行情已过期，保留最近观测数值，请查看日期与更新时间")


class FirstBoardFeedback:
    def __init__(self, repo):
        self.repo = repo
        self._membership = AttentionData(repo)
        self._key: tuple | None = None
        self._prepared_at = 0.0
        self._pool: list[dict] = []
        self._sample_date: date | None = None
        self._excluded = 0
        self._sessions: list[date] = []
        self._session_day: date | None = None
        self._session_checked = 0.0

    def invalidate(self) -> None:
        self._key = None
        self._sessions = []

    def _prepare(self, as_of: date) -> None:
        if (self._session_day != as_of or not self._sessions
                or time.monotonic() - self._session_checked >= 300):
            sessions_frame = self.repo.get_index_daily(
                CORE_INDEX_SYMBOLS[0], start=as_of - timedelta(days=45),
                end=as_of - timedelta(days=1), columns=["date"],
            )
            sessions = sorted({day for day in sessions_frame["date"].to_list()
                               if type(day) is date and day < as_of})[-3:]
            if len(sessions) != 3:
                raise ValueError("缺少前三个交易日的独立指数日线，无法确认昨日首板")
            from app.services import data_integrity

            with data_integrity._CAL_LOCK:
                cached = data_integrity._CAL[1]
                calendar = sorted(cached) if cached else []
            if (calendar and calendar[0] <= as_of <= calendar[-1]
                    and (as_of not in calendar or [day for day in calendar if day < as_of][-3:] != sessions)):
                raise ValueError("指数历史与已缓存交易日历不一致，请补齐历史数据")
            if not calendar or not calendar[0] <= as_of <= calendar[-1]:
                # Without a holiday calendar, only a fully evidenced weekday
                # sequence is safe. A weekday gap may be missing data or a holiday;
                # never guess which, or silently move the sample to an older day.
                weekdays = sorted(as_of - timedelta(days=offset) for offset in range(1, 10)
                                  if (as_of - timedelta(days=offset)).weekday() < 5)[-3:]
                if sessions != weekdays:
                    raise ValueError("缺少最近交易日连续性证据，请补齐指数日线或交易日历")
            self._sessions, self._session_day = sessions, as_of
            self._session_checked = time.monotonic()
        sessions = self._sessions
        token = self.repo.get_first_board_history_signature(sessions)
        key = (as_of, token)
        if self._key == key and time.monotonic() - self._prepared_at < 300:
            return
        previous, sample_date = sessions[-2:]
        # The narrow reader checks only completed partitions; current-session
        # writes cannot invalidate yesterday's fixed sample on every quote tick.
        history, loaded_token = self.repo.get_first_board_history(sessions)
        if loaded_token != token:
            raise ValueError("历史正在更新，等待同一版本数据后重新计算")
        if history is None or history.is_empty() or not set(_HISTORY_COLUMNS).issubset(history.columns):
            raise ValueError("昨日首板所需历史尚未就绪，请等待日线数据准备完成")
        if "name" not in history.columns:
            instruments = self.repo.get_instruments()
            if instruments.is_empty() or not {"symbol", "name"}.issubset(instruments.columns):
                raise ValueError("缺少股票名称，无法确认非ST样本范围")
            names = instruments.select("symbol", "name").filter(~pl.col("symbol").is_duplicated())
            history = history.join(names, on="symbol", how="left")
        history = history.filter(pl.col("date").is_in(sessions[-2:]))
        if set(history["date"].to_list()) != set(sessions[-2:]):
            raise ValueError("股票历史缺少对应交易日，不使用更早日期替代")
        complete = pl.lit(True)
        if "quote_ts" in history.columns:
            stamp = _number(history, "quote_ts")
            day_start = pl.col("date").cast(pl.Datetime).dt.replace_time_zone("Asia/Shanghai").dt.epoch("ms")
            complete = stamp.is_null() | stamp.is_between(day_start + 15 * 3600000,
                                                         day_start + 86400000, closed="left")
            if history.schema["quote_ts"] == pl.Boolean:
                complete = pl.lit(False)
        history = history.with_columns(
            _number(history, "raw_close").alias("raw_close"),
            _number(history, "limit_up_price").alias("limit_up_price"),
            pl.len().over("symbol", "date").alias("_count"),
        )
        # A null threshold with complete limit evidence is an explicitly known
        # no-limit session. A null caused by missing paired history or metadata
        # is unknown and must be reported, not silently removed from the sample.
        evidence = (_positive(pl.col("limit_up_price")) if "limit_evidence_known" not in history.columns
                    else pl.col("limit_evidence_known").fill_null(False)
                    if history.schema["limit_evidence_known"] == pl.Boolean else pl.lit(False))
        history = history.with_columns(
            (evidence & (_positive(pl.col("limit_up_price")) | pl.col("limit_up_price").is_null())
             & _positive(pl.col("raw_close")) & (pl.col("_count") == 1) & complete).alias("_history_known")
        ).with_columns(
            (pl.col("_history_known") & _positive(pl.col("limit_up_price"))).alias("_valid")
        )
        yesterday_scope = history.filter(
            (pl.col("date") == sample_date)
            & pl.col("symbol").str.contains(_UNIVERSE_PATTERNS["hs_a_non_st"])
            & pl.col("name").is_not_null() & (pl.col("name").str.len_chars() > 0)
            & ~pl.col("name").str.contains("(?i)ST|退")
        )
        incomplete = yesterday_scope.filter(
            ~pl.col("_history_known").fill_null(False)
        ).select("symbol").n_unique()
        yesterday = yesterday_scope.filter(
            _positive(pl.col("raw_close")) & _positive(pl.col("limit_up_price"))
            & (pl.col("raw_close") >= pl.col("limit_up_price"))
        )
        yesterday = yesterday.filter(pl.col("_valid"))
        prior = history.filter(pl.col("date") == previous).group_by("symbol").agg(
            ((pl.col("_history_known")
              & (pl.col("limit_up_price").is_null() | (pl.col("raw_close") < pl.col("limit_up_price")))).all()
             & (pl.len() == 1)).alias("_not_limit"),
            (pl.col("_history_known").all() & (pl.len() == 1)).alias("_prior_known"),
        )
        matched = yesterday.join(prior, on="symbol", how="left")
        excluded = incomplete + matched.filter(~pl.col("_prior_known").fill_null(False)).height
        pool = matched.filter(pl.col("_not_limit").fill_null(False)).select("symbol", "name").sort("symbol")
        if self.repo.get_first_board_history_signature(sessions) != token:
            raise ValueError("历史正在更新，等待同一版本数据后重新计算")
        self._membership.prepare(universe="hs_a_non_st")
        self._pool = [{**row, "board": "main" if row["symbol"].startswith(("60", "00")) else "growth"}
                      for row in pool.to_dicts()]
        self._sample_date, self._excluded = sample_date, excluded
        self._key, self._prepared_at = key, time.monotonic()

    @staticmethod
    def _quotes(current: pl.DataFrame, as_of: date, now: datetime, live: bool,
                max_age: float) -> dict[str, dict]:
        if current.is_empty() or not {"symbol", "date"}.issubset(current.columns):
            return {}
        frame = current.select(
            "symbol", "date", *[_number(current, col).alias(col) for col in (
                "raw_close", "close", "open", "change_pct", "limit_up_price", "quote_ts",
            )],
        ).with_columns(pl.len().over("symbol").alias("_count"))
        valid = (_positive(pl.col("raw_close")) & _positive(pl.col("close"))
                 & pl.col("change_pct").is_finite() & (pl.col("change_pct") > -1))
        age = now.timestamp() - pl.col("quote_ts") / 1000
        start = datetime.combine(as_of, datetime.min.time(), CN_TZ).timestamp() * 1000
        known_stamp = pl.col("quote_ts").is_not_null()
        timestamp_valid = pl.col("quote_ts").is_between(start, start + 86400000, closed="left") & (age >= 0)
        frame = frame.with_columns(
            pl.when(pl.col("_count") != 1).then(pl.lit("invalid"))
            .when((pl.col("date") != as_of).fill_null(True)).then(pl.lit("missing"))
            .when(~valid.fill_null(False)).then(pl.lit("invalid"))
            .when(known_stamp & ~timestamp_valid.fill_null(False)).then(pl.lit("invalid"))
            .when(pl.lit(live) & ~age.is_between(0, max_age).fill_null(False)).then(pl.lit("stale"))
            .otherwise(pl.lit("valid")).alias("status"),
        )
        usable = pl.col("status") == "valid"
        open_ok = usable & _positive(pl.col("open"))
        frame = frame.select(
            "symbol", "status", "quote_ts",
            pl.when(usable).then(pl.col("raw_close")).otherwise(None).alias("price"),
            pl.when(usable).then(pl.col("change_pct")).otherwise(None).alias("change_pct"),
            # open/close share the adjusted scale; change_pct supplies today's
            # authoritative return base, including ex-rights days.
            pl.when(open_ok).then(pl.col("open") / pl.col("close") * (1 + pl.col("change_pct")) - 1)
            .otherwise(None).alias("open_premium"),
            pl.when(open_ok).then(pl.col("close") / pl.col("open") - 1).otherwise(None).alias("open_change"),
            pl.when(usable & _positive(pl.col("limit_up_price")))
            .then(pl.col("raw_close") >= pl.col("limit_up_price")).otherwise(None).alias("at_limit"),
        ).unique("symbol", keep="first")
        return {row["symbol"]: row for row in frame.to_dicts()}

    def _sectors(self, rows: list[dict], quotes: dict[str, dict]) -> list[dict]:
        cohort = {row["symbol"]: row for row in rows}
        groups, membership = {}, []
        for group in self._membership.groups:
            symbols = sorted(set(group["members"]))
            members = [cohort[symbol] for symbol in symbols if symbol in cohort]
            if len(members) < 2:
                continue
            groups[group["id"]] = (group, symbols, _summary(members))
            membership.extend({"id": group["id"], "symbol": symbol} for symbol in symbols)
        if not groups:
            return []
        valid = [{"symbol": symbol, "change_pct": row["change_pct"]}
                 for symbol, row in quotes.items() if row["status"] == "valid"]
        frame = pl.DataFrame(valid, schema={"symbol": pl.String, "change_pct": pl.Float64})
        aggregated = pl.DataFrame(membership).join(frame, on="symbol", how="left").group_by("id").agg(
            pl.col("change_pct").count().alias("member_quote_count"),
            pl.col("change_pct").mean().alias("sector_mean_change_pct"),
            (pl.col("change_pct") > 0).sum().alias("up_count"),
        )
        output = []
        for aggregate in aggregated.to_dicts():
            group, symbols, summary = groups[aggregate["id"]]
            quoted = aggregate["member_quote_count"]
            coverage = quoted / len(symbols)
            ready = len(symbols) >= 5 and coverage >= .8
            output.append({
                "id": group["id"], "name": group["name"], "kind": group["kind"],
                "sample_count": summary["sample_count"], "quote_count": summary["quote_count"],
                "mean_change_pct": summary["mean_change_pct"], "limit_up_count": summary["limit_up_count"],
                "limit_known_count": summary["limit_known_count"],
                "member_count": len(symbols), "member_quote_count": quoted, "coverage": coverage,
                "sector_mean_change_pct": aggregate["sector_mean_change_pct"] if ready else None,
                "up_ratio": aggregate["up_count"] / quoted if ready and quoted else None,
                "status": "ready" if ready else "insufficient",
                "reason": None if ready else "板块成员少于5只或有效行情覆盖不足80%",
            })
        return sorted(output, key=lambda row: (row["kind"], -row["sample_count"], row["name"], row["id"]))

    def evaluate(self, current: pl.DataFrame, as_of: date, *, now: datetime,
                 live: bool, max_quote_age_seconds: float = 30) -> dict:
        now = now.replace(tzinfo=CN_TZ) if now.tzinfo is None else now.astimezone(CN_TZ)
        result = empty_feedback()
        result.update(as_of=str(as_of), observed_at=now.isoformat(), quote_mode="live" if live else "snapshot")
        if as_of > now.date():
            result.update(status="unavailable", message="行情日期晚于当前日期，暂不计算昨日首板反馈")
            return result
        try:
            self._prepare(as_of)
        except Exception as exc:  # noqa: BLE001 - optional observation must not stop the strategy worker
            self._key = None
            logger.warning("yesterday first-board preparation unavailable (%s)", type(exc).__name__)
            result.update(status="unavailable", message=str(exc) if isinstance(exc, ValueError)
                          else "昨日首板历史暂不可用，请检查日线数据后刷新")
            return result
        result.update(sample_date=str(self._sample_date), history_excluded_count=self._excluded)
        quotes = self._quotes(current, as_of, now, live, max_quote_age_seconds)
        rows = []
        for member in self._pool:
            quote = quotes.get(member["symbol"], {"status": "missing"})
            stamp = quote.get("quote_ts")
            quote_time = None
            if isinstance(stamp, (int, float)):
                with suppress(ValueError, OSError, OverflowError):
                    quote_time = datetime.fromtimestamp(stamp / 1000, CN_TZ).isoformat()
            status = quote["status"]
            rows.append({**member, **{key: quote.get(key) for key in (
                "price", "change_pct", "open_premium", "open_change", "at_limit",
            )}, "quote_time": quote_time, "status": status,
                "reason": {"valid": None, "missing": "缺少对应交易日行情", "stale": "行情已过期或时间缺失",
                           "invalid": "行情重复、价格或时间无效"}[status]})
        summaries = {key: _summary(rows if key == "all" else [r for r in rows if r["board"] == key])
                     for key in ("all", "main", "growth")}
        summary = summaries["all"]
        partial = (summary["quote_count"] < len(rows) or summary["limit_known_count"] < len(rows)
                   or self._excluded > 0)
        message = ("盘中实时反馈；二板数量为当前涨停状态" if live else
                   "最近行情快照；未另行确认最终收盘结果")
        if not rows:
            message = "对应交易日没有可确认的沪深非ST首板样本"
        if self._excluded:
            message += f"；{self._excluded}只股票缺少首板识别所需的历史或涨停依据，未纳入确认样本"
        if summary["quote_count"] < len(rows):
            message += "；部分样本行情缺失或过期，保留完整样本分母"
        result.update(status="partial" if partial else "ready", message=message,
                      groups=summaries, rows=rows, sectors=self._sectors(rows, quotes))
        if self._membership.reason:
            result["membership_note"] += " " + self._membership.reason
        return result
