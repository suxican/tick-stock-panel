"""资讯分析·公告排雷 — 按自选股向巨潮查询公告, 读取时按标题规则识别风险。

- 范围只限自选股; 每股一次区间查询, 由显式同步在后台进行, 读取只读本地快照。
- 落盘保存每股的全部公告 (data/news_announcements/<symbol>.json), 风险规则在读取时应用,
  调整规则不必重拉。
- 公告日次日 06:00 (北京) 之后取得的日期视为完整, 不再重拉; 此前取得的可再次同步补全。
- 回看窗口按自然日覆盖到第 N 个前交易日 (周末与节假日也有公告)。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from threading import Event, Lock, Thread

from app.market_time import CN_TZ, cn_now
from app.services.dragon_tiger import _local_trading_days
from app.services.fs_utils import atomic_write_text

logger = logging.getLogger(__name__)

LOOKBACK_SESSIONS = 20  # 回看交易日数 (含所选日); 减持预披露后约 15 个交易日才开始减持
_FINAL_AFTER = time(6)
_SYMBOL = re.compile(r"[0-9]{6}\.(SH|SZ|BJ)")

# 顺序即优先级: 一条公告只归入第一条命中的规则。
# (类型, 名称, 级别, 命中, 排除)
_RULES = [
    ("penalty", "立案处罚", "high", r"立案|行政处罚|警示函|纪律处分|通报批评|公开谴责|市场禁入", r""),
    ("delist", "退市风险", "high", r"退市风险警示|终止上市|其他风险警示|面值退市|退市整理", r"撤销"),
    ("inquiry", "监管问询", "high", r"问询函|关注函|监管函|监管工作函|督促函", r"审核问询"),
    ("freeze", "冻结平仓", "medium", r"冻结|司法拍卖|强制平仓|被动减持|平仓风险|司法划转", r"解除冻结|解冻"),
    ("reduce_plan", "减持计划", "high", r"减持.*预披露|预披露.*减持|拟减持|减持(股份)?计划",
     r"不减持|终止|完毕|届满|结果|进展"),
    ("reduce", "减持进展", "medium", r"减持", r"不减持|终止"),
    ("unlock", "限售解禁", "medium", r"解除限售|限售股.*上市流通|限售股份上市流通", r"限制性股票|股权激励|归属"),
    ("loss", "业绩预亏", "medium", r"业绩预亏|业绩预减|预计.*亏损|首亏|续亏", r""),
    ("abnormal", "异常波动", "medium", r"异常波动|股价异动|交易异动", r""),
    ("guarantee", "担保诉讼", "medium", r"违规担保|资金占用|重大诉讼|仲裁|债务逾期", r""),
    ("clarify", "澄清说明", "low", r"澄清", r""),
    ("risk", "风险提示", "low", r"风险提示", r""),
]
# 中介机构的核查/法律意见与公司公告重复, 只看公司公告。
_NOISE = re.compile(r"核查意见|法律意见|独立财务顾问|保荐|英文版")
_COMPILED = [(k, label, level, re.compile(hit), re.compile(skip) if skip else None)
             for k, label, level, hit, skip in _RULES]
RISK_TYPES = [{"type": k, "label": label, "level": level} for k, label, level, _, _ in _RULES]
_LEVEL_ORDER = {"high": 0, "medium": 1, "low": 2}


def classify(title: str) -> tuple[str, str, str] | None:
    if _NOISE.search(title):
        return None
    for kind, label, level, hit, skip in _COMPILED:
        if hit.search(title) and not (skip and skip.search(title)):
            return kind, label, level
    return None


def _cache_path(data_dir: Path, symbol: str) -> Path:
    if not _SYMBOL.fullmatch(symbol):
        raise ValueError(f"无效代码: {symbol}")
    return data_dir / "news_announcements" / f"{symbol}.json"


def _is_final(d: date, fetched_at: str) -> bool:
    try:
        stamp = datetime.fromisoformat(fetched_at)
    except ValueError:
        return False
    return stamp >= datetime.combine(d + timedelta(days=1), _FINAL_AFTER, CN_TZ)


def _load(data_dir: Path, symbol: str) -> dict:
    """{"days": {日期: 取得时间}, "items": [...]}; 缺失或损坏视为未取得。"""
    try:
        data = json.loads(_cache_path(data_dir, symbol).read_text(encoding="utf-8"))
        if (isinstance(data, dict) and data.get("symbol") == symbol
                and isinstance(data.get("days"), dict) and isinstance(data.get("items"), list)):
            return data
    except (OSError, ValueError):
        pass
    return {"symbol": symbol, "days": {}, "items": []}


def window(data_dir: Path, target: date) -> list[date]:
    sessions = [d for d in _local_trading_days(data_dir) if d < target][-(LOOKBACK_SESSIONS - 1):]
    start = sessions[0] if sessions else target
    return [start + timedelta(days=i) for i in range((target - start).days + 1)]


def _pending(record: dict, days: list[date]) -> list[date]:
    return [d for d in days if not _is_final(d, record["days"].get(d.isoformat(), ""))]


@dataclass
class _Task:
    target: date
    symbols: list[str]
    progress: dict = field(default_factory=lambda: {"done": 0, "total": 0, "failed": 0})
    state: str = "running"
    message: str = ""
    stop: Event = field(default_factory=Event)


_LOCK = Lock()
_task: _Task | None = None


def _client():
    from app.plugins.cninfo.client import CninfoClient

    return CninfoClient()


def _org_ids(client, data_dir: Path) -> dict[str, str]:
    """巨潮机构代码表变化很少: 本地副本 7 天内直接用, 过期时重取, 重取失败退回旧副本。"""
    from app.plugins.cninfo.client import CninfoError

    path = data_dir / "news_announcements" / "_org_ids.json"
    saved: dict = {}
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
        if cn_now() - datetime.fromisoformat(saved["fetched_at"]) < timedelta(days=7):
            return saved["org_ids"]
    except (OSError, ValueError, KeyError, TypeError):
        saved = {}
    try:
        org_ids = client.org_ids()
    except CninfoError:
        if isinstance(saved.get("org_ids"), dict) and saved["org_ids"]:
            return saved["org_ids"]
        raise
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps({"fetched_at": cn_now().isoformat(timespec="seconds"),
                                        "org_ids": org_ids}, ensure_ascii=False))
    return org_ids


def _sync_symbol(client, org_ids: dict[str, str], data_dir: Path, symbol: str, days: list[date]) -> bool:
    org_id = org_ids.get(symbol.split(".")[0])
    if org_id is None:
        return False
    record = _load(data_dir, symbol)
    need = _pending(record, days)
    if not need:
        return True
    start, end = need[0], need[-1]
    raw = client.stock_announcements(symbol, org_id, start, end)
    stamp = cn_now().isoformat(timespec="seconds")
    kept = [i for i in record["items"] if not start.isoformat() <= i["published_at"][:10] <= end.isoformat()]
    fresh = [{"symbol": symbol, **{k: r[k] for k in ("id", "name", "title", "published_at", "has_time", "url")}}
             for r in raw]
    covered = dict(record["days"])
    for i in range((end - start).days + 1):
        covered[(start + timedelta(days=i)).isoformat()] = stamp
    path = _cache_path(data_dir, symbol)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps({"symbol": symbol, "days": covered, "items": kept + fresh},
                                       ensure_ascii=False))
    return True


def _run(task: _Task, data_dir: Path) -> None:
    from app.plugins.cninfo.client import CninfoError

    client = None
    days = window(data_dir, task.target)
    failed: list[str] = []
    try:
        client = _client()
        org_ids = _org_ids(client, data_dir)
        for symbol in task.symbols:
            if task.stop.is_set():
                break
            try:
                ok = _sync_symbol(client, org_ids, data_dir, symbol, days)
            except (CninfoError, ValueError, OSError) as exc:
                logger.warning("巨潮公告同步失败 %s (%s)", symbol, exc)
                ok = False
            if not ok:
                failed.append(symbol)
            with _LOCK:
                task.progress.update(done=task.progress["done"] + 1, failed=len(failed))
        with _LOCK:
            task.state = "error" if failed else "ready"
            task.message = f"{len(failed)} 只未能取得公告，可稍后重试。" if failed else ""
    except CninfoError as exc:
        logger.warning("巨潮代码表获取失败 (%s)", exc)
        with _LOCK:
            task.state, task.message = "error", "巨潮暂时无法访问，请稍后重试。"
    except Exception as exc:  # noqa: BLE001 -- 后台线程须以可见状态结束, 不外泄响应内容
        logger.warning("巨潮公告同步异常 (%s)", type(exc).__name__)
        with _LOCK:
            task.state, task.message = "error", "公告同步失败，请稍后重试。"
    finally:
        if client is not None:
            client.close()


def start_sync(data_dir: Path, target: date, symbols: list[str]) -> dict:
    """同步自选股在回看窗口内缺失或尚未完整的公告。已在运行时不重复启动。"""
    global _task
    days = window(data_dir, target)
    todo = [s for s in symbols if _SYMBOL.fullmatch(s) and _pending(_load(data_dir, s), days)]
    with _LOCK:
        if _task and _task.state == "running":
            return {"started": False, "message": "公告正在同步，请稍后。"}
        if not todo:
            return {"started": False, "message": "自选股的公告已是完整快照。"}
        _task = _Task(target, todo)
        _task.progress["total"] = len(todo)
        task = _task
    Thread(target=_run, args=(task, data_dir), daemon=True, name="cninfo-announcements").start()
    return {"started": True, "message": f"开始同步 {len(todo)} 只自选股的公告。"}


def stop_sync() -> None:
    with _LOCK:
        if _task:
            _task.stop.set()


def _sync_status() -> dict | None:
    with _LOCK:
        if _task is None:
            return None
        return {"state": _task.state, "message": _task.message, **dict(_task.progress)}


def get_risks(data_dir: Path, target: date, symbols: list[str]) -> dict:
    """自选股在回看窗口内的风险公告 (只读本地快照)。"""
    days = window(data_dir, target)
    start, end = days[0].isoformat(), target.isoformat()
    rows, missing, partial = [], [], []
    for symbol in symbols:
        if not _SYMBOL.fullmatch(symbol):
            continue
        record = _load(data_dir, symbol)
        if not record["days"]:
            missing.append(symbol)
            continue
        if any(d.isoformat() not in record["days"] for d in days):
            partial.append(symbol)
        for item in record["items"]:
            if not start <= item["published_at"][:10] <= end:
                continue
            if hit := classify(item["title"]):
                rows.append({**item, "type": hit[0], "label": hit[1], "level": hit[2]})
    # 级别优先, 同级别新公告在前
    rows.sort(key=lambda r: (_LEVEL_ORDER[r["level"]], -datetime.fromisoformat(r["published_at"]).timestamp()))
    counts = {t["type"]: 0 for t in RISK_TYPES}
    for row in rows:
        counts[row["type"]] += 1
    return {
        "date": end,
        "start": start,
        "sync": _sync_status(),
        "symbols": len([s for s in symbols if _SYMBOL.fullmatch(s)]),
        "missing": missing,
        "partial": partial,
        "types": RISK_TYPES,
        "counts": counts,
        "rows": rows,
    }
