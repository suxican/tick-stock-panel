"""Read and freeze the existing exchange-calendar contract for game reports."""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, time, timedelta
from pathlib import Path

from app.market_time import CN_TZ
from app.services.huichun_calendar import load_market_calendar


def _calendar_paths(data_dir: Path) -> list[Path]:
    return [
        data_dir / "user_data" / "market_game" / "market_calendar.json",
        data_dir / "user_data" / "huichun" / "market_calendar.json",
        Path(__file__).parents[3] / "docs/策略/回春策略/evidence/exchange_calendar_2019_2026.json",
    ]


def load_game_calendar(data_dir: Path, *, start: date, cutoff: datetime,
                       through: date | None = None, path: Path | None = None) -> dict:
    """Return bounded ISO sessions, with no network, migration, or file writes.

    ``exit_sessions`` contains the first three complete sessions starting at
    ``entry_session``; it is an observation window, not three executable exits.
    Publication dates have day precision, so same-day sources cannot prove that
    the schedule was already known at the observation time.
    """
    cutoff = cutoff.astimezone(CN_TZ)
    through = through or (cutoff.date() if cutoff.time() >= time(15, 10) else cutoff.date() - timedelta(days=1))
    result = {
        "state": "unavailable", "verified": False, "future_verified": False,
        "source": None, "version": None, "coverage_start": None, "coverage_end": None,
        "known_by": None, "sources": [], "sessions": [], "expected_latest_session": None,
        "entry_session": None, "exit_sessions": [], "missing_sessions": [], "limitations": [],
    }
    selected = path or next((candidate for candidate in _calendar_paths(Path(data_dir)) if candidate.is_file()), None)
    if selected is None or not selected.is_file():
        result["limitations"].append("独立交易日历缺失; 不以工作日或自然日推定交易日期, 暂不生成候选。")
        return result
    try:
        content_bytes = selected.read_bytes()
        content = json.loads(content_bytes.decode("utf-8-sig"))
        first = date.fromisoformat(content["coverage_start"])
        last = date.fromisoformat(content["coverage_end"])
        result.update({
            "source": "独立交易所日历文件", "version": hashlib.sha256(content_bytes).hexdigest()[:24],
            "coverage_start": first.isoformat(), "coverage_end": last.isoformat(),
        })
        if first > start or last < through:
            result["state"] = "out_of_coverage"
            result["limitations"].append("独立交易日历未覆盖所需历史或最新收盘日期, 暂不生成候选。")
            return result
        # The existing loader validates duplicate keys, all source URLs, closure
        # references and interval coverage. Never invent a second calendar parser.
        sessions = load_market_calendar(selected, start=start, end=min(last, cutoff.date() + timedelta(days=45)))
        if selected.read_bytes() != content_bytes:
            raise ValueError("calendar changed during read")
        published = max(date.fromisoformat(source["published_on"]) for source in content["sources"])
        result["sources"] = content["sources"]
        result["known_by"] = published.isoformat()
        if published >= cutoff.date():
            result["state"] = "publication_unverified"
            result["limitations"].append("日历含观察当日或之后发布的来源, 不能证明当时已知, 不给出未来入场日期。")
            return result
        result["state"] = "verified"
        result["verified"] = True
        result["sessions"] = [day.isoformat() for day in sessions]
        completed = [day for day in sessions if day <= through]
        result["expected_latest_session"] = completed[-1].isoformat() if completed else None
        future = [day for day in sessions if datetime.combine(day, time(9, 30), tzinfo=CN_TZ) > cutoff][:3]
        if len(future) == 3:
            result["future_verified"] = True
            result["entry_session"] = future[0].isoformat()
            result["exit_sessions"] = [day.isoformat() for day in future]
        else:
            result["limitations"].append("日历未覆盖观察时点之后三个完整交易日, 不推定入场及后续观察日期。")
        result["limitations"].append("日历来源地址与发布时间通过本地契约校验; 本次没有联网重新核对公告内容。")
    except (OSError, ValueError, TypeError, KeyError, OverflowError):
        result["state"] = "invalid"
        result["limitations"].append("独立交易日历格式、来源或读取一致性校验失败, 暂不生成候选。")
    return result
