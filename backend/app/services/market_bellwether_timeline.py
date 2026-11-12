"""盘中风向标状态时间线 —— 后台按行情更新记录风向标的状态变化。

页面轮询只能看到某一刻, 两次刷新之间的炸板、回封会漏掉, 页面没打开时更是
一无所知。BellwetherTracker 订阅行情更新通知(与 SSE 同一入口, 不改行情线程),
在连续竞价时段最多每 5 秒取一次三个范围(大盘/持仓/全部自选)的风向标快照,
与页面共用 market_bellwether 的短缓存, 只记录状态和结论的变化。

时间线按交易日保存为 user_data/bellwether_timeline/<日期>.json, 盘中重启后读回
继续记录, 只保留最近 KEEP_DAYS 天。展示时由 annotate 附加摘要, 不改缓存中的快照。
"""
from __future__ import annotations

import copy
import json
import logging
import re
import threading
from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import Path

from app.market_time import CN_TZ, cn_now, in_continuous_session
from app.services.fs_utils import atomic_write_text

logger = logging.getLogger(__name__)

TIMELINE_DIR = Path("user_data") / "bellwether_timeline"
FILE_VERSION = 1
KEEP_DAYS = 5
TICK_SECONDS = 5
TREND_MINUTES = 15
MAX_EVENTS = 60               # 单只股票/板块当日最多保留的变化条数
SCOPES = ("market", "holdings", "watchlist")
_DAY_FILE = re.compile(r"^\d{4}-\d{2}-\d{2}\.json$")
# 状态对应的强弱档位, 用于判断弱转强/强转弱
_LEVEL = {"limit_down": 0, "weak": 0, "broken": 0, "flat": 1, "strong": 2, "sealed": 2}


def _clock(moment: datetime) -> str:
    return moment.astimezone(CN_TZ).strftime("%H:%M:%S")


def _empty(day: date) -> dict:
    return {"version": FILE_VERSION, "date": day.isoformat(), "stocks": {}, "sectors": {}}


def _observations(snapshot: dict) -> tuple[dict[str, tuple[str, str]], dict[str, tuple[str, str]]]:
    """快照 → ({股票: (名称, 状态)}, {板块键: (名称, 结论)})。"""
    stocks: dict[str, tuple[str, str]] = {}
    sectors: dict[str, tuple[str, str]] = {}

    def stock(entry: dict) -> None:
        if entry.get("symbol") and entry.get("state"):
            stocks[entry["symbol"]] = (entry.get("name") or entry["symbol"], entry["state"])

    if snapshot.get("scope") == "market":
        for item in snapshot.get("items") or []:
            stock(item)
        if snapshot.get("verdict", {}).get("key"):
            sectors["market"] = ("大盘", snapshot["verdict"]["key"])
    for sector in snapshot.get("sectors") or []:
        for entry in [*(sector.get("items") or []), *(sector.get("mine") or [])]:
            stock(entry)
        if sector.get("verdict", {}).get("key"):
            sectors[f"{sector['kind']}:{sector['name']}"] = (sector["name"], sector["verdict"]["key"])
    return stocks, sectors


class BellwetherTimeline:
    """当日状态时间线; 线程安全, 只在状态变化时写盘。"""

    def __init__(self, data_dir: Path | None) -> None:
        self._folder = Path(data_dir) / TIMELINE_DIR if data_dir is not None else None
        self._lock = threading.Lock()
        self._day: date | None = None
        self._data: dict = {}

    # ── 读写 ────────────────────────────────────────────
    def _path(self, day: date) -> Path | None:
        return self._folder / f"{day.isoformat()}.json" if self._folder else None

    def _load(self, day: date) -> None:
        self._day, self._data = day, _empty(day)
        path = self._path(day)
        if path is None or not path.exists():
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if raw.get("version") == FILE_VERSION and raw.get("date") == day.isoformat():
                self._data = {**_empty(day), "stocks": dict(raw.get("stocks") or {}),
                              "sectors": dict(raw.get("sectors") or {})}
        except (OSError, ValueError, AttributeError):
            logger.warning("风向标时间线文件损坏, 当日从现在起重新记录: %s", path.name)

    def _save(self) -> None:
        path = self._path(self._day)
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(path, json.dumps(self._data, ensure_ascii=False))
            days = sorted(p for p in path.parent.iterdir() if p.is_file() and _DAY_FILE.match(p.name))
            for old in days[:-KEEP_DAYS]:
                old.unlink(missing_ok=True)
        except OSError:
            logger.warning("风向标时间线写盘失败, 内存记录继续", exc_info=True)

    # ── 记录 ────────────────────────────────────────────
    @staticmethod
    def _record(bucket: dict, key: str, name: str, value: str, stamp: str) -> bool:
        if value == "unknown":
            return False
        entry = bucket.setdefault(key, {"name": name, "state": None, "events": []})
        entry["name"] = name
        if entry["state"] == value:
            return False
        entry["events"] = [*entry["events"], {"t": stamp, "from": entry["state"], "to": value}][-MAX_EVENTS:]
        entry["state"] = value
        return True

    def observe(self, now: datetime, snapshots: list[dict]) -> bool:
        """把本轮快照中的状态与结论并入时间线; 只接受当天、盘中、未过期的快照。"""
        day = now.astimezone(CN_TZ).date()
        usable = [snap for snap in snapshots
                  if snap and snap.get("status") == "ready" and snap.get("as_of") == day.isoformat()
                  and snap.get("live") and not snap.get("stale")]
        if not usable:
            return False
        stamp = _clock(now)
        changed = False
        with self._lock:
            if self._day != day:
                self._load(day)
            for snap in usable:
                stocks, sectors = _observations(snap)
                for symbol, (name, state) in stocks.items():
                    changed |= self._record(self._data["stocks"], symbol, name, state, stamp)
                for key, (name, value) in sectors.items():
                    changed |= self._record(self._data["sectors"], key, name, value, stamp)
            if changed:
                self._save()
        return changed

    # ── 摘要 ────────────────────────────────────────────
    def _events(self, bucket: str, key: str, day: date) -> list[dict] | None:
        if self._day != day:
            self._load(day)
        entry = self._data[bucket].get(key)
        return list(entry["events"]) if entry else None

    def stock_summary(self, symbol: str, now: datetime) -> dict | None:
        with self._lock:
            events = self._events("stocks", symbol, now.astimezone(CN_TZ).date())
        if not events:
            return None
        cutoff = _clock(now - timedelta(minutes=TREND_MINUTES))
        before = [event for event in events if event["t"] <= cutoff]
        then = before[-1]["to"] if before else None
        current = events[-1]["to"]
        trend = None
        if then is not None and _LEVEL.get(current) is not None and _LEVEL.get(then) is not None:
            trend = ("up" if _LEVEL[current] > _LEVEL[then] else
                     "down" if _LEVEL[current] < _LEVEL[then] else None)
        return {
            "first_sealed_at": next((event["t"] for event in events if event["to"] == "sealed"), None),
            "broken_count": sum(event["from"] == "sealed" and event["to"] == "broken" for event in events),
            "resealed_count": sum(event["from"] == "broken" and event["to"] == "sealed" for event in events),
            "trend": trend,
            "events": events[-12:],
        }

    def last_change(self, key: str, now: datetime) -> dict | None:
        """板块(或大盘)结论最近一次变化; 开盘首个结论不算变化。"""
        with self._lock:
            events = self._events("sectors", key, now.astimezone(CN_TZ).date())
        changes = [event for event in events or [] if event["from"] is not None]
        return changes[-1] if changes else None

    def annotate(self, snapshot: dict, now: datetime | None = None) -> dict:
        """返回附加了时间线摘要的副本; 不是今天的快照原样返回。"""
        now = now or cn_now()
        if snapshot.get("status") != "ready" or snapshot.get("as_of") != now.astimezone(CN_TZ).date().isoformat():
            return snapshot
        out = copy.deepcopy(snapshot)
        if out.get("scope") == "market":
            for item in out.get("items") or []:
                item["timeline"] = self.stock_summary(item["symbol"], now)
            out["verdict_change"] = self.last_change("market", now)
        for sector in out.get("sectors") or []:
            for entry in [*(sector.get("items") or []), *(sector.get("mine") or [])]:
                entry["timeline"] = self.stock_summary(entry["symbol"], now)
            sector["verdict_change"] = self.last_change(f"{sector['kind']}:{sector['name']}", now)
        return out


class BellwetherTracker:
    """订阅行情更新, 在连续竞价时段节流地记录风向标状态。"""

    def __init__(self, quote_service, timeline: BellwetherTimeline, *,
                 snapshot_fn: Callable[[str], dict]) -> None:
        self._quote_service = quote_service
        self.timeline = timeline
        self._snapshot_fn = snapshot_fn
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_tick: datetime | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="bellwether-timeline", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._thread = None

    def _run(self) -> None:
        subscriber = self._quote_service.subscribe()
        try:
            while not self._stop.is_set():
                subscriber.wait(timeout=TICK_SECONDS)
                subscriber.pop()
                if self._stop.is_set():
                    break
                try:
                    self.tick(cn_now())
                except Exception:
                    # 单轮失败不能让记录器退出
                    logger.exception("风向标时间线记录失败, 下一轮继续")
                # 行情通知可能每秒多次, 节流由 tick 负责; 这里避免空转
                self._stop.wait(1)
        finally:
            self._quote_service.unsubscribe(subscriber)

    def tick(self, now: datetime) -> bool:
        """处理一轮; 返回是否实际取了快照。非连续竞价时段或 5 秒内重复调用直接跳过。"""
        if not in_continuous_session(now):
            return False
        if self._last_tick is not None and (now - self._last_tick).total_seconds() < TICK_SECONDS:
            return False
        self._last_tick = now
        snapshots = []
        for scope in SCOPES:
            try:
                snapshots.append(self._snapshot_fn(scope))
            except Exception:  # noqa: BLE001 - 某一范围失败不影响其他范围
                logger.warning("风向标时间线: %s 快照失败", scope, exc_info=True)
        self.timeline.observe(now, snapshots)
        return True


def create_tracker(repo, quote_service, depth_service) -> BellwetherTracker:
    """应用启动时创建; 与接口共用 market_bellwether 的快照缓存。"""
    from app.services import market_bellwether

    def snapshot_fn(scope: str) -> dict:
        return market_bellwether.get_snapshot(repo, scope=scope, quote_service=quote_service,
                                              depth_service=depth_service)

    return BellwetherTracker(quote_service, BellwetherTimeline(repo.store.data_dir), snapshot_fn=snapshot_fn)
