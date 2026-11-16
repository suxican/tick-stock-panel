"""环境结论存档: 记录每个交易日系统实际给出的结论, 供事后复盘是否按预设执行。

结论本身每次现算(用户改预设、重算环境历史都会改变它), 所以要在关键时点留档:
盘后定版(trigger="pipeline")与用户保存预设(trigger="preset")各追加一版。

<data_dir>/user_data/environment_conclusion/<YYYY-MM-DD>.json
  {"date": "...", "versions": [{"recorded_at": ISO, "trigger": "...", "conclusion": {...}}]}

只追加不覆盖; 与上一版决策内容相同则不追加; 文件损坏时跳过, 绝不重写。
"""
from __future__ import annotations

import json
import logging
import re
import threading
from pathlib import Path

from app.market_time import cn_now
from app.services.fs_utils import atomic_write_text

logger = logging.getLogger(__name__)

ARCHIVE_DIR = "environment_conclusion"
_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")
# 决定当天仓位的字段; 证据刷新、存档摘要不构成新版本。
_DECISION_KEYS = ("cell", "missing_reason", "plan", "gate", "effective_cap_pct", "limited_by")
_LOCK = threading.Lock()


def _path(data_dir: Path, day: str) -> Path:
    return Path(data_dir) / "user_data" / ARCHIVE_DIR / f"{day}.json"


def _read(path: Path) -> dict | None:
    """None = 不存在; 抛 ValueError = 损坏。"""
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("versions"), list):
        raise ValueError("环境结论存档结构无效")
    return data


def load_day(data_dir: Path, day: str) -> dict | None:
    if not isinstance(day, str) or not _DAY.fullmatch(day):
        return None
    try:
        return _read(_path(data_dir, day))
    except (OSError, ValueError) as exc:
        logger.warning("environment conclusion archive unreadable (%s): %s", day, exc)
        return None


def append_version(data_dir: Path, conclusion: dict, trigger: str) -> bool:
    """追加一版; 返回是否写入。"""
    day = conclusion.get("date")
    if not isinstance(day, str) or not _DAY.fullmatch(day):
        return False
    path = _path(data_dir, day)
    entry = {key: conclusion.get(key) for key in _DECISION_KEYS}
    with _LOCK:
        try:
            data = _read(path)
        except (OSError, ValueError) as exc:
            logger.warning("environment conclusion archive corrupt, left untouched (%s): %s", day, exc)
            return False
        data = data or {"date": day, "versions": []}
        last = data["versions"][-1]["conclusion"] if data["versions"] else None
        if isinstance(last, dict) and {key: last.get(key) for key in _DECISION_KEYS} == entry:
            return False
        stored = {key: value for key, value in conclusion.items() if key != "archive"}
        data["versions"].append({"recorded_at": cn_now().isoformat(), "trigger": trigger, "conclusion": stored})
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2))
    return True


def summarize_day(data_dir: Path, day: str | None) -> dict | None:
    data = load_day(data_dir, day) if day else None
    if not data or not data["versions"]:
        return None
    last = data["versions"][-1]
    conclusion = last.get("conclusion") if isinstance(last.get("conclusion"), dict) else {}
    return {"versions": len(data["versions"]), "latest_at": last.get("recorded_at"),
            "latest_effective_cap_pct": conclusion.get("effective_cap_pct")}
