"""官网首页题材的本地观察记录和正式收盘榜。历史盘中查询始终只读已采集记录。"""
from __future__ import annotations

import json
from collections import OrderedDict
from concurrent.futures import Future
from datetime import date, datetime, time, timedelta
from pathlib import Path
from threading import Lock
from time import monotonic

from app.data_providers import custom as custom_sources
from app.market_time import CN_TZ, cn_now
from app.services.fs_utils import atomic_write_text
from app.services.market_game_calendar import load_game_calendar

_HISTORY = 10
_lock = Lock()
_write_lock = Lock()
_cache: OrderedDict[tuple, tuple] = OrderedDict()
_pending: dict[tuple, Future] = {}
_rate_limit_until: dict[Path, float] = {}


class HomeUnavailableError(RuntimeError):
    pass


def _shared(key, ttl, load):
    """合并同一请求（含其写盘）；失败短缓存，等待线程不持锁。"""
    with _lock:
        hit = _cache.get(key)
        if hit and hit[0] > monotonic():
            _cache.move_to_end(key)
            return hit[1].result()
        pending = _pending.get(key)
        if pending is None:
            _pending[key] = task = Future()
    if pending is not None:
        return pending.result()
    try:
        task.set_result(load())
    except Exception as exc:  # noqa: BLE001 -- wake all waiters on storage and provider failures
        ttl = 20
        task.set_exception(exc)
    with _lock:
        _pending.pop(key)
        _cache[key] = monotonic() + ttl, task
        _cache.move_to_end(key)
        if len(_cache) > 128:
            _cache.popitem(last=False)
    return task.result()


def _remote(root: Path, method: str, *args):
    with _lock:
        if monotonic() < _rate_limit_until.get(root, 0):
            raise HomeUnavailableError("官网请求受限，请稍后刷新。")
    try:
        return getattr(custom_sources.get_provider("zzshare"), method)(*args)
    except (RuntimeError, ValueError, AttributeError) as exc:
        if getattr(exc, "status_code", None) == 429:
            with _lock:
                _rate_limit_until[root] = monotonic() + 60
            raise HomeUnavailableError("官网请求受限，请稍后刷新。") from exc
        raise HomeUnavailableError("官网榜单暂不可用，请检查数据源后重试。") from exc


def _stamp(value) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.astimezone(CN_TZ) if parsed.utcoffset() is not None else None


def _directory(root, day):
    return root / "zzshare_home" / "v1" / day.isoformat()


def _read(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except (ValueError, OSError) as exc:
        raise HomeUnavailableError("本地官网榜单记录无法读取，原文件已保留。") from exc


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(value, ensure_ascii=False, allow_nan=False))


def _packet(raw, day, observed, mode):
    if not isinstance(raw, dict):
        raise HomeUnavailableError("官网榜单格式异常。")
    if raw.get("date") != day.isoformat():
        return None
    items = raw.get("items")
    if (not isinstance(items, list) or len(items) > 3
            or any(not isinstance(r, dict) or not isinstance(r.get("tid"), str) or not r["tid"] for r in items)
            or len({r["tid"] for r in items}) != len(items)):
        raise HomeUnavailableError("官网榜单席位或题材标识异常。")
    source_at = _stamp(raw.get("source_asof"))
    if raw.get("source_asof") is not None and (not source_at or source_at > observed):
        raise HomeUnavailableError("官网计算时间无法核验。")
    if mode == "close":
        if (raw.get("source") != "historical_score" or raw.get("cache_status") != "historical"
                or raw.get("market_snapshot_at") != "15:00" or raw.get("is_trading") is not False
                or not source_at or source_at < datetime.combine(day, time(15), CN_TZ)):
            return None
    else:
        if observed.date() != day or observed.time() >= time(15) or raw.get("source") == "historical_score":
            return None
        market = raw.get("market_snapshot_at")
        if not source_at or source_at.date() != day or (not items and not market):
            return None
        if market:
            try:
                if time.fromisoformat(market) > observed.time():
                    return None
            except (TypeError, ValueError) as exc:
                raise HomeUnavailableError("官网行情时点无法核验。") from exc
    return raw


def _archived_close(root, day):
    saved = _read(_directory(root, day) / "close.json", None)
    if saved is None:
        return None
    fetched = _stamp(saved.get("fetched_at")) if isinstance(saved, dict) else None
    if not fetched or fetched > cn_now() or not _packet(saved.get("raw"), day, fetched, "close"):
        raise HomeUnavailableError("本地正式收盘榜格式异常，原文件已保留。")
    return saved


def _close(root, day):
    def load():
        saved = _archived_close(root, day)
        now = cn_now()
        if saved is not None:
            checked = _stamp(saved.get("checked_at", saved["fetched_at"]))
            interval = timedelta(minutes=5) if day == now.date() else timedelta(days=1)
            if checked and timedelta(0) <= now - checked < interval:
                return saved
        try:
            raw = _remote(root, "home_topics", day.isoformat())
            now = cn_now()
            if _packet(raw, day, now, "close") is None:
                if saved is None:
                    return None
                raise HomeUnavailableError("官网暂未返回可核验的正式版本。")
            fetched_at = now.isoformat(timespec="microseconds")
            record = {"raw": raw, "fetched_at": fetched_at, "checked_at": fetched_at}
            if saved:
                if raw == saved["raw"]:
                    record["fetched_at"] = saved["fetched_at"]
                elif _stamp(raw["source_asof"]) <= _stamp(saved["raw"]["source_asof"]):
                    raise HomeUnavailableError("官网返回的变更缺少更新的计算时刻。")
            _write(_directory(root, day) / "close.json", record)
            return record
        except (HomeUnavailableError, OSError) as exc:
            if not saved:
                raise
            warning = str(exc) if isinstance(exc, HomeUnavailableError) else "本地日榜更新未能保存。"
            return {**saved, "stale": True, "warning": warning + " 显示已保存的同日正式日榜。"}

    return _shared((root, "close", day), 60, load)


def _ranked(root, day, items, *, offline=False):
    rows = [{**r, "rank": i, "rank_change": None, "new_entry": None, "streak": None, "streak_min": 1}
            for i, r in enumerate(items, 1)]
    pending = {r["tid"]: r for r in rows}
    previous = None
    if not pending:
        return rows, previous
    calendar = load_game_calendar(root, start=day - timedelta(days=40), through=day, cutoff=cn_now())
    iso = day.isoformat()
    if not calendar["verified"] or iso not in calendar["sessions"]:
        return rows, previous
    prior_days = [d for d in calendar["sessions"] if d < iso][-_HISTORY:]
    for index, prior in enumerate(reversed(prior_days)):
        if not pending:
            break
        prior_day = date.fromisoformat(prior)
        try:
            if not offline and not (_directory(root, prior_day) / "close.json").exists():
                dates = _shared((root, "available_dates"), 300, lambda: _remote(root, "home_topic_dates"))
                # 不跳过未发布的交易日；端点只列近60项，更早日期可直接核验历史接口。
                if dates and prior >= min(dates) and prior not in dates:
                    break
            saved = _archived_close(root, prior_day) if offline else _close(root, prior_day)
        except HomeUnavailableError:
            break
        if saved is None:
            break
        offline = offline or saved.get("stale", False)
        if index == 0:
            previous = prior
        ranks = {r["tid"]: i for i, r in enumerate(saved["raw"]["items"], 1)}
        for tid in list(pending):
            row, position = pending[tid], ranks.get(tid)
            if index == 0:
                row["new_entry"] = position is None
                row["rank_change"] = None if position is None else position - row["rank"]
            if position is None:
                row["streak"] = row["streak_min"]
                pending.pop(tid)
            else:
                row["streak_min"] += 1
    return rows, previous


def _view(root, day, raw, mode, *, offline=False):
    rows, previous = _ranked(root, day, raw["items"], offline=offline)
    return {"date": day.isoformat(), "state": "ready", "mode": mode, "as_of": None,
            "source_at": raw.get("source_asof"), "market_snapshot_at": raw.get("market_snapshot_at"),
            "source": raw.get("source"), "selection_rule": raw.get("selection_rule"),
            "candidate_count": raw.get("candidate_count"), "strategy_candidate_count": raw.get("strategy_candidate_count"),
            "previous_date": previous, "rows": rows, "stale": False,
            "message": "" if not rows or previous else "缺少相邻交易日的正式收盘榜，排名变化未知。"}


def _empty(day, mode, message, *, state="no_data"):
    return {"date": day.isoformat(), "state": state, "mode": mode, "as_of": None, "source": None,
            "source_at": None, "market_snapshot_at": None, "fetched_at": None, "stale": False,
            "selection_rule": None, "candidate_count": None, "strategy_candidate_count": None,
            "previous_date": None, "rows": [], "message": message}


def _observations(path, day):
    records = _read(path, [])
    if not isinstance(records, list):
        raise HomeUnavailableError("本地盘中采集记录格式异常。")
    last = None
    for r in records:
        when = _stamp(r.get("fetched_at")) if isinstance(r, dict) else None
        source_time = _stamp(r.get("source_at")) if when else None
        if (not when or when.date() != day or (last and when <= last) or r.get("date") != day.isoformat()
                or r.get("mode") not in ("intraday", "close") or not isinstance(r.get("rows"), list)
                or (r.get("source_at") is not None and (not source_time or source_time > when))):
            raise HomeUnavailableError("本地盘中采集记录格式异常。")
        last = when
    return records


def _record(path, day, observation, fetched):
    # 盘中抓取与盘后刷新边界可能重叠；只有写入/去重持锁。
    with _write_lock:
        records = _observations(path, day)
        points = [r["fetched_at"] for r in records]
        if records and {k: v for k, v in records[-1].items() if k != "fetched_at"} == observation:
            return {**records[-1], "points": points}
        if records and fetched <= _stamp(records[-1]["fetched_at"]):
            raise HomeUnavailableError("采集时钟未前进，已保留原有快照。")
        stamp = fetched.isoformat(timespec="microseconds")
        observation = {**observation, "fetched_at": stamp}
        _write(path, [*records, observation])
        return {**observation, "points": [*points, stamp]}


def read_home(data_dir: Path, day: date, *, as_of: datetime | None = None) -> dict:
    root, now = Path(data_dir).resolve(), cn_now()
    if type(day) is not date or day > now.date():
        raise ValueError("请选择不晚于今天的交易日期")
    if as_of is not None:
        if not isinstance(as_of, datetime) or as_of.utcoffset() is None:
            raise ValueError("回看时点必须带时区")
        as_of = as_of.astimezone(CN_TZ)
        if as_of.date() != day or as_of > now:
            raise ValueError("回看时点必须位于所选日期且不能晚于当前时刻")
    mode = "intraday" if as_of or (day == now.date() and now.time() < time(15)) else "close"
    path = _directory(root, day) / "observations.json"

    def capture():
        records = _observations(path, day)
        points = [r["fetched_at"] for r in records]
        try:
            if mode == "close":
                close = _close(root, day)
                if close:
                    observation = _view(root, day, close["raw"], mode, offline=bool(close.get("stale")))
                    if close.get("stale"):
                        return {**observation, "stale": True, "message": close["warning"],
                                "fetched_at": close["fetched_at"], "points": points}
                    fetched = cn_now()
                    if day == fetched.date():
                        return _record(path, day, observation, fetched)
                    return {**observation, "fetched_at": close["fetched_at"], "points": points}
                message = "官网尚无该日可核验的正式收盘榜。"
            else:
                raw = _remote(root, "home_topics", None)
                if _packet(raw, day, cn_now(), mode) is not None:
                    observation = _view(root, day, raw, mode)
                    fetched = cn_now()
                    if fetched.date() == day and fetched.time() < time(15):
                        return _record(path, day, observation, fetched)
                message = "官网未返回该交易日可核验的盘中榜单。"
            state = "no_data"
        except HomeUnavailableError as exc:
            message, state = str(exc), "unavailable"
        except OSError:
            message, state = "本地官网榜单写入失败，请检查数据目录。", "unavailable"
        if records:
            return {**records[-1], "stale": True, "message": message + " 显示上次本地采集。", "points": points}
        return {**_empty(day, mode, message, state=state), "points": points}

    try:
        if as_of is None:
            return _shared((root, "view", day, mode), 60, capture)
        records = [r for r in _observations(path, day) if _stamp(r["fetched_at"]) <= as_of]
        chosen = records[-1] if records else _empty(day, mode, "该时点之前没有本地采集记录。")
        return {**chosen, "as_of": as_of.isoformat(), "points": [r["fetched_at"] for r in records]}
    except (HomeUnavailableError, OSError):
        return {**_empty(day, mode, "本地榜单记录读写失败，原有数据已保留。", state="unavailable"),
                "as_of": as_of.isoformat() if as_of else None, "points": []}
