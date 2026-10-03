"""Immutable prediction files, with separate explanations and outcome observations.

There is deliberately no automatic retention limit: deleting an old prediction
would also remove evidence needed to assess the strategy. No existing user data
is migrated or rewritten by this store.
"""
from __future__ import annotations

import json
import re
import threading
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

from app.market_time import cn_now
from app.services.fs_utils import atomic_write_text


class MarketGameReportStore:
    def __init__(self, data_dir: Path) -> None:
        self.root = Path(data_dir) / "user_data" / "market_game"
        self._lock = threading.Lock()

    def _path(self, report_id: str, suffix: str = "") -> Path:
        if not re.fullmatch(r"mg_[0-9a-f]{32}", report_id):
            raise ValueError("无效的博弈报告编号")
        path = self.root / f"{report_id}{suffix}.json"
        if path.resolve().parent != self.root.resolve():
            raise ValueError("博弈报告路径无效")
        return path

    def _read(self, report_id: str) -> dict:
        path = self._path(report_id)
        if not path.exists():
            raise FileNotFoundError("博弈报告不存在")
        try:
            envelope = json.loads(path.read_text(encoding="utf-8"))
            if (
                envelope.get("schema_version") != 1
                or not isinstance(envelope.get("report"), dict)
                or not isinstance(envelope.get("snapshot"), dict)
                or envelope["report"].get("id") != report_id
            ):
                raise ValueError("unsupported report")
            return envelope
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValueError("博弈报告损坏或版本不受支持") from exc

    def save(self, report: dict, snapshot: dict) -> dict:
        item = deepcopy(report)
        item["id"] = f"mg_{uuid4().hex}"
        item["created_at"] = cn_now().isoformat(timespec="seconds")
        envelope = {"schema_version": 1, "report": item, "snapshot": snapshot}
        content = json.dumps(envelope, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        with self._lock:
            self.root.mkdir(parents=True, exist_ok=True)
            path = self._path(item["id"])
            if path.exists():
                raise RuntimeError("报告编号冲突, 请重新生成")
            atomic_write_text(path, content)
        return item

    def get(self, report_id: str) -> dict:
        return self._read(report_id)["report"]

    def snapshot(self, report_id: str) -> dict:
        return self._read(report_id)["snapshot"]

    def list_reports(self, limit: int = 200) -> list[dict]:
        # Bound JSON reads on the list hot path; every older report remains
        # addressable by ID. Sidecar files never enter the prediction index.
        paths = sorted(
            (p for p in self.root.glob("mg_*.json") if re.fullmatch(r"mg_[0-9a-f]{32}", p.stem)),
            key=lambda p: p.stat().st_mtime_ns,
            reverse=True,
        )[:limit]
        fields = ("id", "as_of", "created_at", "summary", "quality", "research_only")
        reports = [self.get(path.stem) for path in paths]
        return [{key: report.get(key) for key in fields} for report in reports]

    def _sidecar(self, report_id: str, suffix: str, value: dict) -> None:
        self.get(report_id)
        content = json.dumps(value, ensure_ascii=False, allow_nan=False)
        with self._lock:
            atomic_write_text(self._path(report_id, suffix), content)

    def save_evaluation(self, report_id: str, evaluation: dict) -> None:
        self._sidecar(report_id, ".evaluation", evaluation)

    def save_explanation(self, report_id: str, content: str) -> None:
        self._sidecar(report_id, ".explanation", {"content": content})

    def get_explanation(self, report_id: str) -> str | None:
        self.get(report_id)
        path = self._path(report_id, ".explanation")
        if not path.exists():
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or not isinstance(value.get("content"), str):
            raise ValueError("报告解读记录损坏")
        return value["content"]
