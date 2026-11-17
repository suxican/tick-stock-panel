"""盘中剧本核验 —— 开盘半小时后用上一篇复盘的结构化剧本判断今天走哪个情景。

工作日 10:02(10:00 分钟K落盘后)运行, 也可在复盘页手动触发:
1. 找到紧邻上一交易日的复盘剧本(report["plan"]); 不紧邻或无剧本 → 不判定;
2. 计算今日 09:30–10:00 全市场成交额(与剧本条件同口径, 分钟数据不完整 → 待定);
3. 只判定盘中可得的条件(开盘半小时成交额), 全天指标留待收盘;
4. 给出"倾向情景"及其应对, 推送到复盘推送渠道, 结果归档供复盘页展示。

开盘分钟数据不完整时(全量分钟服务逐标的轮询有缺口 / 未开启), 先经「分钟数据源」
只对缺 09:31–10:00 覆盖的标的补拉今日开盘窗口并落盘, 再重新判定。
判定完全由程序完成, 不调用 AI。
"""
from __future__ import annotations

import logging
import threading
from datetime import date, datetime, time, timedelta

from app.market_time import cn_now
from app.services.json_report_store import JsonReportStore

logger = logging.getLogger(__name__)

JOB_ID = "intraday_plan_check"
RUN_HOUR, RUN_MINUTE = 10, 2
MAX_RESULTS = 20

_store = JsonReportStore("market_recap_intraday.json", MAX_RESULTS, id_prefix="mri", id_with_symbol=False)


def list_results() -> list[dict]:
    return _store.list_reports()


def latest_result(day: str | None = None) -> dict | None:
    for r in list_results():
        if day is None or r.get("date") == day:
            return r
    return None


def _sessions(today: date) -> list[date] | None:
    try:
        from app.services.huichun_calendar import BUNDLED_CALENDAR_PATH, load_market_calendar

        return load_market_calendar(BUNDLED_CALENDAR_PATH, start=today - timedelta(days=20), end=today)
    except Exception as e:  # noqa: BLE001 - 日历不可用时不猜交易日
        logger.info("intraday plan check: calendar unavailable: %s", e)
        return None


def _traded_count(data_dir, day: date) -> int:
    """上一交易日有成交的个股数(涨+跌家数), 作开盘分钟数据完整性的全市场基准。"""
    try:
        import polars as pl

        from app.services.regime_builder import load_regime_history

        df = load_regime_history(data_dir)
        row = df.filter(pl.col("date") == day).to_dicts()
        if row:
            return int((row[0].get("up_count") or 0) + (row[0].get("down_count") or 0))
    except Exception as e:  # noqa: BLE001
        logger.debug("intraday plan check: regime unavailable: %s", e)
    return 0


def _prev_metric(report: dict, key: str) -> float | None:
    for m in (report.get("snapshot") or {}).get("metrics") or []:
        if m.get("key") == key:
            return m.get("today")
    return None


# ───────────────────────── 开盘分钟补数 ─────────────────────────

_OPEN_CHECK_START, _OPEN_END = time(9, 31), time(10, 0)
_OPEN_MINUTES = 30                       # 与 opening_amount_yi 的完整性口径一致
_BACKFILL_START = time(9, 25)            # 与 /api/kline/minute 当日补拉起点一致
_backfill_lock = threading.Lock()
_backfilling: set[str] = set()           # 正在后台补数的日期


def opening_gap_symbols(repo, day: date) -> list[str]:
    """全市场 A 股中, 今日分区 09:31–10:00 不满 30 根的标的(含分区里完全没有的)。"""
    import polars as pl

    inst = repo.get_instruments()
    if inst.is_empty() or "symbol" not in inst.columns:
        return []
    universe = inst["symbol"].cast(pl.Utf8).unique().sort().to_list()
    p = repo.store.data_dir / "kline_minute" / f"date={day.isoformat()}" / "part.parquet"
    complete: set[str] = set()
    if p.exists():
        try:
            done = (
                pl.scan_parquet(p).select(["symbol", "datetime"])
                .filter(pl.col("datetime").dt.time().is_between(_OPEN_CHECK_START, _OPEN_END))
                .group_by("symbol").agg(pl.col("datetime").n_unique().alias("n"))
                .filter(pl.col("n") >= _OPEN_MINUTES)
                .collect()
            )
            complete = set(done["symbol"].to_list())
        except Exception as e:  # noqa: BLE001 - 读失败按全缺处理, 补拉幂等
            logger.info("intraday plan check: read minute partition failed: %s", e)
    return [s for s in universe if s not in complete]


def _minute_source_allowed(capset) -> bool:
    """分钟数据源能否取数(口径同 api/kline._minute_allowed): TickFlow 需分钟批量能力。"""
    from app.services import kline_sync
    from app.tickflow.capabilities import Cap

    provider = kline_sync._selected_minute_provider_name()
    if provider == "tickflow":
        return capset is not None and capset.has(Cap.KLINE_MINUTE_BATCH)
    _, fallback, error = kline_sync._resolve_minute_provider(provider)
    return error is None and not fallback


def backfill_opening_minutes(repo, day: date, capset, *, on_updated=None) -> dict:
    """补拉今日开盘窗口(09:25–10:00)缺覆盖标的的分钟K并落盘。

    返回 {"status": "ok"|"complete"|"unavailable"|"failed", "symbols": 缺口标的数, "rows": 取回行数}。
    走分钟数据源(与自选分时补拉同一路径/限速/落盘纪律), 不经全量分钟服务。
    """
    gaps = opening_gap_symbols(repo, day)
    if not gaps:
        return {"status": "complete", "symbols": 0, "rows": 0}
    if not _minute_source_allowed(capset):
        return {"status": "unavailable", "symbols": len(gaps), "rows": 0}
    from app.services import minute_adjust
    from app.services.minute_shared_pull import pull
    from app.tickflow.capabilities import Cap
    from app.tickflow.rate_limits import resolve_limit

    batch = rpm = None
    if capset is not None:
        lim = resolve_limit(capset, Cap.KLINE_MINUTE_BATCH)
        batch, rpm = lim.batch, lim.rpm
    try:
        df = pull(repo, gaps, asset_type="stock",
                  start=datetime.combine(day, _BACKFILL_START), end=datetime.combine(day, _OPEN_END),
                  batch_size=batch, rpm=rpm,
                  raw_basis=minute_adjust.minute_basis_is_raw(repo.store.data_dir),
                  on_updated=on_updated)
    except Exception as e:  # noqa: BLE001 - 补数失败不阻断核验, 结果仍为待定
        logger.warning("intraday plan check: opening minute backfill failed: %s", e)
        return {"status": "failed", "symbols": len(gaps), "rows": 0}
    logger.info("intraday plan check: backfilled opening minutes for %d symbols (%d rows)",
                len(gaps), df.height)
    return {"status": "ok", "symbols": len(gaps), "rows": df.height}


def is_backfilling(day: str) -> bool:
    with _backfill_lock:
        return day in _backfilling


def run_intraday_check(repo, *, now: datetime | None = None, reports: list[dict] | None = None) -> dict:
    """执行一次盘中核验并返回结果(不归档、不推送)。

    status: ok(已判定) | no_plan(无可用剧本) | not_trading(非交易日)
            | too_early(10:00 前) | data_incomplete(开盘分钟数据不完整)
    """
    from app.services.market_recap_plan import evaluate_plan, opening_amount_yi

    now = now or cn_now()
    today = now.date()
    base = {"date": today.isoformat(), "generated_at": now.strftime("%Y-%m-%dT%H:%M:%S")}
    sessions = _sessions(today)
    if not sessions or today not in sessions:
        return {**base, "status": "not_trading", "message": "今日非交易日或交易日历不可用"}
    if (now.hour, now.minute) < (RUN_HOUR, 1):
        return {**base, "status": "too_early", "message": "10:00 后才有完整的开盘半小时数据"}
    prev_day = max((d for d in sessions if d < today), default=None)
    if reports is None:
        from app.services import market_recap_reports

        reports = market_recap_reports.list_reports()
    candidates = [r for r in reports if r.get("as_of") == (prev_day.isoformat() if prev_day else None) and r.get("plan")]
    if not candidates:
        return {**base, "status": "no_plan",
                "message": f"上一交易日({prev_day})没有带结构化剧本的复盘" if prev_day else "找不到上一交易日"}
    report = max(candidates, key=lambda r: str(r.get("created_at") or ""))

    data_dir = repo.store.data_dir
    amount = opening_amount_yi(data_dir, today.isoformat(), _traded_count(data_dir, prev_day))
    snapshot = {"as_of": today.isoformat(), "intraday": True,
                "metrics": [{"key": "amount_30m", "today": amount}]}
    check = evaluate_plan(report["plan"], snapshot, repo, from_as_of=report["as_of"])
    # 倾向: 盘中已判定的条件全部成立(match=all)或任一成立(match=any)的情景;
    # 全天条件(涨停家数等)保持待定, 收盘复盘再定型
    leaning = []
    for sc in check["scenarios"]:
        states = [c["status"] for c in sc["conditions"]]
        if sc["status"] == "hit" or (sc["status"] == "pending" and "hit" in states):
            leaning.append(sc)
    prev_30m = _prev_metric(report, "amount_30m")
    result = {
        **base,
        "status": "ok" if amount is not None else "data_incomplete",
        "from_as_of": report["as_of"],
        "amount_30m": amount,
        "prev_amount_30m": prev_30m,
        "leaning": [{"name": s["name"], "primary": s["primary"], "action": s.get("action", "")} for s in leaning],
        "check": check,
    }
    if amount is None:
        result["message"] = "开盘分钟数据不完整, 暂无法判定(请确认分钟数据服务在运行)"
    return result


def format_push(result: dict) -> str:
    """盘中核验 → 推送正文(Markdown, 不含持仓)。"""
    lines = [f"**10:00 剧本核验** · 来自 {result.get('from_as_of') or '—'} 复盘"]
    if result.get("status") != "ok":
        lines.append(result.get("message") or "暂无法判定")
        return "\n\n".join(lines)
    amount, prev = result["amount_30m"], result.get("prev_amount_30m")
    vs = f"(较上一交易日同时段 {(amount / prev - 1) * 100:+.0f}%)" if prev else ""
    lines.append(f"开盘半小时成交额 {amount:,.0f} 亿{vs}")
    if result["leaning"]:
        for s in result["leaning"]:
            tag = "(主判断)" if s["primary"] else ""
            act = f": {s['action']}" if s.get("action") else ""
            lines.append(f"→ 倾向「{s['name']}{tag}」{act}")
    else:
        lines.append("→ 开盘量能未落入任何情景区间, 剧本覆盖不到今天, 以观察为主")
    if result["check"].get("overlaps"):
        lines.append("注: 该剧本情景有重叠, 倾向可能不唯一")
    lines.append("其余条件(涨停家数等)待收盘确认。")
    return "\n\n".join(lines)


_BACKFILL_MESSAGES = {
    "unavailable": "开盘分钟数据不完整, 当前分钟数据源无法补拉, 暂无法判定",
    "failed": "开盘分钟数据不完整, 补拉失败, 暂无法判定(可稍后重试)",
}


def run_and_record(repo, *, push: bool, now: datetime | None = None,
                   backfill: bool = False, capset=None, on_updated=None) -> dict:
    """执行核验 → (数据不全时补数后重判) → 归档(仅已到判定时点的结果) → 按需推送。

    供定时任务与手动触发共用; backfill=True 时同步补拉开盘分钟(数十秒到数分钟)。
    """
    result = run_intraday_check(repo, now=now)
    if backfill and result["status"] == "data_incomplete":
        fill = backfill_opening_minutes(repo, date.fromisoformat(result["date"]), capset,
                                        on_updated=on_updated)
        if fill["status"] == "ok":
            result = run_intraday_check(repo, now=now)
        if result["status"] == "data_incomplete" and fill["status"] in _BACKFILL_MESSAGES:
            result["message"] = _BACKFILL_MESSAGES[fill["status"]]
        result["backfill"] = fill
    return _record(result, push=push)


def _record(result: dict, *, push: bool) -> dict:
    if result["status"] in ("ok", "data_incomplete", "no_plan"):
        result = _store.save_report(dict(result))
        if push and result["status"] != "no_plan":
            from app.jobs.daily_pipeline import _maybe_push_review

            _maybe_push_review(format_push(result), {"as_of": result["date"]},
                               title="盘中剧本核验", event="market_review_intraday")
    return result


def run_manual(repo, capset, *, on_updated=None, now: datetime | None = None) -> dict:
    """「立即核验」: 数据完整直接判定归档; 开盘分钟不全 → 后台补数后重判归档(不推送)。

    返回 {"result": 本次结果, "backfilling": 是否在后台补数}; 补数期间重复点击不重复启动。
    """
    day = (now or cn_now()).date().isoformat()
    if is_backfilling(day):
        return {"result": latest_result(day), "backfilling": True}
    result = run_intraday_check(repo, now=now)
    if result["status"] != "data_incomplete":
        return {"result": _record(result, push=False), "backfilling": False}
    with _backfill_lock:
        if day in _backfilling:
            return {"result": result, "backfilling": True}
        _backfilling.add(day)

    def work() -> None:
        try:
            run_and_record(repo, push=False, backfill=True, capset=capset, on_updated=on_updated)
        except Exception:
            logger.exception("intraday plan check backfill failed")
        finally:
            with _backfill_lock:
                _backfilling.discard(day)

    threading.Thread(target=work, daemon=True, name="intraday-check-backfill").start()
    return {"result": {**result, "message": "开盘分钟数据不完整, 正在补拉…"}, "backfilling": True}
