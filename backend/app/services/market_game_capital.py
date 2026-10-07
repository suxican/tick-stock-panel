"""Bounded, read-only minute behaviour observations for a frozen game plan.

Scores describe price/volume behaviour, never an investor's identity or net
buying. Minute queries return a forward-adjusted price projection. We therefore
use only within-day ratios, and a close*volume weighted reference on that same
scale; it is not the exchange VWAP and is not compared to the plan's raw prices.
"""
# ruff: noqa: RUF001
from __future__ import annotations

import math
from contextlib import suppress
from datetime import date, datetime, time, timedelta
from statistics import mean

import polars as pl

from app.market_time import CN_TZ
from app.services.market_game_calendar import load_game_calendar

VERSION = "1.0.0"
MAX_SYMBOLS = 30
HISTORY_DAYS = 80
MIN_SAMPLES = 60
METRICS = ("participation", "support", "distribution")
REQUIRED = {"symbol", "datetime", "open", "high", "low", "close", "volume", "amount"}
LIMITATIONS = [
    "仅观察冻结候选及冻结板块内至多30只样本，不代表全市场或整个板块。",
    "参与=30分钟量比；承接=30分钟收盘区间位置；兑现=距30分钟高点回撤。分位只比较此前至多80个本地历史日期的相同时段，至少60个有效样本。",
    "量价行为与情绪代理存在同源性，不能作为独立机构证据；不识别主力、机构或量化身份，也不将量乘涨跌当资金净流入。",
    "参考价为同尺度分钟收盘价按成交量加权，非真实成交均价；不与冻结计划的原始触发价直接比较。",
    "分钟数据仅本地只读，未新建订阅或主动同步；严格早于当前整分的记录才参与观察。",
    "场景是待验证假设，修复与承接不构成入场确认，不提高原计划仓位。",
]


def _number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _date(value):
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _sample(report, snapshot):
    candidates = {row["symbol"]: row for row in report.get("candidates", []) if row.get("symbol")}
    sectors = list(dict.fromkeys(row["name"] for row in report.get("sectors", []) if row.get("name")))
    stocks = {row["symbol"]: row for row in snapshot.get("stocks", []) if row.get("symbol")}
    selected = list(candidates)[:MAX_SYMBOLS]
    # Round-robin frozen sector members so the largest sector cannot consume
    # every supplemental slot. Ordering uses only the report snapshot.
    groups = [[row["symbol"] for row in sorted(stocks.values(), key=lambda row: (-(_number(row.get("amount")) or 0), row["symbol"]))
               if sector in (row.get("sectors") or [row.get("sector")]) and row["symbol"] not in candidates]
              for sector in sectors]
    for index in range(MAX_SYMBOLS):
        for group in groups:
            if index < len(group) and group[index] not in selected and len(selected) < MAX_SYMBOLS:
                selected.append(group[index])
    result = []
    for symbol in selected:
        stock, candidate = stocks.get(symbol, {}), candidates.get(symbol, {})
        memberships = stock.get("sectors") or [stock.get("sector")]
        sector = candidate.get("sector") or next((name for name in sectors if name in memberships), None)
        result.append({"symbol": symbol, "name": candidate.get("name") or stock.get("name") or symbol,
                       "sector": sector, "is_candidate": symbol in candidates})
    return result


def _calendar_for_observation(repo, snapshot, now):
    calendar = snapshot.get("calendar") or {}
    # Frozen session arrays have a bounded horizon even if their underlying
    # source file covers a longer period. Always refresh the local calendar
    # through the existing read-only loader when a repository is available.
    with suppress(AttributeError, OSError, ValueError, TypeError):
        calendar = load_game_calendar(repo.store.data_dir, start=now.date() - timedelta(days=45), cutoff=now, through=now.date())
    covered = (_date(calendar.get("coverage_start")), _date(calendar.get("coverage_end")))
    if not (calendar.get("verified") and all(covered) and covered[0] <= now.date() <= covered[1]):
        return {}
    sessions = sorted({day for value in calendar.get("sessions", []) if (day := _date(value)) is not None})
    snapshot_date = _date(snapshot.get("cutoff"))
    if not sessions or (sessions[-1] < now.date() and snapshot_date and now.date() > snapshot_date + timedelta(days=30)):
        return {}
    return calendar


def _observation_date(repo, snapshot, now):
    calendar = _calendar_for_observation(repo, snapshot, now)
    if not calendar.get("verified"):
        return now.date(), False
    sessions = sorted({day for value in calendar.get("sessions", []) if (day := _date(value)) is not None and day <= now.date()})
    if now.time().replace(tzinfo=None) <= time(9, 31):
        sessions = [day for day in sessions if day < now.date()]
    return (sessions[-1], True) if sessions else (now.date(), False)


def capital_background(repo, report: dict, snapshot: dict, *, data_date: str | None,
                       now: datetime) -> tuple[bool, str]:
    """The frozen previous-session psychology may contextualize this day only."""
    target = _date(data_date)
    calendar = _calendar_for_observation(repo, snapshot, now.astimezone(CN_TZ))
    if target is None or not calendar.get("verified"):
        return False, "观察日期或独立交易日历未核验，心理背景不可联动"
    if snapshot.get("metadata_scope") == "historical_unverified" or report.get("metadata_scope") == "historical_unverified":
        return False, "历史报告元数据未通过当时可得性核验，不参与盘中场景"
    sessions = sorted({day for value in calendar.get("sessions", []) if (day := _date(value)) is not None})
    previous = [day for day in sessions if day < target]
    if target not in sessions or not previous or _date(report.get("as_of")) != previous[-1]:
        return False, "冻结心理不是观察日的上一独立交易日，只保留报告背景展示"
    try:
        cutoff = datetime.fromisoformat(report["cutoff"])
        created = datetime.fromisoformat(report["created_at"])
        if cutoff.tzinfo is None or cutoff >= datetime.combine(target, time(9, 30), CN_TZ) or cutoff >= now:
            return False, "冻结心理在观察日开盘前尚不可用，不参与盘中场景"
        # Archive creation time has second precision; the snapshot cutoff may
        # include microseconds. Respect that storage precision for ordering only.
        if (created.tzinfo is None or created >= datetime.combine(target, time(9, 30), CN_TZ)
                or created > now or created < cutoff.replace(microsecond=0)):
            return False, "报告未在观察日开盘前真实冻结，不将盘中生成的历史报告用于场景"
    except (TypeError, ValueError, KeyError):
        return False, "冻结报告截止或生成时点缺失或无明确时区"
    dimensions = {item.get("id"): _number(item.get("score")) for item in (report.get("psychology") or {}).get("dimensions", [])}
    if any(dimensions.get(key) is None or not 0 <= dimensions[key] <= 100 for key in ("risk_appetite", "panic_pressure")):
        return False, "冻结风险偏好或恐慌维度不可计算，不参与场景"
    return True, "使用观察日上一独立交易日、且开盘前已冻结的心理背景"


def _slots(day, offset):
    return [datetime.combine(day, start) + timedelta(minutes=offset + i)
            for start in (time(9, 30), time(13)) for i in range(120)]


def _features(records, day, cutoff, *, clocks=None, details=True):
    """Validate causally: an afternoon defect cannot erase morning evidence."""
    records = sorted((row for row in records if row["datetime"] < cutoff), key=lambda row: row["datetime"])
    if not records:
        return {}, None, None, "缺少目标日期已完成分钟"
    first = records[0]["datetime"]
    if first.time() not in (time(9, 30), time(9, 31)):
        return {}, None, first, "分钟缺少开盘起点，无法确认累计基准"
    offset = int(first.time() == time(9, 31))
    expected = [stamp for stamp in _slots(day, offset) if stamp < cutoff]
    by_time = {}
    duplicates = set()
    for row in records:
        stamp = row["datetime"]
        if stamp in by_time:
            duplicates.add(stamp)
        by_time[stamp] = row
    # Non-minute stamps and mixed start/end conventions are not silently dropped.
    unexpected = sorted(set(by_time) - set(expected))
    bad_at = unexpected[0] if unexpected else None
    features, previous, cum_volume, cum_price_volume = {}, [], 0.0, 0.0
    reason = ""
    last_time = records[-1]["datetime"]
    for index, stamp in enumerate(expected):
        if bad_at is not None and stamp >= bad_at:
            reason = "分钟时标不符合完整会话，可能混用开始与结束时标"
            break
        if stamp in duplicates:
            reason = "分钟存在重复记录"
            break
        if stamp not in by_time:
            reason = "分钟尾部未更新" if stamp > last_time else "分钟会话存在缺口"
            break
        source = by_time[stamp]
        values = {key: _number(source.get(key)) for key in ("open", "high", "low", "close", "volume", "amount")}
        if any(value is None for value in values.values()):
            reason = "分钟价格或量额缺失"
            break
        op, high, low, close, volume, amount = (values[key] for key in ("open", "high", "low", "close", "volume", "amount"))
        if min(op, high, low, close) <= 0 or low > min(op, close) or high < max(op, close) or volume < 0 or amount < 0:
            reason = "分钟OHLC或量额无效"
            break
        # Canonical volume is lots and amount is yuan. This is a scale check,
        # not a reconstruction of a tick VWAP. Legacy mixed-adjustment data
        # fail closed. Small currency rounding is allowed, no unit guessing.
        if ((volume == 0) != (amount == 0)
                or (volume > 0 and not low * .99 <= amount / (volume * 100) <= high * 1.01)):
            reason = "分钟量额与价格标尺不一致，不能验证复权口径"
            break
        if index == 120:
            previous = []
        previous.append(values)
        cum_volume += volume
        cum_price_volume += close * volume
        if clocks is not None and stamp.time() not in clocks:
            continue
        reference = cum_price_volume / cum_volume if cum_volume > 0 else None
        windows = []
        for n in (5, 15, 30):
            latest = previous[-n:]
            prior = previous[-2 * n:-n]
            windows.append({
                "minutes": n,
                "return": close / latest[0]["open"] - 1 if len(latest) == n and sum(item["volume"] for item in latest) > 0 else None,
                "volume_ratio": (sum(item["volume"] for item in latest) / sum(item["volume"] for item in prior)
                                 if len(prior) == n and sum(item["volume"] for item in prior) > 0 else None),
            })
        recent = previous[-30:]
        peak, trough = max(item["high"] for item in recent), min(item["low"] for item in recent)
        valid_window = len(recent) == 30 and sum(item["volume"] for item in recent) > 0
        features[stamp.time()] = {
            "participation": windows[-1]["volume_ratio"],
            "support": (close - trough) / (peak - trough) if valid_window and peak > trough else None,
            "distribution": (peak - close) / peak if valid_window and peak > trough else None,
            "offset": offset,
        }
        if details:
            features[stamp.time()].update(windows=windows, reference_price=reference,
                                          price_bias=close / reference - 1 if reference else None)
    if bad_at and not reason:
        reason = "分钟存在会话之外或非整分时标"
    return features, offset, last_time, reason


def _metric(value, history):
    samples = [item for item in history if item is not None]
    score = None
    if value is not None and len(samples) >= MIN_SAMPLES:
        equal = sum(math.isclose(item, value, rel_tol=0, abs_tol=1e-10) for item in samples)
        below = sum(item < value and not math.isclose(item, value, rel_tol=0, abs_tol=1e-10) for item in samples)
        score = round(100 * (below + equal / 2) / len(samples), 2)
    return {"score": score, "raw_value": round(value, 10) if value is not None else None, "sample_size": len(samples)}


def _scores(current, prior, clock):
    return {key: _metric(current.get(key), [item.get(clock, {}).get(key) for item in prior
                                          if item.get(clock, {}).get("offset") == current.get("offset")])
            for key in METRICS}


def _scenario(row, report, now):
    result = {"id": "unconfirmed", "label": "资金行为待确认", "evidence": [],
              "alternative": "消息重估、被动调仓或流动性变化也可能产生同样量价行为，不能确认交易者身份与心理因果。",
              "confirmation": "需后续完整分钟与板块样本共同验证，不能代替计划入场条件。",
              "invalidation": "量价数据缺失、承接转弱或场景证据反转时撤销该假设。"}
    if row["status"] not in {"ready", "historical"}:
        return result
    try:
        cutoff = datetime.fromisoformat(report["cutoff"])
        if cutoff.tzinfo is None or cutoff >= now or cutoff.astimezone(CN_TZ).date() >= _date(row["as_of"]):
            return result
    except (KeyError, ValueError, TypeError):
        return result
    dimensions = {item.get("id"): _number(item.get("score")) for item in (report.get("psychology") or {}).get("dimensions", [])}
    panic, appetite = dimensions.get("panic_pressure"), dimensions.get("risk_appetite")
    participation, support, distribution = (row[key]["score"] for key in METRICS)
    returns = row["windows"][-1]["return"]
    bias = row["price_bias"]
    if None in (participation, support, distribution, returns, bias):
        return result
    selected = None
    if panic is not None and panic >= 75:
        if support >= 75 and distribution <= 25 and returns > 0 and bias >= 0:
            selected = ("panic_absorption", "恐慌背景下承接增强", "冻结恐慌代理偏高；30分钟收盘位置偏强、回撤分位偏低且站上分钟加权参考价。")
        elif support <= 25 and distribution >= 75 and returns < 0 and bias < 0:
            selected = ("panic_withdrawal", "恐慌与撤退共振", "冻结恐慌代理偏高；30分钟收盘位置偏弱、回撤分位偏高且低于分钟加权参考价。")
    elif appetite is not None and appetite >= 75:
        if participation >= 75 and distribution >= 75 and returns <= 0:
            selected = ("hot_distribution", "追涨背景下兑现压力上升", "冻结风险偏好代理偏高；量比放大但30分钟价格未推进、距高点回撤分位偏高。")
        elif participation >= 50 and support >= 75 and distribution <= 25 and returns > 0 and bias >= 0:
            selected = ("trend_feedback", "上涨正反馈观察", "冻结风险偏好代理偏高；放量、区间强收盘与参考价上方推进共同出现。")
    elif appetite is not None and panic is not None and 25 <= appetite < 75 and panic < 75 and support >= 75 and distribution <= 25 and returns > 0 and bias >= 0:
        selected = ("divergence_repair", "分歧后的修复候选", "冻结情绪背景未极端；30分钟收盘位置与参考价上方推进改善。")
    if selected:
        result.update(id=selected[0], label=selected[1], evidence=[selected[2]])
    return result


def _empty_row(stock, reason):
    return {**stock, "status": "unavailable", "reason": reason, "as_of": None,
            "windows": [{"minutes": n, "return": None, "volume_ratio": None} for n in (5, 15, 30)],
            **{key: {"score": None, "raw_value": None, "sample_size": 0} for key in METRICS},
            "reference_price": None, "price_bias": None,
            "scenario": {"id": "unconfirmed", "label": "资金行为待确认", "evidence": [],
                         "alternative": "证据不足，无法区分不同交易动机。", "confirmation": "等待完整分钟与历史同时间样本。", "invalidation": "数据缺失时不维持原判断。"}}


def _sectors(rows):
    result = []
    for name in dict.fromkeys(row["sector"] for row in rows if row["sector"]):
        members = [row for row in rows if row["sector"] == name]
        valid = [row for row in members if row["status"] in {"ready", "limited", "historical"} and row["reference_price"] is not None]
        covered = len(valid) >= 3 and len(valid) / len(members) >= .6
        result.append({
            "name": name, "sample_size": len(members), "valid_count": len(valid), "coverage": len(valid) / len(members),
            "breadth": mean(row["price_bias"] >= 0 for row in valid) if covered else None,
            **{key: (round(mean(scores), 2) if covered and len(scores := [row[key]["score"] for row in valid if row[key]["score"] is not None]) >= 3 and len(scores) / len(members) >= .6 else None) for key in METRICS},
            "scenario": "仅冻结样本的分钟行为汇总" if covered else "有效样本不足，板块判断待确认",
        })
    return result


def build_capital_behavior(repo, report: dict, snapshot: dict, *, now: datetime) -> dict:
    """Observe now using existing local repository APIs; no sync or subscription."""
    if now.tzinfo is None:
        raise ValueError("observation time requires an explicit timezone")
    now = now.astimezone(CN_TZ)
    target, calendar_verified = _observation_date(repo, snapshot, now)
    background_usable, _ = capital_background(repo, report, snapshot, data_date=target.isoformat(), now=now)
    if not background_usable:
        report = {**report, "psychology": None}
    cutoff = now.replace(tzinfo=None, second=0, microsecond=0) if target == now.date() else datetime.combine(target + timedelta(days=1), time())
    sample = _sample(report, snapshot)
    result = {"input_version": VERSION, "status": "unavailable", "reason": "本地分钟数据不可用",
              "data_date": target.isoformat(), "observed_at": None,
              "rows": [_empty_row(stock, "本地分钟数据不可用") for stock in sample],
              "sectors": [], "series": [], "limitations": list(LIMITATIONS)}
    if not sample:
        result["reason"] = "冻结报告没有候选或可观察板块样本"
        return result
    try:
        dates = sorted({day for value in repo.list_minute_dates(target - timedelta(days=540), target, asset_type="stock")
                        if (day := _date(value)) is not None and target - timedelta(days=540) <= day < target})[-HISTORY_DAYS:]
        frame = repo.get_minute_by_dates([row["symbol"] for row in sample], [*dates, target], asset_type="stock")
    except Exception:  # optional local data failures cannot break the frozen report
        return result
    if not isinstance(frame, pl.DataFrame) or frame.is_empty():
        result["status"], result["reason"] = "stale", "目标日期没有本地分钟，不回退到其他日期冒充当前行情"
        for row in result["rows"]:
            row.update(status="stale", reason=result["reason"])
        result["sectors"] = _sectors(result["rows"])
        return result
    if not REQUIRED.issubset(frame.columns) or not isinstance(frame.schema["datetime"], pl.Datetime):
        result["reason"] = "分钟字段或北京墙钟时间类型不完整"
        return result
    if frame.schema["datetime"].time_zone is not None:
        # Canonical repository timestamps must be Beijing wall-clock naive.
        result["reason"] = "分钟时间不是标准北京墙钟，停止推断时区"
        return result
    frame = frame.filter(pl.col("symbol").is_in([row["symbol"] for row in sample])
                         & pl.col("datetime").dt.date().is_in([*dates, target])
                         & (pl.col("datetime") < cutoff)).with_columns(pl.col("datetime").dt.date().alias("_day")).sort(["symbol", "datetime"])
    grouped = frame.partition_by("symbol", as_dict=True)
    paths = {}
    rows = []
    for stock in sample:
        part = grouped.get((stock["symbol"],), pl.DataFrame())
        daily = part.partition_by("_day", as_dict=True) if not part.is_empty() else {}
        current_part = daily.get((target,))
        features, offset, last_time, reason = _features(current_part.to_dicts() if current_part is not None else [], target, cutoff)
        # Retain only the displayed 5-minute points and the latest observation.
        # History carries three scalars, not duplicated per-bar window arrays.
        clocks = {clock for clock in features if clock.minute % 5 == 0}
        if features:
            clocks.add(max(features))
        history = []
        for day in dates:
            historical = daily.get((day,))
            if historical is not None and clocks:
                history_cutoff = datetime.combine(day, max(clocks)) + timedelta(minutes=1)
                history_features, _, _, _ = _features(historical.to_dicts(), day, history_cutoff, clocks=clocks, details=False)
                history.append(history_features)
        row = _empty_row(stock, reason)
        if last_time:
            row["as_of"] = last_time.replace(tzinfo=CN_TZ).isoformat()
        expected = [stamp for stamp in _slots(target, offset or 0) if stamp < cutoff]
        current = features.get(expected[-1].time()) if expected else None
        if current is None or reason:
            row["status"] = "stale" if reason in {"缺少目标日期已完成分钟", "分钟尾部未更新"} else "unavailable"
        else:
            scores = _scores(current, history, expected[-1].time())
            row.update(**scores, windows=current["windows"], reference_price=current["reference_price"], price_bias=current["price_bias"])
            ready = all(score["score"] is not None for score in scores.values())
            row["status"] = "ready" if ready and calendar_verified else "limited"
            row["reason"] = "完整分钟与历史同时间分位可用" if row["status"] == "ready" else "仅展示已知量价；分钟窗口、60日同时间样本或交易日历尚不充分"
            if target < now.date() and calendar_verified:
                row["status"] = "historical"
                row["reason"] = "休市或开盘前，展示最近已知交易日，不代表当前盘中"
            row["scenario"] = _scenario(row, report, now)
        paths[stock["symbol"]] = (features, history)
        rows.append(row)
    clocks = sorted({clock for features, _ in paths.values() for clock in features if clock.minute % 5 == 0})
    series = []
    for clock in clocks:
        points = [_scores(features[clock], history, clock) for features, history in paths.values() if clock in features]
        series.append({"time": clock.strftime("%H:%M"), "sample_size": len(points),
                       **{key: round(mean(values), 2) if len(values := [point[key]["score"] for point in points if point[key]["score"] is not None]) >= max(1, math.ceil(len(sample) * .6)) else None for key in METRICS}})
    # The envelope's created_at records request time. This field describes the
    # actual latest completed source bar, so refreshing empty data is not fresh
    # market evidence. Each row retains its own date/quality independently.
    observed_at = max((row["as_of"] for row in rows if row["as_of"] is not None), default=None)
    result.update(rows=rows, sectors=_sectors(rows), series=series, observed_at=observed_at)
    states = {row["status"] for row in rows}
    if states == {"ready"}:
        result.update(status="ready", reason="冻结样本的当日分钟与同时间历史分位可用")
    elif states == {"historical"}:
        result.update(status="historical", reason="休市或开盘前的最近交易日观察，未冒充当前盘中")
    elif states <= {"stale", "unavailable"}:
        result.update(status="stale" if "stale" in states else "unavailable", reason="目标日期分钟缺失、未更新或校验失败，不能维持资金方向判断")
    else:
        result.update(status="limited", reason="部分样本或历史同时间证据不足，空值不代表中性")
    if not calendar_verified:
        result["limitations"].append("交易日历未覆盖观察时点，日期状态未核验，不回退其他日期或形成可执行判断。")
    return result
