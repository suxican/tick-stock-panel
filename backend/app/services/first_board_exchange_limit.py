"""Pure synthesis of exchange limit-stream evidence for a first-board row.

No IO and no clock reads (``now`` is passed in). The stream is positive
evidence only: event recall is partial, so a missing event never contradicts
a sealed quote. Only a stream-confirmed seal against a broken quote conflicts.
"""
from __future__ import annotations

from datetime import datetime

from app.services.meoz_limit_stream import LimitState


def _clock(at: datetime | None) -> str:
    """" HH:MM:SS" with a leading space, or "" when the event time is unknown."""
    return at.strftime(" %H:%M:%S") if isinstance(at, datetime) else ""


def exchange_limit_evidence(quote_state: str, limit: LimitState | None,
                            stream_status: dict | None, *, now: datetime) -> dict:
    """Combine quote-derived limit state with the exchange stream's state.

    Status "none" means "no evidence", never negative evidence.
    """
    connected = isinstance(stream_status, dict) and stream_status.get("state") == "connected"
    gaps = stream_status.get("seq_gaps") if isinstance(stream_status, dict) else None
    degraded = isinstance(gaps, (int, float)) and gaps > 0
    result = {"status": "unknown", "at": None, "confirmed_opens": 0, "open_times": None,
              "conflict": False, "degraded": bool(degraded), "note": None}
    if not connected:
        return result

    status = limit.status if limit is not None else "none"
    confirmed = int(limit.confirmed_opens or 0) if limit is not None else 0
    n = max((limit.pool_open_times if limit is not None else None) or 0, confirmed)
    at = limit.at if limit is not None else None
    result.update(status=status, at=at.isoformat() if at else None,
                  confirmed_opens=confirmed, open_times=n if limit is not None else None)

    if quote_state == "sealed":
        if status == "sealed_confirmed":
            note = f"交易所确认封板{_clock(at)}"
            if n > 0:
                note += f"，当日开板 {n} 次"
            result["note"] = note
        elif status == "touching":
            result["note"] = "刚触及涨停，封板未满 60 秒"
    elif quote_state == "broken":
        if status == "opened":
            prefix = "最近一次交易所确认开板" + _clock(at) if at else "交易所确认开板"
            result["note"] = f"{prefix}，当日开板 {max(n, 1)} 次"
        elif status == "sealed_confirmed":
            result["conflict"] = at is not None and (now - at).total_seconds() <= 60
    return result
