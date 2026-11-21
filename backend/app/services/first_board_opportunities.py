"""首板机会记录: 把某日某规则版本的事件按股票聚合成一行, 只读、不依赖输入顺序。"""
from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime

from app.market_time import CN_TZ


def _stamp(event: dict) -> datetime | None:
    ts = event.get("ts")
    if type(ts) is not int:
        return None
    try:
        return datetime.fromtimestamp(ts / 1000, CN_TZ)
    except (ValueError, OverflowError, OSError):
        return None


def _row(symbol: str, items: list[tuple[datetime, dict]]) -> dict:
    # (ts, pattern, id) 排序: 与输入顺序无关, 同毫秒提示取形态字典序最小, 与首次提示排名一致。
    items = sorted(items, key=lambda item: (item[0], item[1].get("pattern", ""), item[1]["id"]))
    patterns: list[dict] = []
    for _, event in items:
        if all(seen["pattern"] != event["pattern"] for seen in patterns):
            patterns.append({"pattern": event["pattern"], "pattern_label": event.get("pattern_label")})
    alert = next(((at, event) for at, event in items if event.get("event_type") == "buy_candidate"), None)
    touch = next(((at, event) for at, event in items if event.get("event_type") == "sealed"), None)
    broken_at = resealed_at = None
    if touch:
        later = [(at, event) for at, event in items if (at, event) != touch and at >= touch[0]]
        broken = next((at for at, event in later if event.get("event_type") == "broken"), None)
        if broken:
            broken_at = broken.isoformat()
            again = next((at for at, event in later if at > broken and event.get("event_type") == "sealed"), None)
            resealed_at = again.isoformat() if again else None
    first_at, first = items[0]
    last_at, last = items[-1]
    return {
        "symbol": symbol, "name": first.get("name"), "patterns": patterns,
        "first_alert": {"at": alert[0].isoformat(), "price": alert[1].get("price"),
                        "pattern": alert[1]["pattern"], "event_id": alert[1]["id"]} if alert else None,
        "first_touch": {"at": touch[0].isoformat(), "price": touch[1].get("price")} if touch else None,
        "broken": broken_at, "resealed": resealed_at,
        "last_state": {"event_type": last.get("event_type"), "at": last_at.isoformat()},
        "first_record": {"at": first_at.isoformat(), "event_type": first.get("event_type"),
                         "blocked_reasons": list(first.get("blocked_reasons") or [])},
        "alerted": alert is not None, "sealed_unalerted": touch is not None and alert is None,
        "event_count": len(items),
    }


def aggregate_opportunities(events: Iterable[dict], *, day: date, revision: int) -> dict:
    groups: dict[str, list[tuple[datetime, dict]]] = {}
    skipped = 0
    for event in events:
        if event.get("revision") != revision or event.get("pattern") == "exit":
            continue
        at = _stamp(event)
        if at is None:
            skipped += 1
            continue
        groups.setdefault(event["symbol"], []).append((at, event))
    rows = sorted((_row(symbol, items) for symbol, items in groups.items()),
                  key=lambda row: (row["first_record"]["at"], row["symbol"]))
    return {
        "day": day.isoformat(), "revision": revision, "rows": rows,
        "summary": {"stock_count": len(rows), "alerted_count": sum(row["alerted"] for row in rows),
                    "touched_count": sum(row["first_touch"] is not None for row in rows),
                    "sealed_unalerted_count": sum(row["sealed_unalerted"] for row in rows),
                    "skipped_count": skipped},
    }
