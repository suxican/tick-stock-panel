"""Background-only local input adapter for first-board attention.

Reuses the sector catalog and repository; no provider fetches, migrations or
historical directory scans are performed by this adapter.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from collections import defaultdict
from copy import deepcopy
from datetime import datetime
from pathlib import Path

import polars as pl

from app.data_providers.capabilities import capability_gate
from app.market_time import CN_TZ
from app.services.minute_adjust import compute_ratio_pairs, minute_basis_is_raw
from app.services.sector_monitor import SectorMonitorService
from app.services.sector_rotation import _DEFAULT_EXCLUDE_SECTORS, _MAX_AUTO_MEMBERS
from app.strategy.first_board import _UNIVERSE_PATTERNS

logger = logging.getLogger(__name__)
_FULL_SYMBOL = re.compile(r"^\d{6}\.(SH|SZ|BJ)$")


class AttentionData:
    """Used by one background worker; publish completed membership atomically."""

    def __init__(self, repo) -> None:
        self._repo = repo
        self._data_dir = Path(repo.store.data_dir)
        self._catalog = SectorMonitorService(repo)
        self._membership: tuple[list[dict], str | None, str | None] = ([], None, "板块成员尚未准备")
        self._minute_cache: tuple[tuple, pl.DataFrame, bool, str | None] | None = None

    @property
    def groups(self) -> list[dict]:
        return deepcopy(self._membership[0])

    @property
    def version(self) -> str | None:
        return self._membership[1]

    @property
    def reason(self) -> str | None:
        return self._membership[2]

    def prepare(self, *, universe: str) -> None:
        """Refresh current membership, not historical point-in-time membership.

        An unresolved bare code invalidates its entire group, so dropping an
        unresolvable constituent can never increase apparent quote coverage.
        """
        try:
            pattern = re.compile(_UNIVERSE_PATTERNS[universe])
            instruments = self._repo.get_instruments()
            if instruments.is_empty() or not {"symbol", "name"}.issubset(instruments.columns):
                self._membership = ([], None, "缺少股票维表，无法确认成员代码与非ST范围")
                return
            aliases: dict[str, set[str]] = defaultdict(set)
            excluded_names = set()
            for row in instruments.select("symbol", "name").iter_rows(named=True):
                symbol = str(row["symbol"] or "").strip().upper()
                if not _FULL_SYMBOL.fullmatch(symbol):
                    continue
                aliases[symbol.split(".")[0]].add(symbol)
                if re.search("ST|退", str(row["name"] or ""), re.IGNORECASE):
                    excluded_names.add(symbol)
            targets = self._catalog.membership_snapshot()
            parents = {(item["source_id"], item["field"], item.get("value", "").split("-")[0])
                       for item in targets if item.get("kind") == "industry" and item.get("level") == 2}
            groups, unresolved = [], 0
            for target in targets:
                kind = target.get("kind")
                if kind not in {"industry", "concept"}:
                    continue
                if kind == "industry":
                    level = target.get("level")
                    parent = (target["source_id"], target["field"], target.get("value", "").split("-")[0])
                    if level not in {1, 2} or (level == 1 and parent in parents):
                        continue
                name = str(target.get("name") or "").strip()
                if not name or (kind == "concept" and any(term in name for term in _DEFAULT_EXCLUDE_SECTORS)):
                    continue
                resolved, invalid = set(), False
                for raw in target.get("members", []):
                    member = str(raw or "").strip().upper()
                    if _FULL_SYMBOL.fullmatch(member):
                        resolved.add(member)
                    elif re.fullmatch(r"\d{6}", member) and len(aliases.get(member, set())) == 1:
                        resolved.update(aliases[member])
                    else:
                        invalid = True
                        break
                if invalid:
                    unresolved += 1
                    continue
                if len(resolved) > _MAX_AUTO_MEMBERS:
                    continue
                members = sorted(symbol for symbol in resolved
                                 if pattern.fullmatch(symbol) and symbol not in excluded_names)
                if members:
                    groups.append({"id": target["key"], "kind": kind, "name": name,
                                   "source_id": target["source_id"], "field": target["field"],
                                   "members": members})
            groups.sort(key=lambda group: (group["kind"], group["source_id"], group["field"], group["id"]))
            reason = f"{unresolved}个板块含无法唯一映射的成员，整组暂不参与计算" if unresolved else None
            if not groups:
                self._membership = ([], None, reason or "缺少符合范围的行业或概念成员")
                return
            encoded = json.dumps({"universe": universe, "groups": groups}, ensure_ascii=False,
                                 sort_keys=True, separators=(",", ":")).encode("utf-8")
            self._membership = (groups, hashlib.sha256(encoded).hexdigest(), reason)
        except Exception as exc:  # noqa: BLE001 - optional context must not break first-board monitoring
            logger.warning("first-board attention membership unavailable (%s)", type(exc).__name__)
            self._membership = ([], None, "板块成员读取失败，暂不计算联动")

    def minutes(self, symbols: set[str], *, now: datetime) -> tuple[pl.DataFrame, bool, str | None]:
        """Read only today's stock partition in one batch, with a causal cutoff.

        Cache identity includes the exact partition's stat, so late writes in
        the same minute invalidate results without scanning other trading days.
        Returned frames are read-only inputs: cache hits retain object identity
        so downstream feature computation can reuse the same completed batch.
        """
        now = now.replace(tzinfo=CN_TZ) if now.tzinfo is None else now.astimezone(CN_TZ)
        cutoff = now.replace(second=0, microsecond=0, tzinfo=None)
        raw = False
        try:
            raw = minute_basis_is_raw(self._data_dir)
            minute_allowed, minute_source = capability_gate("minute")
            full_allowed, full_source = capability_gate("full_minute")
            if not (minute_allowed or full_allowed):
                return pl.DataFrame(), raw, "当前数据源缺少可用分钟能力，请在数据源设置中检查"
            if not symbols:
                return pl.DataFrame(), raw, "当前无首板观察候选"
            partition = self._data_dir / "kline_minute" / f"date={cutoff.date()}" / "part.parquet"
            batch = None
            try:
                stat = partition.stat()
                batch = (stat.st_mtime_ns, stat.st_size)
            except FileNotFoundError:
                pass
            key = (cutoff, tuple(sorted(symbols)), self.version, batch,
                   minute_allowed, minute_source, full_allowed, full_source, raw)
            if self._minute_cache is not None and self._minute_cache[0] == key:
                return self._minute_cache[1], self._minute_cache[2], self._minute_cache[3]
            # The migration marker certifies old storage, not the current
            # provider preference. Without it only newly written row evidence
            # can certify this exact partition; never create the marker here.
            if not raw and (batch is None or "minute_price_basis" not in pl.read_parquet_schema(partition)):
                self._minute_cache = None
                return pl.DataFrame(), False, "分钟价格基准未确认，暂不计算分时联动"
            frame = self._repo.get_minute_by_dates(sorted(symbols), [cutoff.date()], asset_type="stock")
            reason = None
            if frame.is_empty():
                reason = "缺少当日本地分钟数据"
            elif not {"symbol", "datetime"}.issubset(frame.columns) or not isinstance(frame.schema["datetime"], pl.Datetime):
                frame, reason = pl.DataFrame(), "本地分钟时间或股票字段无效"
            else:
                dt = pl.col("datetime")
                if frame.schema["datetime"].time_zone:
                    dt = dt.dt.convert_time_zone(CN_TZ.key).dt.replace_time_zone(None)
                frame = frame.with_columns(dt.alias("datetime")).filter(
                    pl.col("symbol").is_in(symbols) & (pl.col("datetime").dt.date() == cutoff.date())
                    & (pl.col("datetime") < cutoff)
                )
                if frame.is_empty():
                    reason = "缺少当日已完成分钟数据"
                elif "minute_price_basis" in frame.columns:
                    # Keep mixed symbols intact. The evaluator rejects each
                    # stock whose completed rows contain any unknown/forward
                    # basis, so new tail rows cannot launder unknown history.
                    raw = bool(frame.group_by("symbol").agg(
                        (pl.col("minute_price_basis") == "raw").fill_null(False).all().alias("raw")
                    )["raw"].any())
                    if not raw:
                        frame, reason = pl.DataFrame(), "分钟价格基准缺失或混合，暂不计算分时联动"
                elif raw:
                    # Repository reads project migrated storage to forward
                    # prices. Legacy marker fallback is raw only at ratio=1.
                    pairs = compute_ratio_pairs(frame, self._data_dir)
                    raw = not pairs.is_empty() and bool(pairs.select(
                        ((pl.col("_ratio") - 1).abs() <= 1e-12).fill_null(False).all()
                    ).item())
                    if not raw:
                        frame, reason = pl.DataFrame(), "分钟读取已发生复权投影，暂不按原价计算分时联动"
                else:
                    frame, reason = pl.DataFrame(), "分钟价格基准未确认，暂不计算分时联动"
            self._minute_cache = (key, frame, raw, reason)
            return frame, raw, reason
        except Exception as exc:  # noqa: BLE001 - do not reuse stale data after a failed local read
            logger.warning("first-board attention minute read unavailable (%s)", type(exc).__name__)
            self._minute_cache = None
            return pl.DataFrame(), raw, "当日本地分钟读取失败，暂不计算分时联动"
