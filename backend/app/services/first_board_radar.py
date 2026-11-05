"""Optional, point-in-time radar evidence. Quote processing only consumes completed batches."""
from __future__ import annotations

import json
import logging
import math
import re
from datetime import datetime, time, timedelta
from pathlib import Path
from threading import Event, Lock, Thread

from app.data_providers import custom as custom_sources
from app.market_time import CN_TZ, cn_now, in_first_board_session
from app.services.fs_utils import atomic_write_text
from app.services.zzshare_home import read_home
from app.services.zzshare_research import PlateType, shared_reader

logger = logging.getLogger(__name__)
_AGE = timedelta(minutes=10)
_INTERVAL = timedelta(minutes=5)
_SYMBOL = re.compile(r"[0-9]{6}\.(SH|SZ|BJ)")


def _stamp(value):
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    return value.astimezone(CN_TZ) if isinstance(value, datetime) and value.utcoffset() is not None else None


def _fresh(source, fetched, now):
    source, fetched = _stamp(source), _stamp(fetched)
    return bool(source and fetched and source.date() == now.date() and source <= fetched <= now
                and now - source <= _AGE and in_first_board_session(source) and in_first_board_session(now))


def _valid_saved(record, day):
    received = _stamp(record["fetched_at"])
    rows = record["rows"]
    if not received or received.date().isoformat() != day or not rows or len(rows) > 100:
        return False
    ranks = set()
    for symbol, row in rows.items():
        rank = row["rank"]
        if (not _SYMBOL.fullmatch(symbol) or type(rank) is not int or not 1 <= rank <= 100
                or rank in ranks or not _fresh(row["source_at"], received, received)):
            return False
        ranks.add(rank)
    return True


def _hot_history(directory, packet, received):
    day = received.date().isoformat()
    fetched = _stamp(packet.get("fetched_at"))
    if (packet.get("kind") != "ths_hot_top" or packet.get("date") != day or packet.get("stale") is not False
            or not _fresh(fetched, received, received)):
        return []
    rows = {}
    ranks = set()
    for row in packet.get("rows", []):
        symbol, rank, source = row.get("symbol"), row.get("rank"), _stamp(row.get("updated_at"))
        if (not _SYMBOL.fullmatch(symbol or "") or symbol in rows or type(rank) is not int
                or not 1 <= rank <= 100 or rank in ranks or row.get("trade_date") != day
                or not source or source > fetched or not _fresh(source, received, received)):
            return []
        rows[symbol] = {"rank": rank, "source_at": source.isoformat()}
        ranks.add(rank)
    if not rows:
        return []
    # Immutable files preserve when we actually learned a ranking. A cached read
    # of the same source snapshot cannot manufacture an upward rank trend.
    directory = directory / day
    history = [json.loads(path.read_text(encoding="utf-8"))
               for path in sorted(directory.glob("*.json"))[-2:]]
    if (not all(_valid_saved(record, day) for record in history)
            or (len(history) == 2 and _stamp(history[0]["fetched_at"]) >= _stamp(history[1]["fetched_at"]))):
        raise ValueError("人气观测归档异常")
    if history and _stamp(history[-1]["fetched_at"]) > received:
        return []
    if history and history[-1]["rows"] == rows:
        return history
    if history:
        previous = history[-1]["rows"]
        if any(_stamp(rows[s]["source_at"]) < _stamp(previous[s]["source_at"])
               for s in rows.keys() & previous.keys()):
            return []
    if history and _stamp(history[-1]["fetched_at"]) >= received:
        return []
    record = {"fetched_at": received.isoformat(), "rows": rows}
    directory.mkdir(parents=True, exist_ok=True)
    atomic_write_text(directory / f"{received:%H%M%S%f}.json", json.dumps(record, allow_nan=False))
    return [*history[-1:], record]


def _rank_rows(history, now):
    if not history:
        return {}, False
    latest = history[-1]
    received = latest["fetched_at"]
    prior = history[-2] if len(history) > 1 else None
    result = {}
    for symbol, row in latest["rows"].items():
        if not _fresh(row["source_at"], received, now):
            continue
        previous = prior["rows"].get(symbol) if prior else None
        prior_rank = None
        if (previous and _fresh(previous["source_at"], prior["fetched_at"], _stamp(received))
                and _stamp(previous["source_at"]) < _stamp(row["source_at"])):
            prior_rank = previous["rank"]
        rank = row["rank"]
        result[symbol] = {"status": "ready", "rank": rank, "prior_rank": prior_rank,
                          "rank_gain": prior_rank - rank if prior_rank is not None else None,
                          "source_at": row["source_at"], "fetched_at": received}
    return result, len(result) == 100


def _official(root, now):
    # as_of makes this a local archive read, including at market-session boundaries.
    packet = read_home(root, now.date(), as_of=now)
    source, fetched = packet.get("source_at"), packet.get("fetched_at")
    if (packet.get("mode") != "intraday" or packet.get("state") != "ready" or packet.get("stale")
            or not _fresh(source, fetched, now)):
        return {}
    market_time = time.fromisoformat(packet["market_snapshot_at"])
    market = datetime.combine(now.date(), market_time, CN_TZ)
    if not _fresh(market, source, now):
        return {}
    result = {}
    for row in packet["rows"]:
        evidence = {"status": "ready", "source": "official_core", "tid": row["tid"], "name": row["name"],
                    "score": row["score"], "rank": row["rank"], "market_at": market.isoformat(),
                    "source_at": source, "fetched_at": fetched, "plate_type": None}
        for stock in row["core_stocks"]:
            result.setdefault(stock["symbol"], evidence)
    return result


class FirstBoardRadar:
    def __init__(self, data_dir: Path):
        self._root = Path(data_dir)
        self._history_dir = self._root / "user_data" / "first_board" / "radar" / "v1" / "hot"
        self._lock = Lock()
        self._stop = Event()
        self._worker: Thread | None = None
        self._next = None
        self._history = []
        self._official = {}
        self._plates = {}
        self._members = {}  # worker-owned; immutable date + provider type + plate identity

    def _plate(self, kind, now):
        day = now.date()
        packet = shared_reader.read("plates_rank", day, plate_type=kind)
        if (packet.get("date") != day.isoformat() or packet.get("stale") is not False
                or not packet["rows"]):
            return {}
        row = packet["rows"][0]  # provider order; one leading plate per category per poll
        source = row.get("updated_at")
        after_rank = cn_now()
        if (row["plate_type"] != int(kind) or row["trade_date"] != day.isoformat()
                or not _fresh(source, packet.get("fetched_at"), after_rank)
                or not isinstance(row["change_pct"], (int, float)) or not math.isfinite(row["change_pct"])
                or row["change_pct"] <= 0):
            return {}
        code = row["plate_code"]
        key = (day, kind, code)
        if key not in self._members:
            provider = custom_sources.get_provider("zzshare")
            members = provider.research("plates_stocks", day.isoformat(), plate_type=int(kind), code=code)
            if not members or any(r["plate_code"] != code or r["plate_type"] != int(kind)
                                  or r["trade_date"] != day.isoformat() for r in members):
                return {}
            symbols = frozenset(r["symbol"] for r in members if _SYMBOL.fullmatch(r.get("symbol") or ""))
            self._members[key] = (symbols, cn_now().isoformat())
        symbols, member_at = self._members[key]
        received = cn_now()
        if self._stop.is_set() or not _fresh(source, received, received) or _stamp(member_at) > received:
            return {}
        evidence = {"status": "ready", "source": "vendor_plate", "tid": code, "plate_type": int(kind),
                    "name": row["plate_name"], "score": row["source_score"], "rank": 1,
                    "source_at": source, "fetched_at": received.isoformat(), "members_at": member_at}
        return dict.fromkeys(symbols, evidence)

    def _refresh(self, now):
        current = cn_now()
        if self._stop.is_set() or not _fresh(now, current, current):
            return
        try:
            official = _official(self._root, now)
        except Exception as exc:  # noqa: BLE001 - isolated optional archive
            logger.warning("first-board official radar unavailable (%s)", type(exc).__name__)
            official = {}
        with self._lock:
            self._official = official
        try:
            current = cn_now()
            if self._stop.is_set() or not _fresh(now, current, current):
                return
            packet = shared_reader.read("ths_hot_top", now.date())
            received = cn_now()
            history = [] if self._stop.is_set() else _hot_history(self._history_dir, packet, received)
            _rank_rows(history, received)  # reject corrupt archives before publishing to the quote path
        except Exception as exc:  # noqa: BLE001 - source outage or archive integrity failure
            logger.warning("first-board stock popularity unavailable (%s)", type(exc).__name__)
            history = []
        with self._lock:
            self._history = history
        self._members = {k: v for k, v in self._members.items() if k[0] == now.date()}
        plates = {}
        for kind in (PlateType.THEME, PlateType.CONCEPT, PlateType.INDUSTRY):
            current = cn_now()
            if self._stop.is_set() or not _fresh(now, current, current):
                break
            try:
                for symbol, evidence in self._plate(kind, now).items():
                    plates.setdefault(symbol, []).append(evidence)
            except Exception as exc:  # noqa: BLE001 - one category cannot block the rest
                logger.warning("first-board radar category %s unavailable (%s)", kind, type(exc).__name__)
        with self._lock:
            self._plates = plates

    def snapshot(self, symbols: set[str], *, now: datetime, enabled: bool) -> dict[str, dict]:
        now = _stamp(now)
        if not enabled or not symbols or not now or not in_first_board_session(now):
            return {}
        with self._lock:
            if self._stop.is_set():
                return {}
            if ((self._next is None or now >= self._next) and not (self._worker and self._worker.is_alive())
                    and abs(cn_now() - now) < timedelta(seconds=30)):
                self._next = now + _INTERVAL
                self._worker = Thread(target=self._refresh, args=(now,), daemon=True, name="first-board-radar")
                self._worker.start()
            history, official, plates = self._history, self._official, self._plates
        ranks, complete = _rank_rows(history, now)
        missing = {"status": "unavailable"}
        absent = {"status": "unranked", "fetched_at": history[-1]["fetched_at"],
                  "source_at": min(row["source_at"] for row in ranks.values())} if complete else missing
        # Validate each shared plate once per batch, including the official quote clock.
        valid = {}

        def usable(entry):
            if not entry:
                return False
            key = id(entry)
            if key not in valid:
                valid[key] = _fresh(entry.get("market_at") or entry["source_at"], entry["fetched_at"], now)
            return valid[key]

        result = {}
        for symbol in symbols:
            known_plates = [dict(entry) for entry in plates.get(symbol, ()) if usable(entry)]
            vendor = known_plates[0] if known_plates else missing
            core = official.get(symbol)
            theme = core if usable(core) else vendor
            result[symbol] = {"popularity": dict(ranks.get(symbol, absent)), "theme": dict(theme),
                              "plate": dict(vendor), "plates": known_plates}
        return result

    def stop(self):
        self._stop.set()
        with self._lock:
            worker = self._worker
        if worker:
            worker.join(timeout=1)
