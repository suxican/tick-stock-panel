"""开盘啦盘中快照: 合并请求、30 秒缓存、失败沿用旧快照, 并按交易日留档。

留档 data/kaipanla/v1/<date>/:
- observations.jsonl: 连续竞价时段定时采集, 每次新抓取追加一行;
- close.json: 15:05 后首次采集定版, 之后不改写;
- expression.json: 晋级率等 (历史接口当日不可得), 事后补取, 取到后不改写。
开盘啦数据只用于展示, 不进入回测、因子、策略与监控。
"""
from __future__ import annotations

import json
import uuid
from collections import OrderedDict
from concurrent.futures import Future
from datetime import date, datetime, time, timedelta
from pathlib import Path
from threading import Lock

from app.market_time import cn_now, in_continuous_session
from app.plugins.kaipanla.client import KaipanlaClient, KaipanlaError
from app.services.fs_utils import atomic_write_text

TTL_SECONDS = 30
CLOSE_AT = time(15, 5)
_VERSION_DIR = ("kaipanla", "v1")

_lock = Lock()
_write_lock = Lock()
_clients: dict[Path, KaipanlaClient] = {}
_latest: dict[Path, dict] = {}          # 最近一次成功快照
_retry_after: dict[Path, datetime] = {}  # 失败后短暂不再请求
_pending: dict[Path, Future] = {}
_appended: dict[Path, str] = {}          # 最近追加到 observations 的 fetched_at
_status = {"last_success_at": None, "last_error": None}


class KaipanlaUnavailableError(RuntimeError):
    """从未成功取得当日快照。"""


def _reset_for_tests() -> None:
    with _lock:
        _clients.clear(); _latest.clear(); _retry_after.clear(); _pending.clear(); _appended.clear()
        _status.update(last_success_at=None, last_error=None)


def status() -> dict:
    with _lock:
        return dict(_status)


def _root(data_dir: Path) -> Path:
    return Path(data_dir).joinpath(*_VERSION_DIR)


def _day_dir(data_dir: Path, day: str) -> Path:
    return _root(data_dir) / day


def _device_id(data_dir: Path) -> str:
    path = Path(data_dir) / "kaipanla" / "device_id"
    with _write_lock:
        if path.exists():
            value = path.read_text(encoding="utf-8").strip()
            if value:
                return value
        value = uuid.uuid4().hex
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, value)
        return value


def _client(data_dir: Path) -> KaipanlaClient:
    key = Path(data_dir)
    with _lock:
        client = _clients.get(key)
    if client is None:
        client = KaipanlaClient(_device_id(key))
        with _lock:
            client = _clients.setdefault(key, client)
    return client


def _themes(stocks: list[dict]) -> list[dict]:
    groups: OrderedDict[str, dict] = OrderedDict()
    for s in stocks:
        code = s.get("theme_code") or ""
        if not code:
            continue
        g = groups.setdefault(code, {"code": code, "name": s.get("reason") or "", "count": 0, "symbols": []})
        g["count"] += 1
        g["symbols"].append(s["symbol"])
    return sorted(groups.values(), key=lambda g: -g["count"])


def _prev_expression(data_dir: Path, day: str) -> dict | None:
    root = _root(data_dir)
    if not root.exists():
        return None
    for d in sorted((p for p in root.iterdir() if p.is_dir() and p.name < day), key=lambda p: p.name, reverse=True):
        f = d / "expression.json"
        if f.exists():
            return {"date": d.name, **json.loads(f.read_text(encoding="utf-8"))}
    return None


def _fetch(data_dir: Path, client: KaipanlaClient, now: datetime) -> dict:
    """一轮完整抓取。涨停列表是核心, 失败即整轮失败; 其余子接口失败记入 missing。"""
    stocks: list[dict] = []
    day = None
    for pid in range(1, 6):
        d, rows = client.limit_up_stocks(pid)
        day = day or d
        stocks.extend(rows)
    missing: list[str] = []

    def optional(call, keys):
        try:
            return call()
        except KaipanlaError:
            missing.extend(keys)
            return None

    counts = optional(client.limit_counts, ["tiers"])
    broken = optional(client.first_board_broken, ["first_board_broken", "broken_rate"])
    senti = optional(client.sentiment, ["kpl_strong", "sharp_drawdown"])
    totals = optional(client.limit_totals, ["limit_down"])

    limit_up = (totals or {}).get("limit_up")
    if limit_up is None:
        limit_up = (senti or {}).get("limit_up", len(stocks))
    max_boards = (senti or {}).get("max_boards")
    if max_boards is None:
        max_boards = max((s["boards"] for s in stocks), default=0)
    if counts is None:
        counts = [sum(1 for s in stocks if min(s["boards"], 5) == b) for b in range(1, 6)]
    n_broken = len(broken) if broken is not None else None
    denom = (limit_up + n_broken) if n_broken is not None else 0
    return {
        "date": day, "fetched_at": now.isoformat(), "stale_seconds": 0,
        "summary": {
            "kpl_strong": (senti or {}).get("kpl_strong"), "limit_up": limit_up,
            "limit_down": (totals or {}).get("limit_down"), "max_boards": max_boards,
            "sharp_drawdown": (senti or {}).get("sharp_drawdown"),
            "first_board_broken": n_broken, "broken_rate": (n_broken / denom) if denom else None,
        },
        "tiers": [{"boards": b, "count": c} for b, c in zip(range(1, 6), counts)],
        "stocks": stocks,
        "themes": _themes(stocks),
        "prev_expression": _prev_expression(data_dir, day),
        "missing": missing,
    }


def _stale(snap: dict, now: datetime) -> dict:
    age = int((now - datetime.fromisoformat(snap["fetched_at"])).total_seconds())
    return {**snap, "stale_seconds": max(age, 0)}


def read_intraday(data_dir: Path, *, now: datetime | None = None, client: KaipanlaClient | None = None) -> dict:
    """当日快照。30 秒内的调用 (含并发) 共享一轮抓取。"""
    now = now or cn_now()
    key = Path(data_dir)
    with _lock:
        latest = _latest.get(key)
        if latest and (now - datetime.fromisoformat(latest["fetched_at"])).total_seconds() < TTL_SECONDS:
            return latest
        retry = _retry_after.get(key)
        if retry and now < retry:
            if latest:
                return _stale(latest, now)
            raise KaipanlaUnavailableError(_status["last_error"] or "开盘啦暂不可用")
        pending = _pending.get(key)
        if pending is None:
            _pending[key] = task = Future()
    if pending is not None:
        return pending.result()
    try:
        snap = _fetch(key, client or _client(key), now)
    except KaipanlaError as exc:
        with _lock:
            _status["last_error"] = str(exc)
            _retry_after[key] = now + timedelta(seconds=TTL_SECONDS)
            latest = _latest.get(key)
        result = _stale(latest, now) if latest else None
        error = None if latest else KaipanlaUnavailableError(f"开盘啦暂不可用：{exc}")
    else:
        with _lock:
            _latest[key] = snap
            _retry_after.pop(key, None)
            _status["last_success_at"] = snap["fetched_at"]
            _status["last_error"] = None
        result, error = snap, None
    with _lock:
        _pending.pop(key, None)
    if error:
        task.set_exception(error)
        raise error
    task.set_result(result)
    return result


def _archive_view(snap: dict) -> dict:
    return {k: v for k, v in snap.items() if k not in ("stale_seconds", "prev_expression")}


def _append_observation(data_dir: Path, snap: dict) -> None:
    key = Path(data_dir)
    with _write_lock:
        if _appended.get(key) == snap["fetched_at"]:
            return
        path = _day_dir(key, snap["date"]) / "observations.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(_archive_view(snap), ensure_ascii=False) + "\n")
        _appended[key] = snap["fetched_at"]


def _freeze_close(data_dir: Path, snap: dict) -> None:
    path = _day_dir(data_dir, snap["date"]) / "close.json"
    with _write_lock:
        if path.exists():
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, json.dumps(_archive_view(snap), ensure_ascii=False))


def _backfill_expression(data_dir: Path, today: date, client: KaipanlaClient) -> None:
    root = _root(data_dir)
    if not root.exists():
        return
    candidates = sorted((d for d in root.iterdir() if d.is_dir() and d.name < today.isoformat()
                         and (d / "close.json").exists()), key=lambda d: d.name, reverse=True)
    if not candidates or (candidates[0] / "expression.json").exists():
        return
    target = candidates[0]
    try:
        exp = client.expression(date.fromisoformat(target.name))
    except KaipanlaError as exc:
        with _lock:
            _status["last_error"] = str(exc)
        return
    if exp is None:
        return
    with _write_lock:
        path = target / "expression.json"
        if not path.exists():
            atomic_write_text(path, json.dumps(exp, ensure_ascii=False))


def capture(data_dir: Path, now: datetime, *, client: KaipanlaClient | None = None) -> None:
    """定时入口: 补前一交易日晋级率; 连续竞价时段留观测; 15:05 后定版收盘快照。"""
    key = Path(data_dir)
    client = client or _client(key)
    _backfill_expression(key, now.date(), client)
    in_session = in_continuous_session(now)
    closing = now.time() >= CLOSE_AT and not (_day_dir(key, now.date().isoformat()) / "close.json").exists()
    if not in_session and not closing:
        return
    try:
        snap = read_intraday(key, now=now, client=client)
    except KaipanlaUnavailableError:
        return
    if snap["stale_seconds"]:
        return
    if in_session:
        _append_observation(key, snap)
    if closing:
        _freeze_close(key, snap)


def read_day(data_dir: Path, day: date) -> dict | None:
    d = _day_dir(data_dir, day.isoformat())
    close = d / "close.json"
    if not close.exists():
        return None
    snap = json.loads(close.read_text(encoding="utf-8"))
    exp = d / "expression.json"
    snap["expression"] = json.loads(exp.read_text(encoding="utf-8")) if exp.exists() else None
    snap["stale_seconds"] = 0
    return snap
