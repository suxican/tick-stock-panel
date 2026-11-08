"""Offline first-board ideal-price research; never submits or saves backtest trades.

Run from backend: python -m scripts.research_first_board_ideal --output <new.json>
"""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import date, timedelta
from pathlib import Path

import polars as pl

from app.backtest.first_board_ideal import evaluate_first_board_ideal
from app.backtest.first_board_ideal_inputs import prepare_first_board_ideal_candidates
from app.config import settings
from app.market_time import cn_now
from app.services.huichun_calendar import BUNDLED_CALENDAR_PATH, read_market_calendar
from app.services.huichun_data import HuichunDataReader
from app.strategy.config import load_override
from app.strategy.engine import StrategyEngine
from app.tickflow.repository import DataStore, KlineRepository

PATTERNS = ("platform", "oversold", "trend")


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _config_signature(data_dir: Path) -> dict[str, str]:
    paths = [data_dir / "user_data/first_board/config.json"]
    for folder in (data_dir / "strategies", data_dir / "user_data/strategy_overrides"):
        paths.extend(path for path in folder.rglob("*")
                     if path.is_file() and path.suffix in {".py", ".json"})
    return {str(path.relative_to(data_dir)): _digest(path) for path in paths if path.is_file()}


def _parameters(data_dir: Path, explicit: dict) -> dict:
    engine = StrategyEngine([data_dir / "strategies" / "custom"])
    result = {}
    if set(explicit) - set(PATTERNS):
        raise ValueError("参数文件只支持 platform / oversold / trend 三组")
    for pattern in PATTERNS:
        sid = f"custom_first_board_{pattern}_daily"
        strategy = engine.get(sid)
        saved = load_override(data_dir, sid)
        # This diagnostic deliberately shares shape parameters, not arbitrary
        # overlays, entry triggers or portfolio settings from a generic strategy.
        if saved.get("overlay_filter"):
            raise ValueError(f"{sid} 含叠加条件，理想模型无法复现，请先单独导出形态参数")
        supplied = explicit.get(pattern, {})
        if not isinstance(supplied, dict):
            raise ValueError("每个形态的参数必须是对象")
        result[pattern] = {
            "strategy_id": sid,
            "params": engine.resolve_params(strategy, supplied, saved),
            "strategy_sha256": _digest(strategy.file_path) if strategy.file_path else None,
            "signal_source": "shared_first_board_shape_rules_only",
        }
    return result


def run_research(data_dir: Path, *, start: date | None = None, end: date | None = None,
                 sessions: int = 60, explicit_params: dict | None = None) -> dict:
    """Read local raw prices once; keep invalid data and every exclusion visible."""
    if sessions < 1:
        raise ValueError("交易日数量必须为正数")
    before = _config_signature(data_dir)
    parameters = _parameters(data_dir, explicit_params or {})
    calendar = read_market_calendar(BUNDLED_CALENDAR_PATH)
    store = DataStore(data_dir)
    try:
        repo = KlineRepository(store)
        reader = HuichunDataReader(repo, calendar_path=BUNDLED_CALENDAR_PATH)
        latest = repo.execute_all("SELECT MAX(date) FROM kline_daily")[0][0]
        if latest is None:
            raise ValueError("本地没有原始日线")
        now = cn_now()
        completed_through = now.date() if now.hour >= 15 else now.date() - timedelta(days=1)
        latest = min(latest, completed_through)
        available_days = calendar.sessions(start=max(calendar.coverage_start, latest - timedelta(days=1100)),
                                           end=latest)
        if len(available_days) < 2 or (start is None and len(available_days) < sessions + 1):
            raise ValueError("日历覆盖不足，无法预留次日强制退出行情")
        end = end or available_days[-2]
        if end not in available_days or end >= available_days[-1]:
            raise ValueError("结束日须为有下一交易日完成行情的交易日")
        end_index = available_days.index(end)
        if start is None and end_index + 1 < sessions:
            raise ValueError("开始日期超出已加载日历范围，请缩短研究区间")
        start = start or available_days[end_index - sessions + 1]
        if start not in available_days or start > end:
            raise ValueError("开始日必须是覆盖内的交易日且不晚于结束日")
        exit_tail = available_days[end_index + 1]
        # A longer audit prefix detects already-known bad factors before the
        # shape warmup. It is an explicit bounded audit, not a full-history claim.
        audit_start = max(calendar.coverage_start, start - timedelta(days=900))
        days = calendar.sessions(start=audit_start, end=exit_tail)
        signature = reader.input_signature(exit_tail)
        print(json.dumps({"stage": "reading_raw", "start": str(start), "end": str(end),
                          "exit_tail": str(exit_tail), "audit_start": str(audit_start)}), flush=True)
        raw = reader.raw(None, exit_tail, start=audit_start)
        symbols = raw["symbol"].unique().sort().to_list()
        factors = reader.factors(symbols, exit_tail)
        instruments = repo.get_instruments()
        execution_raw = raw.filter(pl.col("date").is_between(start, exit_tail))
        reports = {}
        for pattern, specification in parameters.items():
            print(json.dumps({"stage": "evaluating", "pattern": pattern}), flush=True)
            candidates, audit = prepare_first_board_ideal_candidates(
                raw, factors, instruments, days, pattern=pattern,
                params=specification["params"], start=start, end=end,
            )
            result = evaluate_first_board_ideal(
                candidates, execution_raw, days, stop_loss_pct=.05,
                commission_pct=.0003, stamp_tax_pct=.0005,
            )
            reports[pattern] = {**specification, "input_audit": audit, **result}
            print(json.dumps({"stage": "evaluated", "pattern": pattern,
                              "summary": result["summary"]}, ensure_ascii=False, default=str), flush=True)
        if signature != reader.input_signature(exit_tail):
            raise ValueError("研究期间行情文件发生变化，本次结果不保存，请重新运行")
        if before != _config_signature(data_dir):
            raise ValueError("研究期间策略或工作台参数发生变化，本次结果不保存")
        return {
            "schema_version": 1,
            "model": "first_board_ideal_next_session_high_stop_first",
            "not_executable_backtest": True,
            "start": start.isoformat(), "end": end.isoformat(),
            "exit_tail": exit_tail.isoformat(), "audit_start": audit_start.isoformat(),
            "calendar": "bundled_official_exchange_calendar",
            "signal_sessions": sum(start <= day <= end for day in days),
            "price_source": "local_raw_daily",
            "source_signature_sha256": hashlib.sha256(
                json.dumps(signature, sort_keys=True).encode()).hexdigest(),
            "source_signature": signature,
            "config_unchanged": True,
            "limitations": [
                "当天形态资格来自前一交易日，触板后假设按涨停价买入，不验证排队成交。",
                "下一交易日必须退出；缺行情不能伪造卖出，也不顺延到下一条个股行情。",
                "开盘已低于止损取开盘价，否则触及止损优先，未触及取事后最高价。",
                "日线不证明高点与止损的先后顺序；该约定不是可执行交易策略。",
                "股票范围使用当前名称和上市资料，未复原历史ST及退市股票范围。",
                "原价研究排除含公司事件的窗口；已识别异常因子从异常日起持续隔离。",
                "只使用形态参数，不复现工作台盘中板块、环境、换手和优先分门控。",
                "独立样本收益不等于账户收益，未复利累加，不自动优化或写回参数。",
            ],
            "reports": reports,
        }
    finally:
        store.db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="首板当天涨停价买入、次日必卖的理想价格研究")
    parser.add_argument("--data-dir", type=Path, default=settings.data_dir)
    parser.add_argument("--start", type=date.fromisoformat)
    parser.add_argument("--end", type=date.fromisoformat)
    parser.add_argument("--sessions", type=int, default=60)
    parser.add_argument("--params", type=Path, help="可选JSON；platform/oversold/trend下填写形态参数，不写回")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("输出文件已存在，请使用新文件名")
    explicit = json.loads(args.params.read_text(encoding="utf-8")) if args.params else {}
    if not isinstance(explicit, dict):
        parser.error("参数文件必须是JSON对象")
    report = run_research(args.data_dir, start=args.start, end=args.end,
                          sessions=args.sessions, explicit_params=explicit)
    serialized = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False, default=str)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as handle:
        handle.write(serialized)
    print(json.dumps({"complete": str(args.output), "config_unchanged": True}, ensure_ascii=False))


if __name__ == "__main__":
    main()
