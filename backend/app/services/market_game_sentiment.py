"""zzshare post-close short-term sentiment: append-only archive and frozen context.

Counts such as 面/大面/天地板 are vendor-defined statistics; earn/lose effects and
the sentiment index are vendor models whose methods are not published. Rules use
only historical percentiles of these values. Thresholds below come from an
in-sample 2019-2026 study and have not been validated out of sample.
"""
from __future__ import annotations

import json
import math
import threading
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from datetime import date, datetime, time, timedelta
from pathlib import Path

from app.market_time import CN_TZ, cn_now
from app.services.fs_utils import atomic_write_text
from app.services.market_game_psychology import _percentile

VERSION = "sentiment-v1"
BACKFILL_START = date(2019, 1, 1)
OVERLAP_DAYS = 7
SYNC_BUDGET_SECONDS = 30.0
# Vendor statistics for a session are only archived after this Beijing time,
# so an intraday partial row is never frozen as that day's post-close value.
SAME_DAY_READY = time(16, 0)
WINDOW = 252
MIN_SAMPLES = 60
HISTORY_POINTS = 60
# In-sample research thresholds on the composite loss-effect percentile.
LOSS_GATE_CAUTION = 60.0
LOSS_GATE_VETO = 80.0
# Local "verifiable sample" counts versus vendor post-close counts.
LIMIT_UP_TOLERANCE = (8, 0.15)
LIMIT_DOWN_TOLERANCE = (5, 0.15)
MAX_REVISIONS = 1000

SOURCES = (("sentiment", "sentiment_daily"), ("effect", "effect_daily"), ("index", "market_sentiment"))
METRICS = (
    ("earn_effect", "赚钱效应", "score"),
    ("lose_effect", "亏钱效应", "score"),
    ("loss_count", "面+大面+天地板", "count"),
    ("drop5_share", "跌超5%家数占比", "ratio"),
    ("sentiment_return", "情绪指数日涨跌", "return"),
    ("limit_up", "涨停家数", "count"),
    ("limit_down", "跌停家数", "count"),
    ("broken_limit", "炸板家数", "count"),
    ("max_boards", "最高连板", "count"),
)
GATE_FEATURES = ("loss_count", "lose_effect", "drop5_share")
LIMITATIONS = [
    "情绪数据来自 zzshare 盘后统计；赚钱/亏钱效应与情绪指数为供应商模型，计算方法未公开。",
    "面、大面、天地板等为供应商口径的计数，未与本地逐股行情逐一核对。",
    "历史记录为回填或当日采集，供应商可能事后修订；归档只追加，修订仅记录不覆盖。",
    "亏钱效应门阈值（60/80 分位）来自 2019–2026 样本内研究，尚未样本外验证。",
    "这些是市场层面的行为代理，不能识别散户、机构或量化身份。",
]


def _default_fetch(kind: str, start: date, end: date) -> list:
    from app.services.zzshare_research import fetch_range

    return fetch_range(kind, start, end)


class SentimentArchive:
    """Append-only per-source daily rows; first-seen values stay as originally known."""

    def __init__(self, data_dir: Path) -> None:
        self.path = Path(data_dir) / "user_data" / "market_game" / "sentiment" / "zzshare_daily.json"
        self._lock = threading.Lock()

    def load(self) -> dict:
        if not self.path.exists():
            return {"schema_version": 1, "source": "zzshare", "rows": {}, "revisions": []}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            if (value.get("schema_version") != 1 or not isinstance(value.get("rows"), dict)
                    or not isinstance(value.get("revisions"), list)):
                raise ValueError("unsupported archive")
            return value
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValueError("情绪归档损坏或版本不受支持") from exc

    def rows(self) -> dict:
        return self.load()["rows"]

    def merge(self, batches: list[tuple[str, list[dict]]], *, fetched_at: datetime) -> dict:
        stamp = fetched_at.isoformat(timespec="seconds")
        ready_today = fetched_at.astimezone(CN_TZ).time() >= SAME_DAY_READY
        today = fetched_at.astimezone(CN_TZ).date().isoformat()
        added = revised = 0
        with self._lock:
            archive = self.load()
            rows, revisions = archive["rows"], archive["revisions"]
            for part, records in batches:
                for record in records:
                    day = record["trade_date"]
                    if day > today or (day == today and not ready_today):
                        continue
                    values = {key: value for key, value in record.items() if key != "trade_date"}
                    entry = rows.setdefault(day, {})
                    old = entry.get(part)
                    if old is None:
                        entry[part] = {**values, "fetched_at": stamp}
                        added += 1
                        continue
                    changed = sorted(key for key, value in values.items()
                                     if old.get(key) is not None and old.get(key) != value)
                    if changed:
                        revisions.append({"date": day, "part": part, "fields": changed, "detected_at": stamp})
                        revised += 1
            if added or revised:
                archive["revisions"] = revisions[-MAX_REVISIONS:]
                archive["rows"] = dict(sorted(rows.items()))
                self.path.parent.mkdir(parents=True, exist_ok=True)
                atomic_write_text(self.path, json.dumps(archive, ensure_ascii=False, allow_nan=False,
                                                        separators=(",", ":")))
        return {"added": added, "revised": revised}


def sync(archive: SentimentArchive, target: date, *, fetch=None, now: datetime | None = None) -> dict:
    """Fetch missing sessions for each source; failures degrade, never raise."""
    fetch = fetch or _default_fetch
    try:
        rows = archive.rows()
    except ValueError as exc:
        return {"ok": False, "reason": str(exc)}
    batches = []
    try:
        for part, kind in SOURCES:
            days = [day for day, entry in rows.items() if part in entry]
            last = max(days) if days else None
            if last is not None and last >= target.isoformat():
                continue
            start = BACKFILL_START if last is None else date.fromisoformat(last) - timedelta(days=OVERLAP_DAYS)
            records = fetch(kind, start, target)
            if not isinstance(records, list):
                raise ValueError("情绪数据格式异常")
            batches.append((part, records))
    except Exception as exc:  # noqa: BLE001 - optional vendor data must not block the plan
        return {"ok": False, "reason": f"zzshare 情绪数据暂不可用: {_safe_message(exc)}"}
    if not batches:
        return {"ok": True, "reason": "本地情绪归档已覆盖目标日期", "added": 0, "revised": 0}
    stamp = now or cn_now()
    try:
        result = archive.merge(batches, fetched_at=stamp)
    except (OSError, ValueError) as exc:
        return {"ok": False, "reason": f"情绪归档写入失败: {_safe_message(exc)}"}
    return {"ok": True, "reason": "已增量同步 zzshare 盘后情绪", **result}


def sync_with_budget(archive: SentimentArchive, target: date, *, fetch=None,
                     budget: float = SYNC_BUDGET_SECONDS) -> dict:
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="market-game-sentiment")
    try:
        return executor.submit(sync, archive, target, fetch=fetch).result(timeout=budget)
    except FutureTimeout:
        return {"ok": False, "reason": "zzshare 情绪数据请求超时，本次使用已有归档"}
    finally:
        # A slow request may finish later; the append-only merge is safe then.
        executor.shutdown(wait=False)


def _safe_message(exc: Exception) -> str:
    text = str(exc).strip()
    return text[:80] if text and "\\" not in text and "/" not in text else type(exc).__name__


def _number(value) -> float | None:
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _features(entry: dict, previous: dict | None) -> dict:
    sentiment, effect, index = (entry.get(part) or {} for part in ("sentiment", "effect", "index"))
    mian, damian = _number(sentiment.get("mian_count")), _number(sentiment.get("damian_count"))
    tiandi = _number(sentiment.get("tiandi_count"))
    up, down, lt5 = (_number(sentiment.get(key)) for key in ("up_count", "down_count", "lt5_count"))
    close, prior = _number(index.get("close")), _number(index.get("previous_close"))
    if prior is None and previous is not None:
        prior = _number((previous.get("index") or {}).get("close"))
    return {
        "earn_effect": _number(effect.get("earn_effect")),
        "lose_effect": _number(effect.get("lose_effect")),
        "loss_count": mian + damian + (tiandi or 0) if mian is not None and damian is not None else None,
        "drop5_share": lt5 / (up + down) if None not in (up, down, lt5) and up + down > 0 else None,
        "sentiment_return": close / prior - 1 if close and prior and close > 0 and prior > 0 else None,
        "limit_up": _number(sentiment.get("limit_up")),
        "limit_down": _number(sentiment.get("limit_down")),
        "broken_limit": _number(sentiment.get("broken_limit")),
        "max_boards": _number(sentiment.get("max_boards")),
    }


def _gate(percentiles: dict) -> tuple[float | None, str]:
    scores = [percentiles[key] for key in GATE_FEATURES if percentiles.get(key) is not None]
    if len(scores) < 2:
        return None, "unavailable"
    score = round(sum(scores) / len(scores), 2)
    return score, "veto" if score >= LOSS_GATE_VETO else "caution" if score >= LOSS_GATE_CAUTION else "none"


def _within(local: float | None, vendor: float | None, tolerance: tuple[int, float]) -> bool | None:
    if local is None or vendor is None:
        return None
    return abs(local - vendor) <= max(tolerance[0], tolerance[1] * vendor)


def _cross_check(local_metrics: dict, features: dict | None) -> dict:
    local_up, local_down = (_number((local_metrics or {}).get(key)) for key in ("limit_up", "limit_down"))
    vendor_up = features.get("limit_up") if features else None
    vendor_down = features.get("limit_down") if features else None
    up_ok = _within(local_up, vendor_up, LIMIT_UP_TOLERANCE)
    down_ok = _within(local_down, vendor_down, LIMIT_DOWN_TOLERANCE)
    if up_ok is None or down_ok is None:
        status, reason = "unavailable", "本地或 zzshare 涨跌停家数缺失，无法对账"
    elif up_ok and down_ok:
        status, reason = "matched", "本地涨跌停家数与 zzshare 盘后统计在容差内一致"
    else:
        status, reason = "mismatched", "本地涨跌停家数与 zzshare 盘后统计差异超出容差，可能存在未完成的批次或口径差异"
    return {"status": status, "reason": reason, "local_limit_up": local_up, "vendor_limit_up": vendor_up,
            "local_limit_down": local_down, "vendor_limit_down": vendor_down}


def _unavailable(as_of: str | None, reason: str, sync_reason: str | None, local_metrics: dict) -> dict:
    return {
        "version": VERSION, "source": "zzshare", "status": "unavailable", "as_of": as_of,
        "fetched_at": None, "point_in_time": False, "reason": reason, "sync": sync_reason,
        "metrics": [], "loss_gate": {"score": None, "level": "unavailable", "components": [],
                                     "caution": LOSS_GATE_CAUTION, "veto": LOSS_GATE_VETO},
        "cross_check": _cross_check(local_metrics, None), "history": [], "revisions": 0,
        "limitations": list(LIMITATIONS),
    }


def build_sentiment_context(rows: dict, *, as_of: str, cutoff: str, local_metrics: dict | None,
                            latest_mode: bool, sync_reason: str | None = None,
                            revisions: list | None = None) -> dict:
    """Deterministic frozen block; every percentile uses only earlier archive sessions."""
    local_metrics = local_metrics or {}
    days = sorted(day for day in rows if day <= as_of)
    if not days or days[-1] != as_of:
        return _unavailable(as_of, "zzshare 情绪归档未覆盖行情日期", sync_reason, local_metrics)
    entry = rows[as_of]
    if "sentiment" not in entry and "effect" not in entry:
        return _unavailable(as_of, "zzshare 情绪归档缺少行情日期的盘后统计", sync_reason, local_metrics)
    stamps = [part["fetched_at"] for part in entry.values() if isinstance(part, dict) and part.get("fetched_at")]
    fetched_at = max(stamps) if stamps else None
    cutoff_at = datetime.fromisoformat(cutoff)
    known = fetched_at is not None and datetime.fromisoformat(fetched_at) <= cutoff_at
    if latest_mode and not known:
        return _unavailable(as_of, "情绪数据取得时间晚于本次信息截止，不作当时已知证据", sync_reason, local_metrics)
    features = [_features(rows[day], rows[days[index - 1]] if index else None) for index, day in enumerate(days)]

    def percentiles_at(position: int) -> tuple[dict, dict]:
        values, sizes = {}, {}
        prior = features[max(0, position - WINDOW):position]
        for key, _, _ in METRICS:
            rank, size = _percentile(features[position][key], [row[key] for row in prior], 1)
            values[key], sizes[key] = rank, size
        return values, sizes

    current = len(days) - 1
    ranks, sizes = percentiles_at(current)
    score, level = _gate(ranks)
    history = []
    for position in range(max(0, current - HISTORY_POINTS + 1), current + 1):
        point_ranks = ranks if position == current else percentiles_at(position)[0]
        history.append({"date": days[position], "loss_gate": _gate(point_ranks)[0],
                        "earn_effect": features[position]["earn_effect"],
                        "lose_effect": features[position]["lose_effect"]})
    metrics = [{"id": key, "label": label, "unit": unit, "value": features[current][key],
                "percentile": ranks[key], "sample_size": sizes[key]} for key, label, unit in METRICS]
    complete = all(features[current][key] is not None for key in GATE_FEATURES) and level != "unavailable"
    reason = ("盘后情绪与亏钱效应门可用" if complete and known else
              "部分情绪字段或历史样本不足，亏钱效应门按可计算项判断" if known else
              "历史回看使用后来取得的情绪归档，仅供研究，不代表当时已知")
    limitations = list(LIMITATIONS)
    if not known:
        limitations.append("该日情绪数据在信息截止后才取得，不能作为当时可交易依据。")
    return {
        "version": VERSION, "source": "zzshare",
        "status": "ready" if complete and known else "limited",
        "as_of": as_of, "fetched_at": fetched_at, "point_in_time": known, "reason": reason,
        "sync": sync_reason, "metrics": metrics,
        "loss_gate": {"score": score, "level": level,
                      "components": [key for key in GATE_FEATURES if ranks.get(key) is not None],
                      "caution": LOSS_GATE_CAUTION, "veto": LOSS_GATE_VETO},
        "cross_check": _cross_check(local_metrics, features[current]),
        "history": history,
        "revisions": sum(1 for item in revisions or [] if isinstance(item, dict) and item.get("date", "") <= as_of),
        "limitations": limitations,
    }


def gate_series(rows: dict) -> dict[str, tuple[float | None, str]]:
    """Offline per-session gate replay; each day ranks only against earlier sessions."""
    days = sorted(rows)
    features = [_features(rows[day], rows[days[index - 1]] if index else None) for index, day in enumerate(days)]
    result = {}
    for position, day in enumerate(days):
        prior = features[max(0, position - WINDOW):position]
        ranks = {key: _percentile(features[position][key], [row[key] for row in prior], 1)[0]
                 for key in GATE_FEATURES}
        result[day] = _gate(ranks)
    return result


_COMPLETION_PREFIX = "部分日线没有收盘时间证据"


def apply_cross_check(snapshot: dict, sentiment: dict) -> dict:
    """Upgrade only a sole completion-timestamp gap that two sources corroborate."""
    if (snapshot.get("quality") != "limited" or snapshot.get("quality_reasons") != ["completion_unverified"]
            or (sentiment.get("cross_check") or {}).get("status") != "matched"
            or not sentiment.get("point_in_time")):
        return snapshot
    limitations = [text for text in snapshot.get("limitations") or [] if not text.startswith(_COMPLETION_PREFIX)]
    limitations.append("本地涨跌停家数与 zzshare 盘后统计在容差内一致，作为收盘批次完成的独立旁证；"
                       "指数收盘时间按同批次推定，未单独核验。")
    return {**snapshot, "quality": "ready", "quality_reasons": [],
            "quality_corroboration": "completion_corroborated", "limitations": limitations}


def sentiment_facts(sentiment: dict | None) -> list[str]:
    if not sentiment or sentiment.get("status") == "unavailable":
        return []
    facts = []
    for metric in sentiment["metrics"]:
        if metric["id"] not in ("earn_effect", "lose_effect", "loss_count", "drop5_share") or metric["value"] is None:
            continue
        value = f"{metric['value']:.2%}" if metric["unit"] == "ratio" else f"{metric['value']:.0f}"
        rank = "历史分位不足" if metric["percentile"] is None else f"历史分位 {metric['percentile']:.0f}"
        facts.append(f"zzshare {metric['label']} {value}（{rank}）")
    gate = sentiment["loss_gate"]
    if gate["score"] is not None:
        facts.append(f"亏钱效应门综合分位 {gate['score']:.0f}")
    return facts


def sentiment_evidence(sentiment: dict | None) -> list[dict]:
    if not sentiment or sentiment.get("status") == "unavailable":
        return []
    observed = datetime.combine(date.fromisoformat(sentiment["as_of"]), time(15), tzinfo=CN_TZ).isoformat()
    available = sentiment["fetched_at"] if sentiment.get("point_in_time") else None
    return [{"id": f"zzshare_{metric['id']}", "label": f"zzshare {metric['label']}", "value": metric["value"],
             "unit": {"ratio": "ratio", "return": "ratio", "count": "count"}.get(metric["unit"], "number"),
             "source": "zzshare 盘后情绪统计", "observed_at": observed, "available_at": available}
            for metric in sentiment["metrics"]]
