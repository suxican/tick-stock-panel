"""按指定交易日的 ZZShare 题材候选和完整成分，使用同日本地股票行情计算强度。"""
from __future__ import annotations

import hashlib
import json
import logging
import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from threading import Event, Lock, Thread

import httpx
import polars as pl

from app.data_providers import custom as custom_sources
from app.enriched_generation import enriched_publication_incomplete
from app.market_time import CN_TZ, cn_now, cn_today
from app.parquet import scan_enriched_parquet
from app.polars_guard import guarded_collect
from app.services.daily_health import assert_usable
from app.services.fs_utils import atomic_write_text
from app.services.market_game_calendar import load_game_calendar
from app.tickflow.repository import enriched_dirname

logger = logging.getLogger(__name__)
_VERSION = 1
_SCOPE = "候选取自 ZZShare 指定日题材热度榜（请求上限 1000）。分数使用当日成分和本地同日行情，非实时推送。"
_LOCK = Lock()


@dataclass
class _Task:
    directory: Path
    report: dict
    previous: dict | None
    stop: Event = field(default_factory=Event)


# 同一个后端只允许一个显式同步任务；读者只读整份替换的快照，不等待网络。
_task: _Task | None = None


def _initial(day: date) -> dict:
    return {
        "date": day.isoformat(), "state": "idle", "scope": _SCOPE, "message": "尚未同步该日题材。",
        "rows": [], "coverage": {"candidates": 0, "completed": 0, "eligible": 0},
        "progress": {"done": 0, "total": 0}, "quote_asof": None, "computed_at": None,
    }


def _directory(repo, day: date) -> Path:
    if type(day) is not date or day > cn_today():
        raise ValueError("题材日期必须是不晚于北京今天的有效日期")
    return repo.store.data_dir / "zzshare_radar" / f"v{_VERSION}" / day.isoformat()


def _write(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, allow_nan=False))


def get_themes(repo, day: date) -> dict:
    directory = _directory(repo, day)
    with _LOCK:
        if _task and _task.directory == directory:
            return _task.report
    try:
        record = json.loads((directory / "report.json").read_text(encoding="utf-8"))
        if (record["date"] != day.isoformat() or record["state"] != "ready"
                or not isinstance(record["rows"], list) or not isinstance(record["computed_at"], str)
                or record["coverage"]["completed"] != record["coverage"]["candidates"]
                or record["coverage"]["eligible"] != len(record["rows"])
                or record["progress"]["done"] != record["progress"]["total"]
                or not set(_initial(day)).issubset(record)):
            raise ValueError("invalid cache")
        return record
    except FileNotFoundError:
        return _initial(day)
    except (ValueError, KeyError, TypeError, OSError):
        return {**_initial(day), "state": "error", "message": "该日题材缓存不可读，请重新计算。"}


def _numeric(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _quote_frame(repo, day: date) -> pl.DataFrame:
    """只扫相邻两个已发布的股票 enriched 分区；连板存储列来自统一的涨跌停判定引擎。"""
    root = repo.store.data_dir
    calendar = load_game_calendar(root, start=day - timedelta(days=32), through=day, cutoff=cn_now())
    sessions = [s for s in calendar["sessions"] if s <= day.isoformat()]
    if not calendar["verified"] or len(sessions) < 2 or sessions[-1] != day.isoformat():
        return pl.DataFrame()
    assert_usable(root, "stock", end=day, enriched_only=True)
    if enriched_publication_incomplete(root, "stock"):
        raise ValueError("股票日线分区正在发布")
    paths = [root / enriched_dirname("stock") / f"date={s}" / "part.parquet" for s in sessions[-2:]]
    if not all(path.is_file() for path in paths):
        return pl.DataFrame()
    signature = [path.stat().st_mtime_ns for path in paths]
    prior = scan_enriched_parquet(paths[0]).filter(pl.col("date") == date.fromisoformat(sessions[-2])).select(
        "symbol", pl.col("close").alias("previous_close")
    ).filter(pl.col("symbol").is_unique() & (pl.col("previous_close") > 0))
    today = scan_enriched_parquet(paths[1]).filter(pl.col("date") == day).select(
        "date", "symbol", "close", "raw_close", "quote_ts", "volume", "amount",
        "consecutive_limit_ups", "consecutive_limit_downs",
    )
    frame = guarded_collect(
        today.join(prior, on="symbol", how="inner").with_columns(
            (pl.col("close") / pl.col("previous_close") - 1).alias("change_pct")
        ), priority="background",
    )
    if enriched_publication_incomplete(root, "stock") or signature != [path.stat().st_mtime_ns for path in paths]:
        raise ValueError("行情分区在读取期间发生变化")
    return frame


def _quotes(repo, day: date) -> tuple[dict, str | None]:
    frame = _quote_frame(repo, day)
    required = {"date", "symbol", "change_pct", "raw_close", "consecutive_limit_ups", "consecutive_limit_downs"}
    if not required.issubset(frame.columns):
        return {}, None
    frame = frame.filter(pl.col("date").cast(pl.Date, strict=False) == day).filter(pl.col("symbol").is_unique())
    columns = [*sorted(required), *(c for c in ("amount", "quote_ts", "volume") if c in frame.columns)]
    quotes = {}
    stamps = set()
    now_ms = cn_now().timestamp() * 1000
    midnight_ms = datetime.combine(day, datetime.min.time(), CN_TZ).timestamp() * 1000
    for row in frame.select(columns).iter_rows(named=True):
        symbol, change = row["symbol"], row["change_pct"]
        price, up, down = row["raw_close"], row["consecutive_limit_ups"], row["consecutive_limit_downs"]
        amount, volume, stamp = row.get("amount"), row.get("volume"), row.get("quote_ts")
        if (not _numeric(change) or not _numeric(price) or price <= 0
                or any(type(v) is not int or v < 0 for v in (up, down)) or (up > 0 and down > 0)):
            continue
        if not ((_numeric(volume) and volume > 0) or (_numeric(amount) and amount > 0)):
            continue
        if stamp is not None and not (_numeric(stamp) and midnight_ms <= stamp < midnight_ms + 86400000 and stamp <= now_ms):
            continue
        if not isinstance(symbol, str) or "." not in symbol:
            continue
        quotes[symbol.split(".")[0]] = {
            "symbol": symbol, "change": round(change, 12), "limit_up": up > 0,
            "limit_down": down > 0, "amount": amount if _numeric(amount) and amount > 0 else None,
        }
        stamps.add(stamp)
    # 不将分散时间戳、缺失时间戳、采集时刻冒充统一的行情时点。
    timestamp = stamps.pop() if len(stamps) == 1 else None
    return quotes, datetime.fromtimestamp(timestamp / 1000, CN_TZ).isoformat() if timestamp else None


def _theme(plate: dict, members: dict[str, str], quotes: dict) -> dict | None:
    stocks = [quotes[code] for code in members if code in quotes]
    n = len(stocks)
    if n < 5 or n / len(members) < .80:
        return None
    mean = math.fsum(stock["change"] for stock in stocks) / n
    rising = sum(stock["change"] > 0 for stock in stocks)
    if mean <= 0 or rising * 2 < n:
        return None
    strong = sum(stock["change"] > .05 for stock in stocks)
    up = sum(stock["limit_up"] for stock in stocks)
    down = sum(stock["limit_down"] for stock in stocks)
    score = (40 * min(mean / .05, 1) + 20 * rising / n
             + 20 * min(strong / n / .30, 1) + 20 * min(up / n / .10, 1))
    leader = min(stocks, key=lambda s: (-s["change"], -(s["amount"] or 0), s["symbol"]))
    anchor = min((s for s in stocks if s["change"] > 0 and s["amount"] is not None),
                 key=lambda s: (-s["amount"], -s["change"], s["symbol"]), default=None)

    def core(stock):
        return {"symbol": stock["symbol"], "name": members[stock["symbol"].split(".")[0]],
                "change_pct": round(stock["change"] * 100, 4), "amount_yuan": stock["amount"]}

    return {
        "plate_code": plate["plate_code"], "plate_name": plate["plate_name"], "score": round(score, 4),
        "member_count": len(members), "valid_count": n, "coverage_pct": round(100 * n / len(members), 2),
        "avg_change_pct": round(mean * 100, 4), "rising_count": rising, "rising_pct": round(100 * rising / n, 2),
        "strong_count": strong, "strong_pct": round(100 * strong / n, 2),
        "limit_up_count": up, "limit_down_count": down, "leader": core(leader), "anchor": core(anchor) if anchor else None,
    }


def _members(provider, directory: Path, day: str, code: str) -> dict:
    name = hashlib.sha256(code.encode()).hexdigest()
    path = directory / "members" / f"{name}.json"
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
        if (cached["date"] == day and cached["code"] == code and isinstance(cached["members"], dict)
                and cached["members"] and cached["count"] == len(cached["members"])
                and all(len(ticker) == 6 and ticker.isascii() and ticker.isdigit() and isinstance(name, str)
                        for ticker, name in cached["members"].items())):
            return cached["members"]
    except (OSError, ValueError, TypeError, KeyError):
        pass
    rows = provider.research("plates_stocks", day, code=code, plate_type=17)
    members = {}
    for row in rows:
        ticker = row.get("stock_code")
        if (row.get("trade_date") != day or row.get("plate_code") != code or row.get("plate_type") != 17
                or not isinstance(ticker, str) or len(ticker) != 6 or not ticker.isascii()
                or not ticker.isdigit() or ticker in members):
            raise ValueError("成分日期、标识或唯一性不符")
        members[ticker] = row.get("stock_name") or ticker
    if not members:
        raise ValueError("成分为空")
    _write(path, {"date": day, "code": code, "count": len(members), "members": members})
    return members


def _publish(task: _Task, **updates) -> dict:
    with _LOCK:
        task.report = {**task.report, **updates}
        return task.report


def _failed(task: _Task, message: str, *, state: str = "error") -> None:
    if task.previous is not None:
        _publish(task, **{**task.previous, "progress": task.report["progress"], "state": "error",
                          "message": message + " 现显示上次完整计算的同日快照。"})
    else:
        _publish(task, state=state, message=message)


def _run(task: _Task, repo, day: date) -> None:
    try:
        quotes, quote_asof = _quotes(repo, day)
        if not quotes:
            _failed(task, "缺少可核验的交易日历、相邻两日股票行情或当日涨跌停计数，请检查日线数据。", state="no_data")
            return
        provider = custom_sources.get_provider("zzshare")
        ds = day.isoformat()
        plates = provider.research("plates_rank", ds, plate_type=17, limit=1000)
        if not plates:
            _failed(task, "上游未返回该日题材候选。", state="no_data")
            return
        if (any(p.get("trade_date") != ds or p.get("plate_type") != 17 or not p.get("plate_code")
                or not p.get("plate_name") for p in plates)
                or len({p["plate_code"] for p in plates}) != len(plates)):
            raise ValueError("题材候选返回错日、缺字段或重复代码")
        total = len(plates)
        completed = failures = consecutive_failures = 0
        halted_reason = ""
        eligible = []
        if task.previous is not None:
            _publish(task, progress={"done": 0, "total": total}, message="正在重新核验，现显示上次完整计算的同日快照。")
        else:
            _publish(task, quote_asof=quote_asof, progress={"done": 0, "total": total},
                     coverage={"candidates": total, "completed": 0, "eligible": 0}, message="正在按当日成分核验，当前排名为暂定结果。")
        for index, plate in enumerate(plates, 1):
            if task.stop.is_set():
                break
            try:
                members = _members(provider, task.directory, ds, plate["plate_code"])
                ranked = _theme(plate, members, quotes)
            except (ValueError, RuntimeError) as exc:
                failures += 1
                status = getattr(exc, "status_code", None)
                transport_failure = isinstance(exc.__cause__, httpx.RequestError) or (isinstance(status, int) and status >= 500)
                consecutive_failures = consecutive_failures + 1 if transport_failure else 0
                logger.warning("zzshare theme %s %s unavailable (%s)", ds, plate["plate_code"], type(exc).__name__)
                halted_reason = _source_status(exc)
            else:
                completed += 1
                consecutive_failures = 0
                if ranked:
                    eligible.append(ranked)
            progress = {"done": index, "total": total}
            if task.previous is not None:
                _publish(task, progress=progress)
            else:
                _publish(task, rows=_ordered(eligible), progress=progress,
                         coverage={"candidates": total, "completed": completed, "eligible": len(eligible)})
            if halted_reason or consecutive_failures >= 3:
                break
        if failures or completed != total:
            _failed(task, f"{halted_reason}本次仅完成 {completed}/{total} 个题材，再次同步可复用已取得的成分。")
            return
        final = {**task.report, "state": "ready", "computed_at": cn_now().isoformat(timespec="seconds"),
                 "quote_asof": quote_asof, "rows": _ordered(eligible),
                 "coverage": {"candidates": total, "completed": completed, "eligible": len(eligible)},
                 "message": f"已核验当日返回的 {total} 个题材，{len(eligible)} 个通过强度门槛。行情为本地指定日观测。"}
        _write(task.directory / "report.json", final)
        _publish(task, **final)
    except Exception as exc:  # noqa: BLE001 -- detached worker must finish visibly, without exposing payloads or credentials
        logger.warning("zzshare radar %s failed (%s)", day, type(exc).__name__)
        _failed(task, _source_status(exc) or "同步失败，请检查本地行情、ZZShare 连接和数据目录后重试。")


def _source_status(exc: Exception) -> str:
    status = getattr(exc, "status_code", None)
    if status == 429:
        return "ZZShare 限流（HTTP 429），请稍后重试。"
    if status in (401, 403):
        return f"ZZShare 拒绝访问（HTTP {status}），请检查凭据或数据权限。"
    return ""


def _ordered(rows: list[dict]) -> list[dict]:
    return sorted(rows, key=lambda r: (-r["score"], -r["avg_change_pct"], r["plate_code"]))


def start_themes_sync(repo, day: date) -> dict:
    global _task
    directory = _directory(repo, day)
    prior = get_themes(repo, day)
    with _LOCK:
        if _task and _task.report["state"] == "running":
            if _task.directory == directory:
                return _task.report
            return {**_initial(day), "message": f"正在同步 {_task.report['date']}，请稍后再试。"}
        previous = prior if prior["computed_at"] else None
        report = {**(previous or _initial(day)), "state": "running", "progress": {"done": 0, "total": 0},
                  "message": "正在重新核验，现显示上次完整计算的同日快照。" if previous else "正在准备指定日行情和题材目录。"}
        task = _Task(directory, report, previous)
        _task = task
        try:
            Thread(target=_run, args=(task, repo, day), daemon=True, name="zzshare-radar").start()
        except RuntimeError:
            task.report = {**report, "state": "error", "message": "后台同步未能启动，请稍后重试。"}
        return task.report


def stop_themes_sync() -> None:
    with _LOCK:
        if _task:
            _task.stop.set()
