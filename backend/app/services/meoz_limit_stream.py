"""猫爪异动订阅 (stream_limit_event_v2) 的单连接接入: 盘中涨跌停事件 → 内存 + 按日留档。

- 交易日 09:20–15:05 保持一个 WebSocket 连接 (账号同时最多 3 个连接, 本服务只占 1 个);
  票据每次连接重取, 断线按 2→60 秒退避重连, 票据与连接地址视同密钥, 不写日志。
- 推送字段顺序取自订阅确认消息, 不写死; `seq` 不连续记一次缺口 (事件可能漏收, 调用方
  应把本流当作"尽力而为", 并以涨停池轮询兜底)。
- 事件写入 data/meoz_limit_events/v1/date=<日期>.jsonl (追加), 重启后当日事件从留档恢复。
- 首次触板时间: 冲击涨停(1)、封死涨停(2)、涨停回封(3) 中最早的交易所事件时间。注意事件 2/3
  要求封单额与持续 60 秒, 晚于真实触板; 只有事件 1 才是触板时刻, 两者以 source 区分。
本模块只提供事实, 不参与任何交易规则判断。
"""
from __future__ import annotations

import contextlib
import json
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path

from app.data_providers import custom as custom_sources
from app.market_time import CN_TZ, cn_now
from app.plugins.meoz.client import MeozAuthError

logger = logging.getLogger(__name__)

ROOT = ("meoz_limit_events", "v1")
OPEN_AT, CLOSE_AT = time(9, 20), time(15, 5)
UP_EVENTS = {1: "touch", 2: "sealed", 3: "resealed"}  # 涨停方向的"进入涨停"类事件
_TICKET_BODY = {"channel": "tick_stream", "stream_version": "stream_limit_event_v2", "ttl_seconds": 600,
                "symbols": [], "event_types": list(range(1, 13))}
_KEEP = ("symbol", "name", "time", "event", "servertime", "last_price", "limit_price", "amount",
         "fd_amount", "trade_amount_3s", "fd_decrease_12s", "limit_times", "open_times")


@dataclass(frozen=True)
class Touch:
    at: datetime
    source: str  # touch=冲击涨停事件(触板时刻) / sealed、resealed=封死/回封事件(晚于触板)


def parse_push(fields: list[str], message: dict, recv_ms: int) -> list[dict]:
    """一条推送 → 事件字典列表 (按确认消息给出的字段顺序解析, 非法行跳过)。"""
    out = []
    for row in message.get("i") or []:
        if not isinstance(row, list) or len(row) != len(fields):
            continue
        item = dict(zip(fields, row, strict=True))
        if not isinstance(item.get("symbol"), str) or not isinstance(item.get("time"), (int, float)) \
                or not isinstance(item.get("event"), int):
            continue
        out.append({**{k: item.get(k) for k in _KEEP}, "recv_ms": recv_ms})
    return out


class LimitStream:
    def __init__(self, data_dir: Path, *, enabled: Callable[[], bool] = lambda: True,
                 now: Callable[[], datetime] = cn_now,
                 is_trading_day: Callable[[datetime], bool | None] | None = None,
                 ticket: Callable[[dict], dict] | None = None,
                 connect: Callable[[str], object] | None = None,
                 sleep: Callable[[float], bool] | None = None) -> None:
        self._dir = Path(data_dir).joinpath(*ROOT)
        self._enabled, self._now = enabled, now
        self._is_trading_day = is_trading_day or _default_trading_day
        self._ticket = ticket or _default_ticket
        self._connect = connect or _default_connect
        self._stop = threading.Event()
        self._wait = sleep or (lambda seconds: self._stop.wait(seconds))
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._day: date | None = None
        self._events: list[dict] = []
        self._touch: dict[str, Touch] = {}
        self._status = {"state": "idle", "connected_at": None, "last_message_at": None, "reconnects": 0,
                        "seq_gaps": 0, "last_error": None}

    # ---- 事实读取 ----
    def touch_times(self, day: date | None = None) -> dict[str, Touch]:
        with self._lock:
            return dict(self._touch) if (day or self._day) == self._day else {}

    def events(self, symbol: str | None = None, day: date | None = None) -> list[dict]:
        with self._lock:
            if (day or self._day) != self._day:
                return []
            return [e for e in self._events if symbol is None or e["symbol"] == symbol]

    def status(self) -> dict:
        with self._lock:
            return {**self._status, "enabled": self._enabled(), "events_today": len(self._events),
                    "touch_symbols": len(self._touch), "date": self._day.isoformat() if self._day else None}

    # ---- 生命周期 ----
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="meoz-limit-stream", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _in_window(self) -> bool:
        now = self._now()
        return self._is_trading_day(now) is True and OPEN_AT <= now.timetz().replace(tzinfo=None) <= CLOSE_AT

    def _set(self, **values) -> None:
        with self._lock:
            self._status.update(values)

    def _run(self) -> None:
        backoff = 2.0
        while not self._stop.is_set():
            if not self._enabled() or not self._in_window():
                self._set(state="idle" if self._enabled() else "disabled")
                if self._wait(30):
                    return
                continue
            try:
                self._session()
                backoff = 2.0
            except MeozAuthError as exc:
                self._set(state="error", last_error=f"无权限或 Key 无效: {exc}"[:160])
                if self._wait(300):
                    return
            except ValueError as exc:  # 猫爪插件未加载
                self._set(state="error", last_error=str(exc)[:160])
                if self._wait(60):
                    return
            except Exception as exc:  # noqa: BLE001 -- 断线、票据失败等: 退避重连; 异常文本可能含连接地址, 只记类型
                self._set(state="error", last_error=f"{type(exc).__name__}", reconnects=self._status["reconnects"] + 1)
                logger.info("猫爪异动订阅断开: %s", type(exc).__name__)
                if self._wait(backoff):
                    return
                backoff = min(backoff * 2, 60.0)

    def _session(self) -> None:
        self._set(state="connecting")
        url = self._ticket(_TICKET_BODY)["url"]
        fields: list[str] = []
        last_seq: int | None = None
        ws = self._connect(url)
        try:
            self._set(state="connected", connected_at=self._now().isoformat(timespec="seconds"), last_error=None)
            while not self._stop.is_set() and self._enabled() and self._in_window():
                try:
                    raw = ws.recv(timeout=30)
                except TimeoutError:
                    continue
                try:
                    message = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                if not isinstance(message, dict):
                    continue
                if message.get("type") == "ack":
                    if message.get("op") == "sub" and isinstance(message.get("fields"), list):
                        fields = message["fields"]
                    continue
                if message.get("t") != "stream_limit_event_v2" or not fields:
                    continue
                seq = message.get("seq")
                if isinstance(seq, int):
                    if last_seq is not None and seq != last_seq + 1:
                        with self._lock:
                            self._status["seq_gaps"] += 1
                    last_seq = seq
                now = self._now()
                self._ingest(parse_push(fields, message, int(now.timestamp() * 1000)), now)
        finally:
            with contextlib.suppress(Exception):  # 关闭失败不影响重连
                ws.close()
            self._set(state="idle")

    # ---- 落盘与内存 ----
    def _path(self, day: date) -> Path:
        return self._dir / f"date={day.isoformat()}.jsonl"

    def _roll(self, day: date) -> None:
        """调用方持锁。换日清空; 首次进入某日从留档恢复。"""
        if self._day == day:
            return
        self._day, self._events, self._touch = day, [], {}
        path = self._path(day)
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    self._add(json.loads(line))
                except (ValueError, TypeError, KeyError):
                    continue

    def _add(self, event: dict) -> None:
        self._events.append(event)
        kind = UP_EVENTS.get(event["event"])
        if kind is None:
            return
        at = datetime.fromtimestamp(event["time"] / 1000, CN_TZ)
        old = self._touch.get(event["symbol"])
        # 取最早时刻; 同一时刻以触板事件为准
        if old is None or at < old.at or (at == old.at and kind == "touch"):
            self._touch[event["symbol"]] = Touch(at, kind)

    def _ingest(self, events: list[dict], now: datetime) -> None:
        if not events:
            return
        with self._lock:
            self._roll(now.date())
            for event in events:
                self._add(event)
            self._status["last_message_at"] = now.isoformat(timespec="seconds")
            path = self._path(now.date())
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.writelines(json.dumps(e, ensure_ascii=False) + "\n" for e in events)


def _default_trading_day(now: datetime) -> bool | None:
    from app.services.trading_day import is_trading_day

    return is_trading_day(now)


def _default_ticket(body: dict) -> dict:
    return custom_sources.get_provider("meoz").ws_ticket(body)


def _default_connect(url: str):
    from websockets.sync.client import connect

    return connect(url, open_timeout=15, ping_interval=20)


_stream: LimitStream | None = None


def get_stream(data_dir: Path) -> LimitStream:
    global _stream
    if _stream is None:
        from app.services import preferences

        _stream = LimitStream(data_dir, enabled=lambda: bool(preferences.load().get("meoz_limit_stream_enabled", True)))
    return _stream


def reset_for_tests() -> None:
    global _stream
    _stream = None

