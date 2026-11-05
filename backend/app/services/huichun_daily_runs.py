"""Compact per-session run evidence, committed atomically with the scan snapshot."""

from __future__ import annotations

import hashlib
from datetime import date, datetime
from pathlib import Path

from app.services.huichun_calendar import load_market_calendar


def _sessions(path: Path | None, start: date, end: date) -> list[date] | None:
    if path is None:
        return None
    if start > end:
        return []
    try:
        return load_market_calendar(path, start=start, end=end)
    except (OSError, ValueError):
        return None


def record_waiting(snapshot, day, revision, quality, reason, now, *, recheck=False) -> bool:
    ledger = snapshot["daily_runs"]
    if str(day) < ledger["since"]:
        return False
    row = ledger["days"].setdefault(str(day), {})
    if not recheck and str(revision) in row.get("completed", {}):
        return False
    evidence = {"rule_revision": revision, "publication_status": quality, "reason": reason, "recheck": recheck}
    previous = row.get("attempt", {})
    if all(previous.get(key) == value for key, value in evidence.items()):
        return False
    row["attempt"] = {**evidence, "last_attempt_at": now.isoformat()}
    return True


def record_scan(snapshot, scan, controls, now: datetime, path, active_revision, input_digest, *, trigger="manual"):
    ledger = snapshot["daily_runs"]
    revision = scan["rule_revision"]
    today, stamp = now.date(), now.isoformat()
    start = max(date.fromisoformat(ledger["since"]), date.fromisoformat(scan["start_date"]))
    end = min(today, date.fromisoformat(scan["end_date"]))
    sessions = _sessions(path, start, end)
    if sessions is None:
        return
    quality = scan["coverage"].get("publication_status")
    for day in sessions:
        label = str(day)
        if day < today:
            row = ledger["days"].setdefault(label, {})
            row["backfilled_at"] = stamp
            continue
        if scan.get("observation_date") != label:
            continue
        if revision != active_revision:
            record_waiting(snapshot, day, active_revision, "superseded", "规则已更新，等待按新版本扫描", now)
            continue
        if quality != "ready":
            previous = ledger["days"].get(label, {}).get("completed", {}).get(str(revision), {})
            record_waiting(snapshot, day, revision, quality, quality_reason(scan["coverage"]), now,
                           recheck=bool(previous) and previous.get("input_digest") != input_digest)
            continue
        pools = (
            [r for r in scan["candidates"] if r["signal_date"] == label],
            [r for r in controls if r["signal_date"] == label],
            [r for r in scan["observations"] if r["observation_date"] == label],
        )
        fields = ("id", "baseline_fingerprint", "raw_close", "g_date", "d_date", "rally_return",
                  "zero_distance", "gap_distance", "first_watch_date", "volatility_20")
        payload = [[tuple(r.get(k) for k in fields) for r in sorted(pool, key=lambda r: r["id"])]
                   for pool in pools]
        fingerprint = hashlib.sha256(repr(payload).encode()).hexdigest()
        row = ledger["days"].setdefault(label, {})
        versions = row.setdefault("completed", {})
        prev = versions.get(str(revision), {})
        row.pop("attempt", None)
        if prev.get("fingerprint") == fingerprint:
            prev["input_digest"] = input_digest
            continue
        versions[str(revision)] = {
            "completed_at": stamp, "first_completed_at": prev.get("first_completed_at", stamp),
            "first_scan_id": prev.get("first_scan_id", scan["id"]), "scan_id": scan["id"],
            "trigger": trigger, "first_trigger": prev.get("first_trigger", trigger),
            "confirmed_count": len(pools[0]), "control_count": len(pools[1]),
            "pending_count": len(pools[2]), "fingerprint": fingerprint,
            "correction_count": prev.get("correction_count", -1) + 1, "input_digest": input_digest,
        }


def quality_reason(coverage):
    checks = coverage.get("publication_checks", {})
    if checks.get("known_raw_issue"):
        return "当日日线有待修复记录"
    if n := checks.get("unfinalized_symbols"):
        return f"{n} 只股票的日线尚未核准"
    return {
        "incomplete": "有效收盘行情覆盖不足", "stale": "尚无目标交易日行情",
        "calendar_unverified": "独立交易日历不可用", "before_close": "尚未到盘后核准时间",
    }.get(coverage.get("publication_status"), "等待当日有效收盘扫描")


def daily_summary(ledger, path, today: date, revision: int, limit: int):
    sessions = _sessions(path, date.fromisoformat(ledger["since"]), today)
    response = {"tracking_since": ledger["since"], "calendar_status":
                "verified" if sessions is not None else "unavailable", "records": []}
    # Without a calendar we can show an actual attempt, but cannot infer a missed session.
    dates = [str(d) for d in sessions] if sessions is not None else sorted(
        day for day, row in ledger["days"].items() if row.get("completed") or (day == str(today) and row.get("attempt"))
    )
    for label in reversed(dates[-limit:]):
        current = label == str(today)
        row = ledger["days"].get(label, {})
        versions = row.get("completed", {})
        revisions = sorted(map(int, versions))
        attempt = row.get("attempt", {})
        chosen = revision if current else (revisions[-1] if revisions else None)
        complete = versions.get(str(chosen))
        if current and attempt.get("recheck") and attempt["rule_revision"] == chosen:
            complete = None
        reason = attempt.get("reason")
        if not complete and current and attempt.get("rule_revision") != revision:
            reason = "等待按当前规则扫描" if revisions else "等待当日盘后扫描"
        entry = {
            "date": label, "status": "completed" if complete else "waiting" if current else "missed",
            "publication_status": "ready" if complete else attempt.get("publication_status"),
            "reason": None if complete else reason,
            "rule_revision": chosen if (current or complete) else attempt.get("rule_revision"),
            "completed_revisions": revisions, "confirmed_count": None, "control_count": None,
            "pending_count": None, "completed_at": None, "first_completed_at": None,
            "trigger": None, "scan_id": None,
            "correction_count": 0, "last_attempt_at": attempt.get("last_attempt_at"),
            "backfilled_at": row.get("backfilled_at"),
        }
        if complete:
            entry.update({k: complete[k] for k in (
                "confirmed_count", "control_count", "pending_count", "first_completed_at",
                "completed_at", "correction_count",
            )})
            entry.update(trigger=complete.get("trigger"), scan_id=complete.get("scan_id"))
            entry["last_attempt_at"] = max(complete["completed_at"], entry["last_attempt_at"] or "")
        response["records"].append(entry)
    return response
