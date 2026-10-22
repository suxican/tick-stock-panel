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
from datetime import datetime
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

    def capital_observations(self, report_id: str, limit: int = 60) -> list[dict]:
        """Read a bounded observation history; frozen plans are never rewritten."""
        self.get(report_id)
        limit = max(1, min(60, limit))
        paths = sorted(
            (path for path in self.root.glob(f"{report_id}.capital.*.json")
             if re.fullmatch(rf"{report_id}\.capital\.[0-9a-f]{{32}}", path.stem)),
            key=lambda path: (path.stat().st_mtime_ns, path.name), reverse=True,
        )[:limit]
        return self._read_capital_paths(report_id, paths)

    def execution_capital_observations(self, report_id: str) -> list[dict]:
        """Never silently omit an early veto from an execution replay."""
        self.get(report_id)
        paths = sorted(
            (path for path in self.root.glob(f"{report_id}.capital.*.json")
             if re.fullmatch(rf"{report_id}\.capital\.[0-9a-f]{{32}}", path.stem)),
            key=lambda path: (path.stat().st_mtime_ns, path.name),
        )
        if len(paths) > 400:
            raise ValueError("资金观察超过本次有界重放窗口")
        return self._read_capital_paths(report_id, paths)

    def _read_capital_paths(self, report_id: str, paths: list[Path]) -> list[dict]:
        observations = []
        for path in paths:
            try:
                envelope = json.loads(path.read_text(encoding="utf-8"))
                item = envelope["observation"]
                if (envelope.get("schema_version") != 1 or not isinstance(item, dict)
                        or item.get("report_id") != report_id
                        or item.get("id") != path.stem.rsplit(".", 1)[1]):
                    raise ValueError("unsupported observation")
                observations.append(item)
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("资金观察记录损坏或版本不受支持") from exc
        return observations

    def save_capital_observation(self, report_id: str, observation: dict) -> dict:
        self.get(report_id)
        item = deepcopy(observation)
        item.update(id=uuid4().hex, report_id=report_id)
        envelope = {"schema_version": 1, "observation": item}
        content = json.dumps(envelope, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        with self._lock:
            path = self._path(report_id, f".capital.{item['id']}")
            if path.exists():
                raise RuntimeError("资金观察编号冲突")
            atomic_write_text(path, content)
        return item

    def ecology_snapshots(self, *, as_of: str, before: datetime, limit: int = 20,
                         warnings: list[str] | None = None) -> list[dict]:
        """Bounded original archives; never reconstruct past memberships today."""
        if before.tzinfo is None:
            raise ValueError("历史归档截止时间须包含时区")
        paths = sorted(
            (path for path in self.root.glob("mg_*.json")
             if re.fullmatch(r"mg_[0-9a-f]{32}", path.stem)),
            key=lambda path: path.stat().st_mtime_ns, reverse=True,
        )[:60]
        result = []
        for path in paths:
            try:
                snapshot = self.snapshot(path.stem)
            except (ValueError, OSError):
                if warnings is not None and not warnings:
                    warnings.append("部分历史观察归档不可读, 已跳过对应比较; 原始文件未修改。")
                continue
            try:
                stamp = datetime.fromisoformat(snapshot.get("cutoff", ""))
            except (TypeError, ValueError):
                continue
            if (stamp.tzinfo is not None and stamp < before
                    and snapshot.get("as_of", as_of) < as_of):
                result.append(snapshot)
        # Preserve duplicate-day observations. The model rejects ambiguous
        # comparisons instead of selecting whichever happens to look stronger.
        return sorted(result, key=lambda row: (row["as_of"], row["cutoff"]), reverse=True)[:max(1, min(20, limit))]

    def save_model_evaluation(self, report_id: str, evaluation: dict, *, execution_inputs: dict | None = None) -> None:
        """Append a research evaluation; previous outcomes and plans survive."""
        self.get(report_id)
        self._check_model_evaluation(report_id, evaluation)
        envelope = {"schema_version": 1, "evaluation": deepcopy(evaluation),
                    "execution_inputs": deepcopy(execution_inputs)}
        content = json.dumps(envelope, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        with self._lock:
            path = self._path(report_id, f".model.{uuid4().hex}")
            if path.exists():
                raise RuntimeError("模型评估编号冲突")
            atomic_write_text(path, content)

    @staticmethod
    def _check_model_evaluation(report_id: str, item: dict) -> datetime:
        if not isinstance(item, dict) or item.get("report_id") != report_id:
            raise ValueError("模型评估与报告不匹配")
        try:
            stamp = datetime.fromisoformat(item["evaluated_at"])
            if stamp.tzinfo is None:
                raise ValueError("missing timezone")
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("模型评估时间无效") from exc
        return stamp

    def _read_model_evaluation(self, path: Path) -> dict:
        try:
            envelope = json.loads(path.read_text(encoding="utf-8"))
            if envelope.get("schema_version") != 1:
                raise ValueError("unsupported schema")
            item = envelope["evaluation"]
            self._check_model_evaluation(path.name.split(".", 1)[0], item)
            return item
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise ValueError("模型评估记录损坏或版本不受支持") from exc

    def model_evaluations(self, report_id: str, limit: int = 60) -> list[dict]:
        self.get(report_id)
        paths = sorted(
            (path for path in self.root.glob(f"{report_id}.model.*.json")
             if re.fullmatch(rf"{report_id}\.model\.[0-9a-f]{{32}}", path.stem)),
            key=lambda path: (path.stat().st_mtime_ns, path.name), reverse=True,
        )[:60]
        return sorted((self._read_model_evaluation(path) for path in paths),
                      key=lambda item: datetime.fromisoformat(item["evaluated_at"]), reverse=True)[:max(1, min(60, limit))]

    def execution_history(self, *, before: datetime, exclude_report_id: str | None = None,
                          limit: int = 200) -> list[dict]:
        """Bounded known evaluations, preserving first-mature result timestamps."""
        if before.tzinfo is None:
            raise ValueError("历史评估截止时间须包含时区")
        paths = sorted(
            (path for path in self.root.glob("mg_*.model.*.json")
             if re.fullmatch(r"mg_[0-9a-f]{32}\.model\.[0-9a-f]{32}", path.stem)),
            key=lambda path: (path.stat().st_mtime_ns, path.name), reverse=True,
        )[:400]
        results = []
        for path in paths:
            report_id = path.name.split(".", 1)[0]
            if report_id == exclude_report_id:
                continue
            item = self._read_model_evaluation(path)
            stamp = datetime.fromisoformat(item["evaluated_at"])
            execution = item.get("execution")
            if stamp > before or not isinstance(execution, dict):
                continue
            results.append(item)
        ordered = sorted(results, key=lambda item: datetime.fromisoformat(item["evaluated_at"]), reverse=True)
        report_ids = list(dict.fromkeys(item["report_id"] for item in ordered))[:max(1, min(200, limit))]
        # The adaptation engine deduplicates labels in chronological order.
        # Collapsing to the latest refresh here would erase when an outcome
        # first became available and introduce hindsight into training folds.
        return [item["execution"] for item in ordered if item["report_id"] in report_ids]
