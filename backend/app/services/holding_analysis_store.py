"""Versioned research snapshots; never writes inventory or trade records."""
from __future__ import annotations

import hashlib
import json
import os
import threading
from copy import deepcopy
from datetime import date
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.market_time import cn_now
from app.services.fs_utils import atomic_write_text

SCHEMA_VERSION = 1
MAX_REPORTS = 100
_locks_guard = threading.Lock()
_locks: dict[str, Any] = {}


class ResearchError(Exception):
    """Safe domain error shared by the research service and its HTTP boundary."""

    def __init__(
        self, message: str, status_code: int = 422, code: str = "invalid_research",
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code


def _json_text(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ResearchError("研究资料包含无法保存的数值或字段") from exc


def _reject_constant(value: str) -> None:
    raise ValueError("non-finite JSON value")


def _version_matches(current: int, expected: int) -> None:
    if type(expected) is not int or expected != current:
        raise ResearchError("资料已更新，请刷新后重试", 409, "version_conflict")


def _report_valid(report: Any) -> bool:
    if not isinstance(report, dict):
        return False
    rows = report.get("rows")
    if not (
        isinstance(report.get("source"), str) and report["source"]
        and (report.get("account_id") is None or isinstance(report["account_id"], str))
        and isinstance(report.get("input_version"), str) and report["input_version"]
        and isinstance(report.get("profile_version"), str)
        and isinstance(rows, list)
    ):
        return False
    keys = [row.get("key") for row in rows if isinstance(row, dict)]
    return (
        len(keys) == len(rows)
        and all(isinstance(key, str) and key for key in keys)
        and len(set(keys)) == len(keys)
    )


class HoldingAnalysisStore:
    """One fixed JSON path, shared lock across instances, no mutable cache.

    Plans embed their adopted row, so report retention cannot erase active
    evidence. Every mutation rereads under the same lock before atomic replace.
    """

    def __init__(self, data_dir: Path) -> None:
        self._path = Path(data_dir).resolve() / "user_data" / "holding_analysis" / "state.json"
        lock_key = os.path.normcase(str(self._path))
        with _locks_guard:
            self._lock = _locks.setdefault(lock_key, threading.RLock())

    def _read(self) -> dict:
        try:
            text = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {"schema_version": SCHEMA_VERSION, "profiles": {}, "reports": [], "plans": []}
        except (OSError, UnicodeError) as exc:
            raise ResearchError("研究资料读取失败，请检查存储状态", 500, "storage_io_error") from exc
        try:
            state = json.loads(text, parse_constant=_reject_constant)
        except (ValueError, RecursionError) as exc:
            raise ResearchError("研究资料文件损坏，已停止读写", 500, "storage_corrupt") from exc
        if (
            isinstance(state, dict) and type(state.get("schema_version")) is int
            and state["schema_version"] != SCHEMA_VERSION
        ):
            raise ResearchError("研究资料版本不受当前程序支持，已停止读写", 409, "unsupported_schema")
        if not self._state_valid(state):
            raise ResearchError("研究资料结构损坏，已停止读写", 500, "storage_corrupt")
        return state

    @staticmethod
    def _state_valid(state: Any) -> bool:
        if not (
            isinstance(state, dict)
            and type(state.get("schema_version")) is int
            and state["schema_version"] == SCHEMA_VERSION
            and isinstance(state.get("profiles"), dict)
            and isinstance(state.get("reports"), list)
            and isinstance(state.get("plans"), list)
        ):
            return False
        for key, entry in state["profiles"].items():
            if not (
                isinstance(key, str) and key and isinstance(entry, dict)
                and isinstance(entry.get("profile"), dict)
                and type(entry.get("version")) is int and entry["version"] > 0
            ):
                return False
        for report in state["reports"]:
            if not _report_valid(report) or not isinstance(report.get("id"), str) or not report["id"]:
                return False
        for plan in state["plans"]:
            if not (
                isinstance(plan, dict) and isinstance(plan.get("id"), str) and plan["id"]
                and type(plan.get("version")) is int and plan["version"] > 0
                and isinstance(plan.get("status"), str)
                and plan["status"] in {"adopted", "invalidated"}
                and isinstance(plan.get("row"), dict)
                and isinstance(plan["row"].get("key"), str) and plan["row"]["key"]
                and isinstance(plan.get("report_id"), str) and plan["report_id"]
                and isinstance(plan.get("input_version"), str) and plan["input_version"]
                and isinstance(plan.get("profile_version"), str)
                and isinstance(plan.get("profile_keys"), list)
                and all(isinstance(key, str) and key for key in plan["profile_keys"])
                and isinstance(plan.get("source"), str) and plan["source"]
                and (plan.get("account_id") is None or isinstance(plan["account_id"], str))
                and isinstance(plan.get("created_at"), str)
                and isinstance(plan.get("expires_on"), str)
                and isinstance(plan.get("alert_rule_ids"), list)
                and all(isinstance(item, str) and item for item in plan["alert_rule_ids"])
            ):
                return False
            bindings = plan.get("alert_bindings", {})
            if not isinstance(bindings, dict) or any(
                rule_id not in plan["alert_rule_ids"] or not isinstance(status, str)
                or status not in {"pending", "active", "paused"}
                for rule_id, status in bindings.items()
            ):
                return False
            try:
                date.fromisoformat(plan["expires_on"])
            except ValueError:
                return False
        return all(
            len({item["id"] for item in state[name]}) == len(state[name])
            for name in ("reports", "plans")
        )

    def _write(self, state: dict) -> None:
        text = _json_text(state)
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(self._path, text)
        except OSError as exc:
            raise ResearchError("研究资料保存失败，原资料未更新", 500, "storage_io_error") from exc

    @staticmethod
    def _find(state: dict, collection: str, identifier: str) -> dict:
        for item in state[collection]:
            if item["id"] == identifier:
                return item
        label = "分析报告" if collection == "reports" else "操作计划"
        code = "report_not_found" if collection == "reports" else "plan_not_found"
        raise ResearchError(f"{label}不存在或已归档", 404, code)

    def profiles(self) -> dict:
        with self._lock:
            return deepcopy(self._read()["profiles"])

    def profile_version(self, keys: list[str]) -> str:
        with self._lock:
            return self._profile_version(self._read(), keys)

    @staticmethod
    def _profile_version(state: dict, keys: list[str]) -> str:
        selected = {
            key: state["profiles"].get(key, {"profile": {}, "version": 0})
            for key in sorted(set(keys))
        }
        return hashlib.sha256(_json_text(selected).encode("utf-8")).hexdigest()

    @classmethod
    def _check_profiles(cls, state: dict, keys: list[str], expected: str) -> None:
        if cls._profile_version(state, keys) != expected:
            raise ResearchError("研究资料已变化，请重新分析后采纳", 409, "stale_profile")

    @staticmethod
    def _check_expiry(value: Any, today: date) -> date:
        try:
            expires = date.fromisoformat(value)
        except (TypeError, ValueError) as exc:
            raise ResearchError("计划缺少有效的截止日期", 422, "invalid_expiry") from exc
        if expires < today:
            raise ResearchError("分析计划已过期，请重新分析", 409, "expired_report")
        return expires

    def save_profile(self, key: str, profile: dict, expected_version: int) -> dict:
        if not isinstance(key, str) or not key or not isinstance(profile, dict):
            raise ResearchError("研究资料标识或内容无效")
        profile = deepcopy(profile)
        _json_text(profile)
        with self._lock:
            state = self._read()
            current = state["profiles"].get(key, {"version": 0})["version"]
            _version_matches(current, expected_version)
            entry = {"profile": profile, "version": current + 1}
            state["profiles"][key] = entry
            self._write(state)
            return deepcopy(entry)

    def save_report(self, report: dict) -> dict:
        report = deepcopy(report)
        if not _report_valid(report):
            raise ResearchError("分析报告缺少来源、版本或持仓条目")
        _json_text(report)
        with self._lock:
            state = self._read()
            report["id"] = f"har_{uuid4().hex}"
            report["created_at"] = cn_now().isoformat(timespec="seconds")
            state["reports"].insert(0, report)
            del state["reports"][MAX_REPORTS:]
            self._write(state)
            return deepcopy(report)

    def latest_report(self, source: str, account_id: str | None) -> dict | None:
        with self._lock:
            return deepcopy(next((
                report for report in self._read()["reports"]
                if report["source"] == source and report.get("account_id") == account_id
            ), None))

    def get_report(self, identifier: str) -> dict:
        with self._lock:
            return deepcopy(self._find(self._read(), "reports", identifier))

    def adopt_plan(self, report_id: str, row_key: str, expected_input_version: str) -> dict:
        with self._lock:
            state = self._read()
            now = cn_now()
            for existing in state["plans"]:
                if existing["report_id"] == report_id and existing["row"]["key"] == row_key:
                    if existing["input_version"] != expected_input_version:
                        raise ResearchError("持仓已变化，请重新分析", 409, "stale_report")
                    if existing["status"] != "adopted":
                        raise ResearchError("该计划已失效，请重新分析后采纳", 409, "plan_invalidated")
                    self._check_profiles(state, existing["profile_keys"], existing["profile_version"])
                    self._check_expiry(existing["expires_on"], now.date())
                    return deepcopy(existing)
            report = self._find(state, "reports", report_id)
            if report["input_version"] != expected_input_version:
                raise ResearchError("持仓已变化，请重新分析", 409, "stale_report")
            row = next((row for row in report["rows"] if row["key"] == row_key), None)
            if row is None:
                raise ResearchError("报告中不存在该持仓条目", 404, "row_not_found")
            draft = row.get("plan")
            if not isinstance(draft, dict) or draft.get("can_adopt") is not True:
                raise ResearchError("该条目暂不满足计划采纳条件", 409, "not_adoptable")
            expires = self._check_expiry(draft.get("expires_on"), now.date())
            profile_keys = [item["key"] for item in report["rows"]]
            self._check_profiles(state, profile_keys, report["profile_version"])
            if any(
                plan["status"] == "adopted" and plan["source"] == report["source"]
                and plan.get("account_id") == report.get("account_id")
                and plan["row"]["key"] == row_key for plan in state["plans"]
            ):
                raise ResearchError("该持仓已有已采纳计划，请先作废旧计划", 409, "active_plan_exists")
            plan = {
                "id": f"hap_{uuid4().hex}", "version": 1, "status": "adopted",
                "created_at": now.isoformat(timespec="seconds"),
                "generated_at": report.get("generated_at", report["created_at"]),
                "as_of_date": report.get("as_of_date"),
                "expires_on": expires.isoformat(), "report_id": report_id,
                "row": deepcopy(row), "input_version": report["input_version"],
                "profile_version": report["profile_version"], "profile_keys": profile_keys,
                "source": report["source"], "account_id": report.get("account_id"),
                "alert_rule_ids": [], "alert_bindings": {},
            }
            state["plans"].insert(0, plan)
            self._write(state)
            return deepcopy(plan)

    def all_plans(self) -> list[dict]:
        with self._lock:
            return deepcopy(self._read()["plans"])

    def list_plans(self, source: str, account_id: str | None) -> list[dict]:
        with self._lock:
            return deepcopy([
                plan for plan in self._read()["plans"]
                if plan["source"] == source and plan.get("account_id") == account_id
            ])

    def get_plan(self, identifier: str) -> dict:
        with self._lock:
            return deepcopy(self._find(self._read(), "plans", identifier))

    def invalidate_plan(self, identifier: str, expected_version: int) -> dict:
        with self._lock:
            state = self._read()
            plan = self._find(state, "plans", identifier)
            _version_matches(plan["version"], expected_version)
            if plan["status"] != "invalidated":
                plan["status"] = "invalidated"
                plan["version"] += 1
                plan["invalidated_at"] = cn_now().isoformat(timespec="seconds")
                self._write(state)
            return deepcopy(plan)

    def set_alert_rule(self, identifier: str, expected_version: int, rule_id: str) -> dict:
        """Persist pending ownership before a rule can be enabled externally."""
        if not isinstance(rule_id, str) or not rule_id:
            raise ResearchError("提醒规则标识无效")
        with self._lock:
            state = self._read()
            plan = self._find(state, "plans", identifier)
            _version_matches(plan["version"], expected_version)
            if plan["status"] != "adopted":
                raise ResearchError("该计划已失效，不能添加提醒", 409, "plan_invalidated")
            bindings = plan.setdefault("alert_bindings", {})
            if rule_id not in plan["alert_rule_ids"] or bindings.get(rule_id) != "pending":
                if rule_id not in plan["alert_rule_ids"]:
                    plan["alert_rule_ids"].append(rule_id)
                bindings[rule_id] = "pending"
                plan["version"] += 1
                self._write(state)
            return deepcopy(plan)

    def activate_alert_rule(self, identifier: str, expected_version: int, rule_id: str) -> dict:
        """Commit activation only after the external rule synchronization succeeds."""
        with self._lock:
            state = self._read()
            plan = self._find(state, "plans", identifier)
            _version_matches(plan["version"], expected_version)
            if plan["status"] != "adopted":
                raise ResearchError("该计划已失效，不能启用提醒", 409, "plan_invalidated")
            bindings = plan.get("alert_bindings", {})
            if rule_id not in plan["alert_rule_ids"] or bindings.get(rule_id) != "pending":
                raise ResearchError("该提醒尚未完成待绑定登记，请重新绑定", 409, "alert_not_pending")
            bindings[rule_id] = "active"
            plan["version"] += 1
            self._write(state)
            return deepcopy(plan)
