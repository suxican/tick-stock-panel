"""首板工作台编排: 复用行情/规则/模拟盘, 持久化实验版本和实时证据。

行情线程仅替换一个待处理快照。历史预热、风险检查及写盘由单独工作线程完成;
缺失或陈旧数据关闭买入提示。涨停价观察不等于封单确认或排队成交。
"""
from __future__ import annotations

import copy
import json
import logging
import math
import threading
import time
import uuid
from datetime import date, datetime, timedelta
from datetime import time as day_time
from pathlib import Path
from typing import Literal, Self

import polars as pl
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.market_time import CN_TZ
from app.services.first_board_context import (
    SECTOR_PROXY_NOTE,
    evaluate_sectors,
    load_environment,
    load_sector_members,
)
from app.services.first_board_portfolio import buy_budget, exit_candidates, portfolio_snapshot
from app.services.fs_utils import atomic_write_text
from app.services.index_const import CORE_INDEX_SYMBOLS
from app.strategy import paper
from app.strategy.first_board import (
    FirstBoardRules,
    build_candidates,
    evaluate_candidates,
    required_history_bars,
)

logger = logging.getLogger(__name__)
_LIMITATIONS = [
    "临近涨停提示是可配置的实验规则, 不是课程打板、扫板或排板的逐笔执行。",
    "涨停价观察不能证明封单稳定或能够成交; 当前不模拟封板队列。",
    "只覆盖沪深主板非风险警示股票; 历史名称和行业归属可能采用当前快照。",
    SECTOR_PROXY_NOTE,
]
_MAX_EVENTS_PER_DAY = 50_000
_CONFIG_WRITE_LOCK = threading.RLock()
_EVENT_TYPES = {"buy_candidate", "approaching", "sealed", "broken", "watch", "invalid", "exit_candidate"}


def _now() -> datetime:
    return datetime.now(CN_TZ)


def _beijing(value: datetime) -> datetime:
    return value.astimezone(CN_TZ) if value.tzinfo else value.replace(tzinfo=CN_TZ)


def _quote_age(stamp: str | None, now: datetime) -> float:
    try:
        parsed = datetime.fromisoformat(stamp) if stamp else None
        if parsed is None or parsed.tzinfo is None or parsed.astimezone(CN_TZ).date() != now.date():
            return math.inf
        return (now - parsed).total_seconds()
    except (ValueError, TypeError):
        return math.inf


def _continuous(now: datetime) -> bool:
    within_session = now.weekday() < 5 and (
        day_time(9, 30) <= now.time().replace(tzinfo=None) < day_time(11, 30)
        or day_time(13) <= now.time().replace(tzinfo=None) < day_time(14, 57)
    )
    if not within_session:
        return False
    from app.services import data_integrity

    with data_integrity._CAL_LOCK:
        calendar = data_integrity._CAL[1]
        if calendar and min(calendar) <= now.date() <= max(calendar):
            return now.date() in calendar
    return True


class FirstBoardConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, frozen=True)

    schema_version: Literal[1] = 1
    revision: int = Field(default=0, ge=0)
    updated_at: str | None = None
    enabled: bool = False
    notify: bool = True
    rules: FirstBoardRules = Field(default_factory=FirstBoardRules)
    allowed_market_states: list[Literal["strong", "lean_strong", "range", "lean_weak", "weak"]] = Field(
        default_factory=lambda: ["strong", "lean_strong", "range"])
    require_sector_confirmation: bool = True
    max_quote_age_seconds: int = Field(default=30, ge=5, le=120)
    paper_account_id: str | None = None
    max_positions: int = Field(default=4, ge=1, le=10)
    max_stock_weight: float = Field(default=.25, gt=0, le=1)
    max_sector_weight: float = Field(default=.5, gt=0, le=1)
    total_exposure: float = Field(default=.5, gt=0, le=1)
    exit_time: str = "10:30"
    exit_loss_pct: float = Field(default=.03, gt=0, le=.2)

    @model_validator(mode="after")
    def validate_rules(self) -> Self:
        try:
            parsed = datetime.strptime(self.exit_time, "%H:%M").time()
        except ValueError as exc:
            raise ValueError("退出时间须为有效的 HH:MM") from exc
        if not (day_time(9, 30) <= parsed < day_time(11, 30)
                or day_time(13) <= parsed < day_time(14, 57)):
            raise ValueError("退出时间必须在连续竞价时段")
        if self.max_stock_weight > min(self.max_sector_weight, self.total_exposure):
            raise ValueError("个股仓位上限不能超过行业或总仓位上限")
        if self.paper_account_id:
            paper.validate_account_id(self.paper_account_id)
        if len(set(self.allowed_market_states)) != len(self.allowed_market_states):
            raise ValueError("市场环境不能重复")
        return self


class FirstBoardService:
    def __init__(self, repo, *, history_loader, publish=None, quote_service=None):
        self.repo = repo
        self.root: Path = repo.store.data_dir / "user_data" / "first_board"
        self.config_path = self.root / "config.json"
        self._history_loader = history_loader
        self._publish = publish
        self._quote_service = quote_service
        self._lock = threading.RLock()
        self._pending_lock = threading.Lock()
        self._process_lock = threading.Lock()
        self._research_lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._pending: tuple[pl.DataFrame, date] | None = None
        self._refresh_requested = False
        self._force_refresh_requested = False
        self._last_request = 0.0
        self._prepared_key = None
        self._prepared_at = 0.0
        self._candidates = pl.DataFrame()
        self._sectors: dict[str, str] = {}
        self._environment: dict = {}
        self._latest_prices: dict[str, float] = {}
        self._latest_quote_times: dict[str, str] = {}
        self._state_day: date | None = None
        self._state_signature: tuple[int, int] | None = None
        self._day_state: dict = {}
        self._config, self._versions = self._load_config()
        self._snapshot = self._empty_snapshot()

    def _load_config(self) -> tuple[FirstBoardConfig, list[dict]]:
        if not self.config_path.exists():
            config = FirstBoardConfig()
            return config, [config.model_dump(mode="json")]
        try:
            doc = json.loads(self.config_path.read_text(encoding="utf-8"))
            current = FirstBoardConfig.model_validate(doc["current"])
            versions = [FirstBoardConfig.model_validate(item).model_dump(mode="json")
                        for item in doc["versions"]]
            if (not versions or versions[-1] != current.model_dump(mode="json")
                    or [item["revision"] for item in versions] != list(range(current.revision + 1))):
                raise ValueError("配置历史与当前版本不一致")
            return current, versions
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ValueError("首板配置损坏或版本不受支持, 已停止加载且保留原文件") from exc

    def _empty_snapshot(self) -> dict:
        return {
            "status": "waiting" if self._config.enabled else "disabled",
            "message": "等待有效行情; 可先配置规则或运行历史观察研究。",
            "as_of": None, "observed_at": None, "revision": self._config.revision,
            "rows": [], "environment": {},
            "coverage": {"candidate_count": 0, "symbol_count": 0, "quote_count": 0, "fresh_count": 0},
            "limitations": list(_LIMITATIONS),
            "paper": {"initialized": False, "account_id": self._config.paper_account_id},
        }

    def get_config(self) -> dict:
        with self._lock, _CONFIG_WRITE_LOCK:
            self._sync_config_from_disk()
            return self._config.model_dump(mode="json")

    def _sync_config_from_disk(self) -> None:
        """调用方持有配置锁; 验证完成后才接受其他实例写入的新版本。"""
        config, versions = self._load_config()
        if config.revision < self._config.revision:
            raise ValueError("首板磁盘配置版本已回退或文件缺失, 保留当前内存配置")
        if config.revision == self._config.revision:
            return
        self._config, self._versions = config, versions
        self._prepared_key = None
        self._snapshot = self._empty_snapshot()
        self._latest_prices, self._latest_quote_times = {}, {}

    @property
    def enabled(self) -> bool:
        # 配置只整体替换不可变模型; 行情线程读取引用不等待候选评估和写盘。
        return self._config.enabled

    def _realtime_block_reason(self) -> str | None:
        if self._quote_service is None:
            return None
        try:
            status = self._quote_service.status()
            if not status.get("enabled"):
                return "全局实时行情未启用, 请在实时行情设置中开启后继续盯盘"
            if status.get("realtime_allowed") is False or status.get("mode") == "none":
                return "当前数据源缺少实时行情能力, 买卖提示已暂停"
            if status.get("paused"):
                return "全局实时行情暂时暂停, 请等待行情恢复"
        except Exception:
            logger.warning("首板读取实时行情状态失败", exc_info=True)
            return "无法确认实时行情状态, 买卖提示已暂停"
        return None

    def _assert_config_current(self) -> None:
        disk, _ = self._load_config()
        if disk.revision != self._config.revision:
            raise ValueError("规则版本已变化, 请重新加载后操作")

    def versions(self) -> list[dict]:
        with self._lock, _CONFIG_WRITE_LOCK:
            self._sync_config_from_disk()
            return copy.deepcopy(self._versions[::-1])

    def save_config(self, values: dict) -> dict:
        config = FirstBoardConfig.model_validate(values)
        with self._lock, _CONFIG_WRITE_LOCK:
            if config.revision != self._config.revision:
                raise ValueError("规则版本已变化, 请重新加载后保存")
            self._assert_config_current()
            with paper.PAPER_LOCK:
                if config.paper_account_id and paper.get_account(self.repo.store.data_dir, config.paper_account_id) is None:
                    raise ValueError("关联模拟账户不存在, 请先在模拟盘创建账户")
            config = config.model_copy(update={"revision": config.revision + 1, "updated_at": _now().isoformat()})
            document = config.model_dump(mode="json")
            versions = [*self._versions, document]
            self.root.mkdir(parents=True, exist_ok=True)
            atomic_write_text(self.config_path, json.dumps({"current": document, "versions": versions}, ensure_ascii=False))
            self._config, self._versions = config, versions
            self._prepared_key = None
            self._snapshot = self._empty_snapshot()
            self._latest_prices, self._latest_quote_times = {}, {}
        self.request_refresh(force=True)
        return copy.deepcopy(document)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._worker, name="first-board", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=5)

    def offer_snapshot(self, current: pl.DataFrame, as_of: date | None) -> None:
        """只保留最新快照, 历史计算及持久化不占用行情线程。"""
        if not self.enabled or as_of is None or current.is_empty():
            return
        with self._pending_lock:
            self._pending = (current, as_of)
        self._wake.set()

    def request_refresh(self, *, force: bool = False) -> None:
        with self._pending_lock:
            now = time.monotonic()
            if not force and now - self._last_request < 5:
                return
            self._last_request = now
            self._refresh_requested = True
            if force:
                self._force_refresh_requested = True
        self._wake.set()

    def _worker(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(timeout=10)
            self._wake.clear()
            if self._stop.is_set():
                break
            with self._pending_lock:
                pending, self._pending = self._pending, None
                refresh, self._refresh_requested = self._refresh_requested, False
                force, self._force_refresh_requested = self._force_refresh_requested, False
            if pending is None and not refresh:
                continue
            try:
                if force:
                    self._prepared_key = None
                if pending is None:
                    current, day = self._refresh_snapshot()
                    if day is None or current.is_empty():
                        continue
                    pending = (current, day)
                self.process_snapshot(*pending)
            except Exception:
                logger.exception("首板快照评估失败")
                with self._lock:
                    self._snapshot = {**self._snapshot, "status": "error",
                                      "message": "首板数据评估失败, 买入提示已暂停; 请检查数据后刷新。",
                                      "rows": [{**row, "can_buy": False, "event_id": None}
                                               for row in self._snapshot["rows"]]}
                    self._latest_prices, self._latest_quote_times = {}, {}

    def _refresh_snapshot(self) -> tuple[pl.DataFrame, date | None]:
        if self._quote_service is not None:
            try:
                current, day = self._quote_service.get_enriched_today()
                if day is not None and not current.is_empty():
                    return current, day
            except Exception:
                logger.warning("首板实时快照读取失败, 回退仓库", exc_info=True)
        return self.repo.get_enriched_latest()

    def _prepare(self, as_of: date, config: FirstBoardConfig) -> None:
        key = (as_of, config.revision)
        if self._prepared_key == key and time.monotonic() - self._prepared_at < 300:
            return
        history = self._history_loader(as_of, required_history_bars(config.rules) + 2)
        candidates = build_candidates(history, as_of, config.rules)
        if not candidates.is_empty():
            reference = candidates["reference_date"].max()
        else:
            dates = history.filter(pl.col("date") < as_of)["date"] if "date" in history.columns else []
            reference = max(dates) if len(dates) else None
        environment = load_environment(self.repo.store.data_dir, as_of=as_of,
                                       reference_date=reference, allowed_states=config.allowed_market_states)
        validation = self._validate_history_sessions(history, candidates, as_of, config)
        environment["history_validation"] = validation
        if not validation["allowed"]:
            environment.update(allowed=False, reason=validation["reason"])
        sectors = load_sector_members(self.repo)
        self._candidates, self._environment, self._sectors = candidates, environment, sectors
        self._prepared_key, self._prepared_at = key, time.monotonic()

    def _validate_history_sessions(self, history: pl.DataFrame, candidates: pl.DataFrame,
                                   as_of: date, config: FirstBoardConfig) -> dict:
        """独立指数完成日线校验历史窗口; 只读现有日历缓存, 不在行情路径联网。"""
        result = {"allowed": False, "source": "local_index_daily", "reference_date": None,
                  "reason": "缺少独立指数历史交易日证据, 暂停买入提示"}
        try:
            count = config.rules.lookback_days
            frame = self.repo.get_index_daily(
                CORE_INDEX_SYMBOLS[0], start=as_of - timedelta(days=max(90, count * 3)),
                end=as_of - timedelta(days=1), columns=["date"],
            )
            if frame.is_empty() or "date" not in frame.columns:
                return result
            sessions = sorted({day for day in frame["date"].to_list()
                               if type(day) is date and day < as_of})[-count:]
            if len(sessions) != count:
                return result
            result["reference_date"] = str(sessions[-1])
            # 完整性服务已取到的日历可提供更强证据。这里只读缓存, 禁止触发网络探测。
            from app.services import data_integrity

            with data_integrity._CAL_LOCK:
                cached_calendar = data_integrity._CAL[1]
                calendar = set(cached_calendar) if cached_calendar else set()
            if calendar and min(calendar) <= as_of <= max(calendar):
                result["source"] = "cached_calendar_and_local_index_daily"
                expected = sorted(day for day in calendar if day < as_of)[-count:]
                if as_of not in calendar or sessions != expected:
                    result["reason"] = "本地指数历史与已缓存交易日历不一致, 暂停买入提示"
                    return result
            if candidates.is_empty() or "reference_date" not in candidates.columns:
                result["reason"] = "缺少可验证的候选历史参考日"
                return result
            if set(candidates["reference_date"].to_list()) != {sessions[-1]}:
                result["reason"] = "候选历史参考日与指数最新完成交易日不一致, 请补齐历史数据"
                return result
            if "date" not in history.columns:
                return result
            stock_sessions = {day for day in history["date"].to_list()
                              if type(day) is date and sessions[0] <= day < as_of}
            if stock_sessions != set(sessions):
                result["reason"] = "股票历史交易日窗口缺失或与指数不一致, 请补齐历史数据"
                return result
            result.update(allowed=True, reason="候选历史与独立本地指数交易日窗口一致")
            return result
        except Exception:
            logger.warning("首板历史交易日证据校验失败", exc_info=True)
            return result

    def _load_day(self, as_of: date) -> None:
        path = self.root / "events" / f"{as_of.isoformat()}.json"
        signature = self._file_signature(path)
        if self._state_day == as_of and signature == self._state_signature:
            return
        if path.exists():
            state = self._read_day(path, as_of)
        else:
            state = {"schema_version": 1, "date": str(as_of), "last": {}, "events": [], "candidate_symbols": []}
        self._state_day, self._day_state = as_of, state
        self._state_signature = signature

    @staticmethod
    def _file_signature(path: Path) -> tuple[int, int] | None:
        try:
            stat = path.stat()
            return stat.st_mtime_ns, stat.st_size
        except FileNotFoundError:
            return None

    @staticmethod
    def _read_day(path: Path, as_of: date) -> dict:
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
            if (not isinstance(state, dict) or state.get("schema_version") != 1
                    or state.get("date") != str(as_of) or not isinstance(state.get("events"), list)
                    or not isinstance(state.get("last"), dict) or len(state["events"]) > _MAX_EVENTS_PER_DAY):
                raise ValueError("invalid state")
            json.dumps(state, allow_nan=False)
            identifiers = set()
            for event in state["events"]:
                if (not isinstance(event, dict) or not isinstance(event.get("id"), str) or not event["id"]
                        or event["id"] in identifiers or event.get("date") != str(as_of)
                        or event.get("event_type") not in _EVENT_TYPES
                        or not isinstance(event.get("symbol"), str) or not event["symbol"]
                        or type(event.get("revision")) is not int or event["revision"] < 0
                        or not isinstance(event.get("evidence"), dict)):
                    raise ValueError("invalid event")
                identifiers.add(event["id"])
            for value in state["last"].values():
                if (not isinstance(value, dict) or value.get("state") not in _EVENT_TYPES
                        or (value.get("event_id") is not None and value["event_id"] not in identifiers)):
                    raise ValueError("invalid transition")
            return state
        except (ValueError, OSError, TypeError, KeyError) as exc:
            raise ValueError("首板信号记录损坏或版本不受支持, 停止评估并保留原文件") from exc

    @staticmethod
    def _fresh_quotes(current: pl.DataFrame, now: datetime, config: FirstBoardConfig) -> tuple[pl.DataFrame, dict]:
        if not {"symbol", "date", "quote_ts"}.issubset(current.columns):
            return current.head(0), {}
        timestamps = {}
        counts: dict[str, int] = {}
        for row in current.select("symbol", "date", "quote_ts").iter_rows(named=True):
            counts[row["symbol"]] = counts.get(row["symbol"], 0) + 1
            timestamp = row["quote_ts"]
            if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)) or not math.isfinite(timestamp):
                continue
            age = now.timestamp() - timestamp / 1000
            if row["date"] == now.date() and 0 <= age <= config.max_quote_age_seconds:
                timestamps[row["symbol"]] = datetime.fromtimestamp(timestamp / 1000, CN_TZ).isoformat()
        timestamps = {key: value for key, value in timestamps.items() if counts[key] == 1}
        return current.filter(pl.col("symbol").is_in(list(timestamps))), timestamps

    def process_snapshot(self, current: pl.DataFrame, as_of: date, *, now: datetime | None = None) -> None:
        with self._process_lock:
            with self._lock:
                config = self._config
            self._prepare(as_of, config)
            now = _beijing(now or _now())
            fresh, times = self._fresh_quotes(current, now, config)
            sectors = evaluate_sectors(fresh, self._sectors)
            prices = {row["symbol"]: float(row["raw_close"]) for row in fresh.iter_rows(named=True)
                      if isinstance(row.get("raw_close"), (float, int)) and math.isfinite(row["raw_close"]) and row["raw_close"] > 0}
            realtime_blocked = self._realtime_block_reason()
            live = config.enabled and not realtime_blocked and as_of == now.date() and _continuous(now)
            rows = evaluate_candidates(self._candidates, current, config.rules, as_of=as_of)
            messages: list[dict] = []
            with self._lock:
                if config.revision != self._config.revision:
                    return
                self._load_day(as_of)
                state = copy.deepcopy(self._day_state)
                state["candidate_symbols"] = sorted({row["symbol"] for row in rows})
                for row in rows:
                    symbol = row["symbol"]
                    row.update(quote_time=times.get(symbol), sector=self._sectors.get(symbol), event_id=None,
                               suggested_amount=None, can_buy=False, blocked_reasons=[])
                    blocked = row["blocked_reasons"]
                    if realtime_blocked:
                        blocked.append(realtime_blocked)
                    if not live:
                        blocked.append("自动盯盘已暂停" if not config.enabled else "当前不在有效盘中时段")
                    if symbol not in times:
                        blocked.append("行情时间缺失、重复或已过期")
                    if not self._environment.get("allowed"):
                        blocked.append(self._environment.get("reason") or "市场环境未通过")
                    sector = sectors.get(row["sector"], {})
                    row["sector_confirmation"] = sector
                    row["evidence"]["environment"] = copy.deepcopy(self._environment)
                    row["evidence"]["sector_confirmation"] = copy.deepcopy(sector)
                    if config.require_sector_confirmation and not sector.get("confirmed"):
                        blocked.append(sector.get("reason") or "缺少可确认的行业联动")
                    if row["state"] == "approaching" and not blocked:
                        budget = buy_budget(self.repo.store.data_dir, config.paper_account_id, symbol=symbol,
                                            prices=prices, sectors=self._sectors, config=config.model_dump())
                        row["evidence"]["risk_budget"] = copy.deepcopy(budget)
                        if not budget["allowed"]:
                            blocked.extend(budget["reasons"])
                        row["suggested_amount"] = budget.get("amount")
                    row["can_buy"] = row["state"] == "approaching" and not blocked
                    event_type = "buy_candidate" if row["can_buy"] else row["state"]
                    key = f"{config.revision}:{symbol}:{row['pattern']}"
                    prior = state["last"].get(key)
                    # 首次观察池不刷屏; 重启复用 last。过期行情不推进业务状态。
                    recordable = live and symbol in times
                    if recordable and event_type != (prior or {}).get("state"):
                        state["last"][key] = {"state": event_type, "event_id": None}
                        if event_type != "watch" or prior:
                            event = self._event(row, event_type, config, now)
                            state["events"].append(event)
                            state["last"][key]["event_id"] = event["id"]
                            messages.append(event)
                    latest = state["last"].get(key, {})
                    if row["can_buy"] and latest.get("state") == "buy_candidate":
                        row["event_id"] = latest.get("event_id")
                if live and config.paper_account_id:
                    for item in exit_candidates(self.repo.store.data_dir, config.paper_account_id, prices,
                                                now=now, exit_time=config.exit_time, exit_loss_pct=config.exit_loss_pct):
                        key = f"{config.revision}:exit:{item['symbol']}"
                        if key in state["last"]:
                            continue
                        event = self._event({"symbol": item["symbol"], "name": item["symbol"],
                                             "pattern": "exit", "pattern_label": "次日退出", "state": "exit_candidate",
                                             "price": item["price"], "reasons": [item["reason"]],
                                             "evidence": {"qty": item["qty"], "account_id": config.paper_account_id},
                                             "blocked_reasons": [], "quote_time": times.get(item["symbol"])},
                                            "exit_candidate", config, now)
                        state["events"].append(event)
                        state["last"][key] = {"state": "exit_candidate", "event_id": event["id"]}
                        messages.append(event)
                if len(state["events"]) > _MAX_EVENTS_PER_DAY:
                    raise ValueError("首板当日事件超过保护上限, 停止提示")
                if messages:
                    directory = self.root / "events"
                    directory.mkdir(parents=True, exist_ok=True)
                    atomic_write_text(directory / f"{as_of}.json", json.dumps(state, ensure_ascii=False, allow_nan=False))
                    self._state_signature = self._file_signature(directory / f"{as_of}.json")
                self._day_state = state
                self._latest_prices = prices
                self._latest_quote_times = times
                status = ("disabled" if not config.enabled else "closed" if not _continuous(now)
                          else "waiting" if realtime_blocked
                          else "stale" if as_of != now.date() or not times else "ready")
                message = {"disabled": "自动盯盘已暂停, 当前结果仅供观察。",
                           "closed": "当前为非交易时段, 显示最近行情观察结果, 不触发买卖提示。",
                           "stale": "缺少当日新鲜行情, 买入提示已暂停。",
                           "waiting": realtime_blocked,
                           "ready": "自动跟踪当前候选; 临近涨停提示需自行确认, 涨停价不代表可成交。"}[status]
                self._snapshot = {
                    "status": status, "message": message, "as_of": str(as_of), "observed_at": now.isoformat(),
                    "revision": config.revision,
                    "rows": sorted(rows, key=lambda row: (not row["can_buy"], row["symbol"], row["pattern"])),
                    "environment": dict(self._environment),
                    "coverage": {"candidate_count": len(rows), "symbol_count": len(state["candidate_symbols"]),
                                 "quote_count": current.height, "fresh_count": fresh.height},
                    "limitations": list(_LIMITATIONS),
                    "paper": portfolio_snapshot(self.repo.store.data_dir, config.paper_account_id, prices),
                }
            if messages and config.notify and self._publish:
                # 只发送状态变化; 日内研究证据已先持久化, 不依赖通知成功。
                publish_now = _beijing(_now())
                current_config = self._config
                if (current_config.enabled and current_config.revision == config.revision
                        and _continuous(publish_now) and not self._realtime_block_reason()):
                    fresh_messages = [event for event in messages
                                      if 0 <= _quote_age(event.get("quote_time"), publish_now)
                                      <= config.max_quote_age_seconds]
                    if fresh_messages:
                        self._publish(fresh_messages)

    @staticmethod
    def _event(row: dict, event_type: str, config: FirstBoardConfig, now: datetime) -> dict:
        label = {"buy_candidate": "临近涨停买入候选", "approaching": "临近涨停但条件未通过",
                 "sealed": "涨停价观察", "broken": "开板观察", "watch": "条件失效",
                 "invalid": "数据或条件失效", "exit_candidate": "次日退出提示"}.get(event_type, event_type)
        reasons = [*row.get("reasons", []), *row.get("blocked_reasons", [])]
        return {
            "id": uuid.uuid4().hex, "ts": int(now.timestamp() * 1000), "date": now.date().isoformat(),
            "source": "first_board", "type": event_type, "event_type": event_type,
            "symbol": row["symbol"], "name": row["name"], "pattern": row["pattern"],
            "pattern_label": row["pattern_label"], "state": row["state"], "price": row.get("price"),
            "change_pct": row.get("change_pct"), "revision": config.revision,
            "rules": config.rules.model_dump(mode="json"), "evidence": copy.deepcopy(row.get("evidence", {})),
            "blocked_reasons": row.get("blocked_reasons", []), "quote_time": row.get("quote_time"),
            "signals": [], "severity": "warn" if event_type == "exit_candidate" else "info",
            "message": f"首板·{row['pattern_label']} {row['name']}: {label}。{'; '.join(reasons)}",
        }

    def snapshot(self) -> dict:
        with self._lock:
            result = copy.deepcopy(self._snapshot)
            config = self._config
            quote_times = dict(self._latest_quote_times)
        # UI 停留也不能让上次有效买点无限期有效。
        now = _beijing(_now())
        realtime_blocked = self._realtime_block_reason()
        continuous = _continuous(now)
        fresh_count = sum(0 <= _quote_age(stamp, now) <= config.max_quote_age_seconds
                          for stamp in quote_times.values())
        same_day = result.get("as_of") == str(now.date())
        usable = config.enabled and continuous and not realtime_blocked and same_day
        result["coverage"]["fresh_count"] = fresh_count if usable else 0
        if not config.enabled:
            result.update(status="disabled", message="自动盯盘已暂停, 当前结果仅供观察。")
        elif result["status"] == "error":
            result["coverage"]["fresh_count"] = 0
        elif not continuous:
            result.update(status="closed", message="当前为非交易时段, 显示最近行情观察结果, 不触发买卖提示。")
        elif realtime_blocked:
            result.update(status="waiting", message=realtime_blocked)
        elif result.get("as_of") is not None and (not same_day or not fresh_count):
            result.update(status="stale", message="缺少当日新鲜行情, 买入提示已暂停。")
        for row in result["rows"]:
            age = _quote_age(row.get("quote_time"), now)
            if not usable or not 0 <= age <= config.max_quote_age_seconds or result["status"] == "error":
                row["can_buy"], row["event_id"] = False, None
                row["suggested_amount"] = None
                row["blocked_reasons"].append(realtime_blocked or "行情已过期或当前不在连续竞价时段")
        return result

    def events(self, day: date | None = None) -> list[dict]:
        day = day or _now().date()
        with self._lock:
            if self._state_day == day:
                self._load_day(day)
                return copy.deepcopy(self._day_state["events"][::-1])
            path = self.root / "events" / f"{day.isoformat()}.json"
            if not path.exists():
                return []
            doc = self._read_day(path, day)
            return doc["events"][::-1]

    def place_order(self, event_id: str, side: str, *, amount: float | None = None,
                    qty: int | None = None, now: datetime | None = None) -> dict:
        now = _beijing(now or _now())
        if side not in {"buy", "sell"}:
            raise ValueError("买卖方向无效")
        if qty is not None and (type(qty) is not int or qty <= 0 or qty % paper.LOT_SIZE):
            raise ValueError("模拟数量必须为正的百股整数倍")
        if amount is not None and (isinstance(amount, bool) or not isinstance(amount, (int, float))
                                   or not math.isfinite(amount) or amount <= 0):
            raise ValueError("模拟金额必须是有效正数")
        if (qty is not None and amount is not None) or (side == "sell" and amount is not None):
            raise ValueError("数量与金额只能提供一个, 卖出仅支持数量")
        with self._lock, _CONFIG_WRITE_LOCK, paper.PAPER_LOCK:
            config = self._config
            self._assert_config_current()
            event = next((item for item in self.events(now.date()) if item["id"] == event_id), None)
            if not event or event["revision"] != config.revision:
                raise ValueError("信号已失效或版本已变化")
            if not config.enabled or not _continuous(now) or not config.paper_account_id:
                raise ValueError("请在盘中启用盯盘并关联模拟账户")
            realtime_blocked = self._realtime_block_reason()
            if realtime_blocked:
                raise ValueError(realtime_blocked)
            account = paper.get_account(self.repo.store.data_dir, config.paper_account_id)
            if account is None:
                raise ValueError("关联模拟账户不存在")
            if account.get("status") != "active":
                raise ValueError("关联模拟账户已冻结")
            if account.get("queue_limit_orders"):
                raise ValueError("首板模拟需关闭账户的次日排队选项, 避免改变信号交易日")
            # 只接受后台最近计算且依然新鲜的当日证据, 不让前端提供价格。
            symbol = event["symbol"]
            row = next((item for item in self._snapshot["rows"] if item["symbol"] == symbol
                        and item.get("event_id") == event_id), None)
            fresh_prices = {item: value for item, value in self._latest_prices.items()
                            if 0 <= _quote_age(self._latest_quote_times.get(item), now) <= config.max_quote_age_seconds}
            if symbol not in fresh_prices:
                raise ValueError("行情已过期, 请等待新的有效信号")
            orders = paper.load_orders(self.repo.store.data_dir, config.paper_account_id)
            source = f"first_board:{event_id}"
            if any(order.get("source") == source and order["side"] == side for order in orders):
                raise ValueError("该信号已提交模拟订单, 请在模拟盘查看")
            price = fresh_prices.get(symbol)
            if price is None or not math.isfinite(price) or price <= 0:
                raise ValueError("缺少新鲜成交参考价")
            if side == "buy":
                if event["event_type"] != "buy_candidate" or row is None or not row["can_buy"]:
                    raise ValueError("该记录不是当前有效买入候选")
                budget = buy_budget(self.repo.store.data_dir, config.paper_account_id, symbol=symbol,
                                    prices=fresh_prices, sectors=self._sectors, config=config.model_dump())
                if not budget["allowed"] or not budget.get("qty"):
                    raise ValueError("; ".join(budget["reasons"]))
                requested = qty if qty is not None else paper.qty_from_amount(amount, price) if amount is not None else budget["qty"]
                if requested <= 0 or requested > budget["qty"]:
                    raise ValueError("模拟买入数量超过当前风险预算或不足一手")
            else:
                if event["event_type"] != "exit_candidate":
                    raise ValueError("该记录不是退出提示")
                current_exit = next((item for item in exit_candidates(
                    self.repo.store.data_dir, config.paper_account_id, fresh_prices, now=now,
                    exit_time=config.exit_time, exit_loss_pct=config.exit_loss_pct,
                ) if item["symbol"] == symbol), None)
                if current_exit is None:
                    raise ValueError("该提示已不满足当前退出条件或没有剩余可卖数量")
                available = paper.normalize_qty(int(current_exit["qty"]))
                requested = qty if qty is not None else available
                if requested <= 0 or requested > available:
                    raise ValueError("模拟卖出数量超过当前剩余可卖数量或不足一手")
            order, error = paper.create_order(self.repo.store.data_dir, symbol, side,
                                              account_id=config.paper_account_id, qty=requested,
                                              ref_price=price, source=source, asset_type="stock")
            if error:
                raise ValueError(error)
            return order

    def research(self, *, start: date, end: date, rules: FirstBoardRules | dict | None = None, compare: bool = False) -> dict:
        from app.backtest.first_board import compare_first_board_rules, run_first_board_research

        if not self._research_lock.acquire(blocking=False):
            raise ValueError("首板研究正在运行, 请等待当前任务完成")
        try:
            if start > end:
                raise ValueError("研究开始日期不能晚于结束日期")
            with self._lock:
                selected_rules = self._config.rules if rules is None else FirstBoardRules.model_validate(rules)
            func = compare_first_board_rules if compare else run_first_board_research
            result = func(self.repo, start=start, end=end, rules=selected_rules)
            run = {"id": uuid.uuid4().hex, "created_at": _now().isoformat(),
                   "kind": "compare" if compare else "research", "result": result}
            directory = self.root / "research"
            directory.mkdir(parents=True, exist_ok=True)
            atomic_write_text(directory / f"{run['id']}.json", json.dumps(run, ensure_ascii=False, allow_nan=False))
            return result
        finally:
            self._research_lock.release()

    def research_runs(self) -> list[dict]:
        directory = self.root / "research"
        if not directory.exists():
            return []
        paths = sorted(directory.glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True)[:10]
        try:
            runs = [json.loads(path.read_text(encoding="utf-8")) for path in paths]
            json.dumps(runs, allow_nan=False)
            if any(not isinstance(run, dict) or not isinstance(run.get("result"), dict)
                   or run.get("kind") not in {"research", "compare"} for run in runs):
                raise ValueError("invalid research")
            return runs
        except (ValueError, OSError, TypeError) as exc:
            raise ValueError("首板研究记录损坏, 已保留原文件") from exc
