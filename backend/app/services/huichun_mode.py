"""Daily Huichun screening and persisted signal-close observation tracking.

This service composes the audited A0 rules. Returns describe price observations,
not executable fills. Heavy repository reads run in a short-lived daemon worker.
"""

# User-facing prose uses native Chinese punctuation.
# ruff: noqa: RUF001

from __future__ import annotations

import copy
import hashlib
import json
import logging
import math
import threading
import time
import uuid
from collections.abc import Callable
from contextlib import suppress
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Literal

import polars as pl
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.market_time import CN_TZ
from app.price_limits import (
    GEM_REGISTRATION_DATE,
    MAIN_BOARD_LIMIT,
    is_risk_warning_name,
    price_limit_pct,
)
from app.services.fs_utils import atomic_write_text
from app.services.huichun_audit import (
    FACTOR_SCHEMA,
    RAW_COLUMNS,
    _main_board,
    _table_exists,
    prepare_batch,
)
from app.services.huichun_calendar import load_market_calendar
from app.strategy.huichun_a0 import A0Params, audit_a0
from app.strategy.huichun_watch import pending_crosses

logger = logging.getLogger(__name__)
_WRITE_LOCK = threading.RLock()
_LIMITATIONS = [
    "A0 日线收盘形态筛选，MACD 参数固定为 10/20/9；不包含 A1/B1 分钟信号。",
    "待金叉观察池只表示截至所选交易日差值收敛，尚未确认金叉，不作为买入或收益跟踪信号。",
    "按当前证券简称排除 ST/*ST；历史风险警示、退市状态及复权事件完整性未核实，资格仍待核验。",
    "收益以信号日复权收盘为基准，不含费用、滑点及成交限制，不代表可成交收益。",
    "按独立交易日历计数；缺口不填补，未来收益待观察。无可用独立日历时收益保持未知。",
    "复权事件与价格连续性冲突的股票被排除；该检查不能证明数据完全正确。",
]


def _now() -> datetime:
    return datetime.now(CN_TZ)


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, default=str)


def _digest(value: object) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _screening_stock(symbol: str) -> bool:
    # Match the existing stock board families, including STAR depositary receipts.
    # The daily stock table is separate from ETF/index tables; suffix checks also
    # prevent malformed or misplaced rows from entering the candidate universe.
    return _main_board(symbol) or (
        symbol.endswith(".SZ") and symbol.startswith(("300", "301"))
    ) or (
        symbol.endswith(".SH") and symbol.startswith(("688", "689"))
    )


class HuichunRules(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, frozen=True)

    warmup_bars: int = Field(default=250, ge=1, le=2000, strict=True)
    rally_threshold: float = Field(default=0.4, ge=0, le=10, strict=True)
    zero_threshold: float = Field(default=0.02, ge=0, le=1, strict=True)
    ma_window: int = Field(default=60, ge=2, le=500, strict=True)
    slope_lag: int = Field(default=5, ge=1, le=250, strict=True)


class HuichunConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    revision: int = Field(default=0, ge=0)
    updated_at: str | None = None
    rules: HuichunRules = Field(default_factory=HuichunRules)


class HuichunScanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    start_date: date | None = None
    end_date: date | None = None

    @model_validator(mode="after")
    def valid_range(self):
        if self.start_date and self.end_date and self.start_date > self.end_date:
            raise ValueError("开始日期不能晚于结束日期")
        return self


class HuichunTrackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    scan_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    candidate_ids: list[str] = Field(min_length=1, max_length=2000)


def _idle_job() -> dict:
    return {
        "id": None,
        "kind": None,
        "status": "idle",
        "started_at": None,
        "finished_at": None,
        "processed_symbols": 0,
        "total_symbols": 0,
        "error": None,
    }


class HuichunModeService:
    def __init__(
        self, repo, *, data_dir: Path | None = None, calendar_path: Path | None = None,
        publish: Callable[[list[dict]], None] | None = None,
    ):
        self.repo = repo
        self._publish = publish
        self.root: Path = (data_dir or repo.store.data_dir) / "user_data" / "huichun"
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._refresh_requested = False
        self._calendar_source = "observed_market_daily_dates"
        self._latest_observed_date: date | None = None
        self._calendar_path = calendar_path
        if calendar_path is None:
            candidates = [
                self.root / "market_calendar.json",
                Path(__file__).parents[3]
                / "docs/策略/回春策略/evidence/exchange_calendar_2019_2026.json",
            ]
            self._calendar_path = next((path for path in candidates if path.exists()), None)
        self._config = self._read_config()
        state = self._read("snapshot.json", {"job": _idle_job(), "scan": None})
        self._snapshot = state
        if state["job"]["status"] == "running":
            state["job"].update(status="failed", error="上次任务因服务重启中断，请重新运行")

    def _read(self, name: str, default: dict) -> dict:
        path = self.root / name
        if not path.exists():
            return copy.deepcopy(default)
        try:
            result = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(result, dict):
                raise ValueError("expected object")
            return result
        except (OSError, ValueError, TypeError) as exc:
            raise ValueError("回春模式保存文件无法读取，已保留原文件") from exc

    def _write(self, name: str, value: dict) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        atomic_write_text(self.root / name, _json(value))

    def _read_config(self) -> HuichunConfig:
        current = self._read("config.json", {"current": HuichunConfig().model_dump()})
        return HuichunConfig.model_validate(current["current"])

    def get_config(self) -> dict:
        with self._lock, _WRITE_LOCK:
            self._config = self._read_config()
            return self._config.model_dump(mode="json")

    def save_config(self, rules: HuichunRules | dict, *, expected_revision: int) -> dict:
        rules = HuichunRules.model_validate(rules)
        with self._lock, _WRITE_LOCK:
            current = self._read_config()
            if current.revision != expected_revision:
                raise ValueError("规则版本已变化，请重新加载后保存")
            if current.rules == rules:
                self._config = current
                return current.model_dump(mode="json")
            updated = HuichunConfig(
                revision=current.revision + 1, updated_at=_now().isoformat(), rules=rules
            )
            history = self._read("config.json", {"versions": [current.model_dump(mode="json")]})
            self._write(
                "config.json",
                {
                    "current": updated.model_dump(mode="json"),
                    "versions": [*history["versions"], updated.model_dump(mode="json")],
                },
            )
            self._config = updated
            return updated.model_dump(mode="json")

    def get_snapshot(self) -> dict:
        with self._lock:
            return copy.deepcopy({**self._snapshot, "config_revision": self._config.revision})

    def get_tracking(self) -> dict:
        with self._lock, _WRITE_LOCK:
            data = self._read("tracking.json", {"records": [], "updated_at": None})
            data["records"] = [r for r in data["records"] if not r.get("removed_at")]
            return data

    def add_tracking(self, scan_id: str, candidate_ids: list[str]) -> dict:
        request = HuichunTrackRequest(scan_id=scan_id, candidate_ids=candidate_ids)
        with self._lock, _WRITE_LOCK:
            scan = self._read(f"scan-{request.scan_id}.json", {})
            if not scan:
                raise ValueError("筛选结果不存在，请重新筛选")
            lookup = {r["id"]: r for r in scan["candidates"]}
            if set(request.candidate_ids) - lookup.keys():
                raise ValueError("所选候选不属于这次筛选结果")
            data = self._read("tracking.json", {"records": [], "updated_at": None})
            records = {r["id"]: r for r in data["records"]}
            for key in dict.fromkeys(request.candidate_ids):
                previous = records.get(key)
                if previous and not previous.get("removed_at"):
                    continue
                candidate = lookup[key]
                records[key] = {
                    "id": key,
                    "symbol": candidate["symbol"],
                    "name": candidate["name"],
                    "signal_date": candidate["signal_date"],
                    "rule_revision": scan["rule_revision"],
                    "rules": scan["rules"],
                    "added_at": _now().isoformat(),
                    "base_close": candidate["raw_close"],
                    "base_adjusted_close": candidate["adjusted_close"],
                    "baseline_fingerprint": candidate["baseline_fingerprint"],
                    "returns": self._empty_returns(),
                    "updated_at": None,
                }
                if previous:
                    records[key]["previous_observations"] = [
                        *previous.get("previous_observations", []),
                        {k: v for k, v in previous.items() if k != "previous_observations"},
                    ]
            data.update(records=list(records.values()), updated_at=_now().isoformat())
            self._write("tracking.json", data)
        # Refresh uses the same single worker; adding during a scan remains safe.
        self.start_tracking_refresh()
        return self.get_tracking()

    def remove_tracking(self, record_id: str) -> dict:
        with self._lock, _WRITE_LOCK:
            data = self._read("tracking.json", {"records": [], "updated_at": None})
            found = False
            for record in data["records"]:
                if record["id"] == record_id:
                    record["removed_at"] = _now().isoformat()
                    found = True
            if not found:
                raise ValueError("跟踪记录不存在")
            data["updated_at"] = _now().isoformat()
            self._write("tracking.json", data)
        return self.get_tracking()

    @staticmethod
    def _empty_returns(status="pending") -> list[dict]:
        return [
            {"horizon": n, "target_date": None, "status": status, "return_pct": None}
            for n in range(1, 6)
        ]

    def start_scan(self, start_date: date | None = None, end_date: date | None = None) -> dict:
        return self._start("scan", HuichunScanRequest(start_date=start_date, end_date=end_date))

    def start_tracking_refresh(self) -> dict:
        return self._start("tracking", None)

    def _start(self, kind: str, request: HuichunScanRequest | None) -> dict:
        with self._lock, _WRITE_LOCK:
            if self._stop.is_set():
                raise ValueError("服务正在关闭")
            if self._snapshot["job"]["status"] == "running":
                if kind == "tracking":
                    self._refresh_requested = True
                return self.get_snapshot()
            config = self._read_config()
            self._config = config
            job = {
                **_idle_job(),
                "id": uuid.uuid4().hex,
                "kind": kind,
                "status": "running",
                "started_at": _now().isoformat(),
            }
            pending = {"job": job, "scan": self._snapshot["scan"]}
            self._write("snapshot.json", pending)
            self._snapshot = pending
            self._thread = threading.Thread(
                target=self._run,
                args=(job["id"], kind, request, config),
                daemon=True,
                name="huichun-mode",
            )
            self._thread.start()
            return self.get_snapshot()

    def close(self, timeout: float = 1.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=timeout)

    def wait(self, timeout: float = 30.0) -> dict:
        """Bounded worker join for lifecycle management and tests, never HTTP GET."""
        deadline = time.monotonic() + timeout
        while self._thread and self._thread.is_alive() and time.monotonic() < deadline:
            self._thread.join(max(0, deadline - time.monotonic()))
        return self.get_snapshot()

    def _check_stop(self) -> None:
        if self._stop.is_set():
            raise ValueError("任务已因服务关闭中止")

    def _input_signature(self) -> list:
        signature = [
            (
                p.relative_to(self.repo.store.data_dir).as_posix(),
                p.stat().st_size,
                p.stat().st_mtime_ns,
            )
            for table in ("kline_daily", "adj_factor", "instruments")
            for p in sorted((self.repo.store.data_dir / table).rglob("*.parquet"))
        ]
        if self._calendar_path and self._calendar_path.exists():
            stat = self._calendar_path.stat()
            signature.append(("calendar", stat.st_size, stat.st_mtime_ns))
        return signature

    def _run(self, job_id, kind, request, config) -> None:
        try:
            before = self._input_signature()
            scan = self._scan(job_id, request, config) if kind == "scan" else None
            tracking = self._calculate_tracking()
            self._check_stop()
            if before != self._input_signature():
                raise ValueError("计算期间行情已更新，请重新运行以使用一致数据")
            with self._lock, _WRITE_LOCK:
                if scan is not None:
                    self._write(f"scan-{job_id}.json", scan)
                self._merge_tracking(tracking)
                pending = copy.deepcopy(self._snapshot)
                if scan is not None:
                    pending["scan"] = scan
                pending["job"].update(status="completed", finished_at=_now().isoformat())
                self._write("snapshot.json", pending)
                self._snapshot = pending
        except Exception as exc:
            logger.exception("Huichun mode worker failed")
            with self._lock, _WRITE_LOCK:
                message = (
                    str(exc) if isinstance(exc, ValueError) else "计算失败，请检查日线数据后重试"
                )
                self._snapshot["job"].update(
                    status="failed", error=message, finished_at=_now().isoformat()
                )
                try:
                    self._write("snapshot.json", self._snapshot)
                except OSError:
                    logger.exception("Huichun mode failure state could not be saved")
        else:
            if scan is not None:
                self._publish_scan_events(scan)
        finally:
            with self._lock:
                refresh = self._refresh_requested and not self._stop.is_set()
                self._refresh_requested = False
            if refresh:
                self.start_tracking_refresh()

    def _publish_scan_events(self, scan: dict) -> None:
        if self._publish is None:
            return
        try:
            day = scan["observation_date"]
            # Historical reviews retain their results but must not become new
            # monitoring signals, nor may an obsolete rule revision notify.
            with self._lock, _WRITE_LOCK:
                if (
                    day != str(self._latest_observed_date)
                    or scan["rule_revision"] != self._read_config().revision
                ):
                    return
            events = []
            published_at = int(_now().timestamp() * 1000)
            for pool, date_field, event_type, label in (
                ("candidates", "signal_date", "a0_confirmed", "A0 形态确认"),
                ("observations", "observation_date", "pending_cross", "待金叉观察，尚未确认金叉"),
            ):
                for row in scan.get(pool, []):
                    if row[date_field] != day:
                        continue
                    events.append({
                        "id": f"huichun:{event_type}:{row['id']}",
                        "source": "huichun", "type": event_type, "event_type": event_type,
                        "ts": published_at, "date": day, "observation_date": day,
                        "symbol": row["symbol"], "name": row["name"],
                        "price": row["raw_close"], "rule_revision": scan["rule_revision"],
                        "message": (
                            f"回春模式 {row['name']}（{row['symbol']}）{day}：{label}；"
                            "日线观察，不代表成交。"
                        ),
                    })
            if events:
                self._publish(events)
        except Exception:
            # Notification availability must not invalidate persisted research.
            logger.exception("Huichun mode monitor publication failed")

    def _calendar(self) -> list[date]:
        if not _table_exists(self.repo, "kline_daily"):
            raise ValueError("暂无日线数据，请先同步历史日线")
        now = _now()
        clauses = "date <= ?" if now.hour >= 15 else "date < ?"
        observed = [
            r[0]
            for r in self.repo.execute_all(
                f"SELECT DISTINCT date FROM kline_daily WHERE {clauses} ORDER BY date", [now.date()]
            )
        ]
        self._latest_observed_date = observed[-1] if observed else None
        self._calendar_source = "observed_market_daily_dates"
        if observed and self._calendar_path is not None:
            cutoff = now.date() if now.hour >= 15 else now.date() - timedelta(days=1)
            try:
                calendar = load_market_calendar(self._calendar_path, start=observed[0], end=cutoff)
            except (OSError, ValueError):
                # Unavailable evidence is an explicit observation-only fallback.
                logger.warning("Huichun market calendar unavailable or outside coverage")
            else:
                if set(observed) - set(calendar):
                    raise ValueError("日线日期与独立交易日历冲突，请检查数据")
                self._calendar_source = "official_exchange_calendar"
                return calendar
        return observed

    def _factors(self, symbols, end) -> pl.DataFrame:
        if not symbols or not _table_exists(self.repo, "adj_factor"):
            return pl.DataFrame(schema=FACTOR_SCHEMA)
        marks = ",".join("?" for _ in symbols)
        rows = self.repo.execute_all(
            "SELECT symbol,trade_date,ex_factor FROM adj_factor "
            f"WHERE (trade_date <= ? OR trade_date IS NULL) AND symbol IN ({marks}) "
            "ORDER BY symbol,trade_date",
            [end, *symbols],
        )
        return pl.DataFrame(rows, schema=FACTOR_SCHEMA, orient="row")

    def _raw(self, symbols, end) -> pl.DataFrame:
        columns = [*RAW_COLUMNS]
        available = {r[0] for r in self.repo.execute_all("DESCRIBE kline_daily")}
        if "quote_ts" in available:
            columns.append("quote_ts")
        marks = ",".join("?" for _ in symbols)
        rows = self.repo.execute_all(
            f"SELECT {','.join(columns)} FROM kline_daily WHERE date <= ? "
            f"AND symbol IN ({marks}) ORDER BY symbol,date",
            [end, *symbols],
        )
        schema = {
            c: pl.String if c == "symbol" else pl.Date if c == "date" else pl.Float64
            for c in columns
        }
        return pl.DataFrame(rows, schema=schema, orient="row")

    @staticmethod
    def _invalid_factors(raw, factors, calendar) -> set[str]:
        """Reject implausible adjusted jumps; this is a data-quality heuristic.

        Board limits plus a two-point/tick margin retain the existing main-board
        gate. These do not establish historical trading eligibility or model IPO
        sessions without price limits.
        """
        bad = set(
            factors.filter(
                pl.col("trade_date").is_null()
                | pl.col("ex_factor").is_null()
                | ~pl.col("ex_factor").is_finite()
                | (pl.col("ex_factor") <= 0)
                | pl.struct("symbol", "trade_date").is_duplicated()
            )["symbol"].to_list()
        )
        if factors.is_empty() or raw.is_empty():
            return bad
        previous = {calendar[i]: calendar[i - 1] for i in range(1, len(calendar))}
        prices = {(s, d): c for s, d, c in raw.select("symbol", "date", "close").iter_rows()}
        first_dates = {
            s: group["date"].min()
            for (s,), group in raw.partition_by("symbol", as_dict=True).items()
        }
        for symbol, day, factor in factors.iter_rows():
            if symbol in bad or factor == 1:
                continue
            old, current = prices.get((symbol, previous.get(day))), prices.get((symbol, day))
            if day <= first_dates.get(symbol, day):
                continue  # Events before the available price history only change its common scale.
            if not old or not current or not math.isfinite(old) or not math.isfinite(current):
                bad.add(symbol)
                continue
            limit = price_limit_pct(symbol, day)
            # The shared helper supplies current board limits. GEM's historical
            # 10% regime also matters when auditing old adjustment events.
            if symbol.startswith(("300", "301")) and day < GEM_REGISTRATION_DATE:
                limit = MAIN_BOARD_LIMIT
            if abs(current * factor / old - 1) > limit + 0.02 + 0.01 / old:
                bad.add(symbol)
        return bad

    @staticmethod
    def _baseline(raw, factors, symbol, signal_date) -> str:
        rows = raw.filter((pl.col("symbol") == symbol) & (pl.col("date") == signal_date))
        events = factors.filter(
            (pl.col("symbol") == symbol) & (pl.col("trade_date") <= signal_date)
        )
        return _digest({"bar": rows.select(*RAW_COLUMNS).rows(), "factors": events.rows()})

    def _scan(self, job_id, request, config) -> dict:
        calendar = self._calendar()
        if not calendar:
            raise ValueError("暂无已收盘日线数据，请先同步历史日线")
        end = request.end_date or self._latest_observed_date
        start = request.start_date or end
        if start > end or end > self._latest_observed_date:
            raise ValueError("筛选日期范围无效或晚于最新已收盘数据日")
        calendar = [d for d in calendar if d <= end]
        if not calendar:
            raise ValueError("所选日期之前没有日线数据")
        symbols = [
            r[0]
            for r in self.repo.execute_all(
                "SELECT DISTINCT symbol FROM kline_daily WHERE date <= ? ORDER BY symbol", [end]
            )
            if _screening_stock(r[0])
        ]
        if not symbols:
            raise ValueError("暂无沪深主板、创业板或科创板日线数据")
        names, listings = {}, {}
        if _table_exists(self.repo, "instruments"):
            available = {r[0] for r in self.repo.execute_all("DESCRIBE instruments")}
            listing_expr = "listing_date" if "listing_date" in available else "NULL"
            for symbol, name, listing in self.repo.execute_all(
                f"SELECT symbol,name,{listing_expr} FROM instruments"
            ):
                names[symbol] = name or symbol
                with suppress(ValueError, TypeError):
                    listings[symbol] = date.fromisoformat(str(listing)[:10])
        st_symbols = {symbol for symbol in symbols if is_risk_warning_name(names.get(symbol))}
        symbols = [symbol for symbol in symbols if symbol not in st_symbols]
        candidates, observations, factor_excluded, gaps = [], [], set(), 0
        params = A0Params(**config.rules.model_dump())
        with self._lock:
            self._snapshot["job"]["total_symbols"] = len(symbols)
        for offset in range(0, len(symbols), 128):
            self._check_stop()
            batch = symbols[offset : offset + 128]
            raw, factors = self._raw(batch, end), self._factors(batch, end)
            bad = self._invalid_factors(raw, factors, calendar)
            factor_excluded.update(bad)
            safe = [s for s in batch if s not in bad]
            prepared = prepare_batch(
                raw.filter(~pl.col("symbol").is_in(bad)),
                factors.filter(~pl.col("symbol").is_in(bad)),
                market_dates=calendar,
                end=end,
                listing_dates=listings,
                expected_symbols=safe,
            )
            if not prepared.coverage.is_empty():
                gaps += prepared.coverage.filter(pl.col("missing_observed_days") > 0).height
            if not prepared.frame.is_empty():
                result = audit_a0(
                    prepared.frame,
                    # Observation state and the preceding indicator must not
                    # depend on the selected confirmed-signal start date.
                    start=prepared.frame["date"].min(),
                    end=end,
                    params=params,
                )
                raw_prices = {
                    (s, d): c for s, d, c in raw.select("symbol", "date", "close").iter_rows()
                }
                for row in result.signals.filter(pl.col("r_date") >= start).iter_rows(named=True):
                    symbol, day = row["symbol"], row["r_date"]
                    candidates.append(
                        {
                            "id": _digest(
                                [symbol, day, config.revision, config.rules.model_dump()]
                            ),
                            "symbol": symbol,
                            "name": names.get(symbol, symbol),
                            "signal_date": str(day),
                            "g_date": str(row["g_date"]),
                            "d_date": str(row["d_date"]),
                            "rally_return": row["rally_return"],
                            "zero_distance": row["zero_distance"],
                            "close_above_ma_pct": row["r_close"] / row["ma60"] - 1,
                            "ma_slope_pct": row["ma60"] / row["ma60_lag"] - 1,
                            "raw_close": raw_prices[symbol, day],
                            "adjusted_close": row["r_close"],
                            "eligibility_status": "unknown",
                            "rule_revision": config.revision,
                            "baseline_fingerprint": self._baseline(raw, factors, symbol, day),
                        }
                    )
                pending = pending_crosses(result, observation_date=calendar[-1], params=params)
                for row in pending.iter_rows(named=True):
                    symbol, day = row["symbol"], row["observation_date"]
                    observations.append(
                        {
                            **row,
                            "id": _digest([
                                "pending_cross", symbol, day, config.revision,
                                config.rules.model_dump(),
                            ]),
                            "name": names.get(symbol, symbol),
                            "observation_date": str(day),
                            "g_date": str(row["g_date"]),
                            "d_date": str(row["d_date"]),
                            "raw_close": raw_prices[symbol, day],
                            "eligibility_status": "unknown",
                            "rule_revision": config.revision,
                        }
                    )
            with self._lock:
                self._snapshot["job"]["processed_symbols"] = min(offset + 128, len(symbols))
        return {
            "id": job_id,
            "start_date": str(start),
            "end_date": str(end),
            "observation_date": str(calendar[-1]),
            "created_at": _now().isoformat(),
            "rule_revision": config.revision,
            "rules": config.rules.model_dump(),
            "candidates": sorted(
                candidates, key=lambda r: (r["signal_date"], r["rally_return"]), reverse=True
            ),
            "observations": sorted(observations, key=lambda r: (r["gap_distance"], r["symbol"])),
            "coverage": {
                "universe": "sh_sz_a_shares",
                "st_filter": "current_instrument_name",
                "st_excluded_count": len(st_symbols),
                "symbol_count": len(symbols),
                "candidate_count": len(candidates),
                "factor_excluded_count": len(factor_excluded),
                "gap_symbol_count": gaps,
                "latest_daily_date": str(calendar[-1]),
                "calendar_source": self._calendar_source,
            },
            "limitations": list(_LIMITATIONS),
        }

    def _tracking_quotes(self, symbols: list[str]) -> dict[str, tuple[date, float]]:
        """Read one shared snapshot in the worker; no per-record quote calls."""
        try:
            frame, _ = self.repo.get_enriched_latest()
        except Exception:
            logger.warning("Huichun current quote cache unavailable", exc_info=True)
            return {}
        if not {"symbol", "date", "raw_close"}.issubset(frame.columns):
            return {}
        frame = frame.filter(pl.col("symbol").is_in(symbols))
        frame = frame.filter(~pl.col("symbol").is_duplicated())
        quotes = {}
        for symbol, day, price in frame.select("symbol", "date", "raw_close").iter_rows():
            if isinstance(day, str):
                try:
                    parsed = date.fromisoformat(day)
                    day = parsed if parsed.isoformat() == day else None
                except ValueError:
                    day = None
            if (
                type(day) is date
                and day <= _now().date()
                and isinstance(price, (int, float))
                and not isinstance(price, bool)
                and math.isfinite(price)
                and price > 0
            ):
                quotes[symbol] = (day, float(price))
        return quotes

    def _current_basis(self, record, quote, raw, factors, calendar, *, changed, undated) -> dict:
        """Validate a same-date multiplier without storing or freezing live price."""
        day = date.fromisoformat(record["signal_date"])
        result = {"quote_date": None, "status": "no_quote", "factor_multiplier": None}
        if quote is None or quote[0] < day:
            return result
        target, current_price = quote
        result["quote_date"] = str(target)
        if undated:
            result["status"] = "invalid_factor"
        elif changed:
            result["status"] = "baseline_changed"
        elif self._calendar_source != "official_exchange_calendar" or day not in calendar or target not in calendar:
            result["status"] = "unknown_gap"
        else:
            symbol = record["symbol"]
            window = [d for d in calendar if day <= d <= target]
            scoped_raw = raw.filter((pl.col("symbol") == symbol) & pl.col("date").is_between(day, target))
            prepared = prepare_batch(
                scoped_raw, pl.DataFrame(schema=FACTOR_SCHEMA), market_dates=window,
                end=target, listing_dates={symbol: day},
            )
            prices = {d: c for d, c in prepared.frame.select("date", "close").iter_rows()}
            base = prices.get(day)
            # The quote may be intraday. Every earlier session still needs a
            # valid completed bar; the signal itself must retain its close.
            if base is None or any(d not in prices for d in window[:-1]):
                result["status"] = "unknown_gap"
            elif base != record.get("base_close"):
                result["status"] = "baseline_changed"
            else:
                events = factors.filter(
                    (pl.col("symbol") == symbol)
                    & (pl.col("trade_date") > day)
                    & (pl.col("trade_date") <= target)
                )
                event_prices = pl.DataFrame(
                    [(symbol, d, prices[d]) for d in window[:-1]] + [(symbol, target, current_price)],
                    schema={"symbol": pl.String, "date": pl.Date, "close": pl.Float64}, orient="row",
                )
                if symbol in self._invalid_factors(event_prices, events, calendar):
                    result["status"] = "invalid_factor"
                else:
                    multiplier = math.prod(events["ex_factor"].to_list())
                    if not math.isfinite(multiplier) or multiplier <= 0:
                        result["status"] = "invalid_factor"
                    else:
                        result.update(status="ok", factor_multiplier=multiplier)
        return result

    def _calculate_tracking(self) -> dict[str, dict]:
        records = self.get_tracking()["records"]
        if not records:
            return {}
        calendar = self._calendar()
        if not calendar:
            raise ValueError("暂无已收盘日线数据，跟踪结果未更新")
        end = calendar[-1]
        symbols = sorted({r["symbol"] for r in records})
        quotes = self._tracking_quotes(symbols)
        quote_end = max([end, *(quote[0] for quote in quotes.values())])
        raw, factors = self._raw(symbols, quote_end), self._factors(symbols, quote_end)
        quote_calendar = calendar
        if quote_end > end:
            try:
                quote_calendar = load_market_calendar(self._calendar_path, start=calendar[0], end=quote_end) if self._calendar_path else []
            except (OSError, ValueError):
                quote_calendar = []
        indices = {day: i for i, day in enumerate(calendar)}
        updates = {}
        for record in records:
            self._check_stop()
            symbol, day = record["symbol"], date.fromisoformat(record["signal_date"])
            index = indices.get(day)
            window = calendar[index : index + 6] if index is not None else []
            scope_end = window[-1] if window else day
            scoped_raw = raw.filter(
                (pl.col("symbol") == symbol) & pl.col("date").is_between(day, scope_end)
            )
            scoped_factors = factors.filter(
                (pl.col("symbol") == symbol)
                & (pl.col("trade_date") > day)
                & (pl.col("trade_date") <= scope_end)
            )
            # Before-signal factors cancel in the return ratio. Preserve their
            # fingerprint, then apply only events within each observation window.
            prepared = prepare_batch(
                scoped_raw,
                pl.DataFrame(schema=FACTOR_SCHEMA),
                market_dates=window or [day],
                end=scope_end,
                listing_dates={symbol: day},
            )
            prices = {d: c for d, c in prepared.frame.select("date", "close").iter_rows()}
            base = prices.get(day)
            changed = record["baseline_fingerprint"] != self._baseline(raw, factors, symbol, day)
            undated_factor = not factors.filter(
                (pl.col("symbol") == symbol) & pl.col("trade_date").is_null()
            ).is_empty()
            observations = []
            for horizon in range(1, 6):
                target_index = index + horizon if index is not None else None
                target = (
                    calendar[target_index]
                    if target_index is not None and target_index < len(calendar)
                    else None
                )
                status, value = "pending", None
                if undated_factor:
                    status = "invalid_factor"
                elif (
                    self._calendar_source != "official_exchange_calendar"
                    or base is None
                    or index is None
                ):
                    status = "unknown_gap"
                elif changed:
                    status = "baseline_changed"
                elif target is not None:
                    events = scoped_factors.filter(pl.col("trade_date") <= target)
                    bad = self._invalid_factors(
                        scoped_raw.filter(pl.col("date") <= target), events, calendar
                    )
                    path = [prices.get(d) for d in calendar[index + 1 : target_index + 1]]
                    if symbol in bad:
                        status = "invalid_factor"
                    elif (
                        any(p is None for p in path)
                        or self._calendar_source != "official_exchange_calendar"
                    ):
                        status = "unknown_gap"
                    else:
                        status = "ok"
                        value = path[-1] * math.prod(events["ex_factor"].to_list()) / base - 1
                observations.append(
                    {
                        "horizon": horizon,
                        "target_date": str(target) if target else None,
                        "status": status,
                        "return_pct": value,
                    }
                )
            updates[record["id"]] = {
                "returns": observations,
                "current_basis": self._current_basis(
                    record, quotes.get(symbol), raw, factors, quote_calendar,
                    changed=changed, undated=undated_factor,
                ),
                "updated_at": _now().isoformat(),
                "baseline_fingerprint": record["baseline_fingerprint"],
                "added_at": record["added_at"],
            }
        return updates

    def _merge_tracking(self, updates: dict) -> None:
        # Merge against the newest file so adds/removals during a scan are retained.
        if not updates:
            return
        data = self._read("tracking.json", {"records": [], "updated_at": None})
        for record in data["records"]:
            update = updates.get(record["id"])
            if (
                update
                and not record.get("removed_at")
                and record["baseline_fingerprint"] == update["baseline_fingerprint"]
                and record["added_at"] == update["added_at"]
            ):
                record.update(update)
        data["updated_at"] = _now().isoformat()
        self._write("tracking.json", data)
