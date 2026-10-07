"""Bounded reconstruction through existing repository and price-basis contracts."""
# ruff: noqa: RUF001
from __future__ import annotations

import hashlib
import json
import math
from datetime import date, datetime, time, timedelta

import numpy as np
import polars as pl

from app.market_time import CN_TZ
from app.price_limits import numpy_limit_price, price_limit_pct
from app.services.market_game_evaluation import _closing_evidence
from app.services.market_game_execution import _iso, _number, minute_slots, timestamp
from app.services.market_game_execution_models import ExecutionPolicy
from app.services.minute_adjust import compute_ratio_pairs, minute_basis_is_raw

MAX_UNIVERSE = 6000
REQUIRED = {"symbol", "datetime", "open", "high", "low", "close", "volume", "amount"}


def _normalize(frame, day, now):
    """Normalize one documented minute convention per symbol, never mix them."""
    if frame.is_empty() or not set(frame.columns) >= REQUIRED:
        return pl.DataFrame()
    if frame.schema["datetime"].time_zone is not None:
        return pl.DataFrame()
    frame = frame.filter(pl.col("datetime").dt.date() == day)
    starts = frame.group_by("symbol").agg(pl.col("datetime").min().alias("first"))
    # 09:31 alone cannot prove end-labelled bars: it may be a start-labelled
    # archive missing 09:30. There is no independent convention field in the
    # repository contract yet, so only an explicit 09:30 origin is accepted.
    starts = starts.with_columns(pl.when(pl.col("first").dt.time() == time(9, 30)).then(0).otherwise(None).alias("offset"))
    frame = frame.join(starts.select("symbol", "offset"), on="symbol", how="left").filter(pl.col("offset").is_not_null())
    frame = frame.with_columns((pl.col("datetime") - pl.duration(minutes=pl.col("offset"))).alias("datetime"))
    slots = [stamp.replace(tzinfo=None) for stamp in minute_slots(day)]
    frame = frame.filter(pl.col("datetime").is_in(slots) & (pl.col("datetime") + pl.duration(minutes=1) <= now.replace(tzinfo=None)))
    keys = ["symbol", "datetime"]
    # Duplicate minutes remain gaps, rather than selecting an arbitrary quote.
    frame = frame.filter(pl.len().over(keys) == 1)
    return frame.sort(keys)


def _valid_rows(frame):
    values = [pl.col(key).is_not_null() & pl.col(key).is_finite() for key in REQUIRED - {"symbol", "datetime"}]
    return frame.filter(
        pl.all_horizontal(values)
        & (pl.min_horizontal("open", "high", "low", "close") > 0)
        & (pl.col("low") <= pl.min_horizontal("open", "close"))
        & (pl.col("high") >= pl.max_horizontal("open", "close"))
        & (pl.col("volume") >= 0) & (pl.col("amount") >= 0)
        & ((pl.col("volume") == 0) == (pl.col("amount") == 0))
        & ((pl.col("volume") == 0)
           | (pl.col("amount") / (pl.col("volume") * 100)).is_between(pl.col("low") * .999, pl.col("high") * 1.001)),
    )


def build_execution_evidence(repo, report: dict, snapshot: dict, *, now: datetime,
                             observations: list[dict] | None = None) -> dict:
    """One full-universe entry-day batch, then <=5 symbols on two later days.

    The policy explicitly models eligibility, cash and a participation-limited
    fill. It does not certify a live account, quote queue or historical identity.
    """
    result = {"sessions": {}, "summary": [], "limitations": [
        "市场复核使用生成时冻结的全部股票及板块成员；要求该冻结全集在对应分钟100%覆盖，不以小样本外推。",
        "模拟假设冻结交易资格及板块归属在3个交易日内保持有效；真实账户资格、临时交易安排与卖盘仍须另行核验。",
        "市场门是冻结的研究规则：上涨覆盖≥40%、涨幅中位数≥-0.5%、存在昨日涨停样本时其均值≥-1%；并非原收盘结论自动延续。",
        "非单位复权投影或除权事件无法证明原始执行标尺时停止评估，不使用当日收盘信息追认盘中交易。",
        "当前仅接受09:30起点的分钟归档；09:31起点缺少独立时标约定，无法区分结束时标与开盘缺档，保守拒绝。",
    ]}
    if observations is not None and len(observations) > 400:
        result["error"] = "追加资金观察超过400条上限，无法证明已完整重放约束"
        return result
    known_observations = []
    for observation in observations or []:
        try:
            created = timestamp(observation["created_at"])
            if observation.get("report_id") != report["id"]:
                raise ValueError("资金观察属于其他报告")
            if created <= now:
                known_observations.append((created, observation))
        except (ValueError, TypeError, KeyError):
            result["error"] = "资金观察的报告或可得时点不完整，无法重放风险约束"
            return result
    known_observations.sort(key=lambda item: item[0])
    if observations is None:
        result["limitations"].append("未传入追加资金观察记录，本次仅模拟冻结基础计划，不评价追加取消或仓位约束后的结果。")
    candidates = report.get("candidates") or []
    days = (report.get("validity") or {}).get("observation_sessions") or []
    if not candidates or not report.get("execution_policy") or len(days) != 3:
        result["error"] = "缺少候选、冻结执行假设或完整观察交易日"
        return result
    policy = ExecutionPolicy.model_validate(report["execution_policy"])
    stocks = snapshot.get("stocks") or []
    universe = {row["symbol"]: row for row in stocks if row.get("symbol")}
    if (not 1 <= len(universe) <= MAX_UNIVERSE or len(universe) != len(stocks)
            or snapshot.get("coverage") != 1 or snapshot.get("metadata_scope") != "current_observation"):
        result["error"] = "冻结市场全集覆盖不完整、成员重复、超过6000只或历史资格未核验"
        return result
    if any((_number(row.get("ref_close")) or 0) <= 0 for row in stocks):
        result["error"] = "冻结市场全集存在缺失原始参考价"
        return result
    try:
        data_dir = repo.store.data_dir
        if not minute_basis_is_raw(data_dir):
            result["error"] = "分钟库尚未具备原始基准标记，不使用混合复权数据模拟成交"
            return result
    except AttributeError:
        result["error"] = "仓库未提供分钟原始基准证明"
        return result
    base = date.fromisoformat(report["as_of"])
    sessions = [date.fromisoformat(day) for day in days]
    symbols = [item["symbol"] for item in candidates]
    if any(symbol not in universe for symbol in symbols):
        result["error"] = "候选不属于冻结市场全集"
        return result
    references = pl.DataFrame({"symbol": list(universe), "reference": [row["ref_close"] for row in universe.values()],
                               "was_winner": [row.get("limit_up") is True for row in universe.values()]})
    fingerprints = []
    for index, day in enumerate(sessions):
        if datetime.combine(day, time(9, 31), CN_TZ) > now:
            break
        wanted = list(universe) if index == 0 else symbols
        try:
            frame = _normalize(repo.get_minute_batch(wanted, day, asset_type="stock"), day, now)
            if frame.is_empty():
                continue
            pairs = pl.DataFrame({"symbol": wanted * 2,
                                  "datetime": [datetime.combine(base, time(9, 30))] * len(wanted)
                                  + [datetime.combine(day, time(9, 30))] * len(wanted)})
            ratios = compute_ratio_pairs(pairs, data_dir, "stock")
            ratio_map = {(row["symbol"], row["_d"]): row["_ratio"] for row in ratios.to_dicts()}
            valid_symbols = [symbol for symbol in wanted if all(
                math.isclose(ratio_map.get((symbol, value), float("nan")), 1, abs_tol=1e-10)
                for value in (base, day))]
            frame = _valid_rows(frame.filter(pl.col("symbol").is_in(valid_symbols)))
        except (AttributeError, ValueError, TypeError, OSError, pl.exceptions.PolarsError):
            continue
        if frame.is_empty():
            continue
        # A rolling valid prefix prevents a missing early member from silently
        # returning later and retroactively certifying the full-market gate.
        frame = frame.with_columns(pl.col("datetime").rank("ordinal").over("symbol").alias("ordinal"))
        expected = {stamp.replace(tzinfo=None): i + 1 for i, stamp in enumerate(minute_slots(day))}
        frame = frame.with_columns(pl.col("datetime").replace_strict(expected, default=None, return_dtype=pl.Int64).alias("expected"))
        frame = frame.filter(pl.col("ordinal") == pl.col("expected"))
        if index == 0:
            market = frame.join(references, on="symbol", how="inner").with_columns((pl.col("close") / pl.col("reference") - 1).alias("ret"))
            market_stats = {row["datetime"]: row for row in market.group_by("datetime").agg(
                pl.len().alias("count"), (pl.col("ret") > 0).mean().alias("breadth"),
                pl.col("ret").median().alias("median"),
                pl.col("ret").filter(pl.col("was_winner")).mean().alias("winner_premium"),
            ).to_dicts()}
        previous = base if index == 0 else sessions[index - 1]
        prior = _closing_evidence(repo, "kline_daily", symbols, previous, previous,
                                  datetime.combine(day, time(9, 30), CN_TZ))
        for candidate in candidates:
            symbol = candidate["symbol"]
            members = [item["symbol"] for item in stocks if candidate["sector"] in (item.get("sectors") or [item.get("sector")])]
            part = frame.filter(pl.col("symbol") == symbol)
            stock = universe[symbol]
            reference = prior.get((symbol, previous))
            if not reference or part.is_empty():
                continue
            if index == 0 and not math.isclose(reference[0], stock["ref_close"], abs_tol=1e-6, rel_tol=1e-8):
                continue
            limits = np.array([price_limit_pct(symbol, day, is_risk_warning=stock.get("is_st") is True)])
            raw_previous = np.array([reference[0]])
            item = {"bars": [{**{key: row[key] for key in REQUIRED - {"datetime", "symbol"}},
                              "time": _iso(row["datetime"].replace(tzinfo=CN_TZ))} for row in part.to_dicts()],
                    "raw_scale_verified": symbol in valid_symbols, "corporate_action": False,
                    "eligible": stock.get("eligible") is True and (stock.get("listing_days") or 0) >= 60,
                    "limit_up": float(numpy_limit_price(raw_previous, limits, up=True)[0]),
                    "limit_down": float(numpy_limit_price(raw_previous, limits, up=False)[0]),
                    "reference_available_at": _iso(reference[1]), "contexts": {}}
            if index == 0 and members:
                sector_stats = {row["datetime"]: row for row in market.filter(pl.col("symbol").is_in(members)).group_by("datetime").agg(
                    pl.len().alias("count"), (pl.col("ret") > 0).mean().alias("breadth"),
                ).to_dicts()}
                for clock, stats in market_stats.items():
                    sector = sector_stats.get(clock, {})
                    stamp = clock.replace(tzinfo=CN_TZ)
                    winner = stats["winner_premium"]
                    allowed = (stats["breadth"] >= policy.minimum_market_breadth
                               and stats["median"] >= policy.minimum_market_median
                               and (winner is None or winner >= policy.minimum_winner_premium))
                    cap, allowed_by_capital, used = candidate["max_position"], True, []
                    for created, observation in known_observations:
                        if created.date() != day or created > stamp + timedelta(minutes=1):
                            continue
                        for link in observation.get("plan_links", []):
                            if link.get("symbol") != symbol:
                                continue
                            retained = _number(link.get("retained_cap"))
                            if retained is not None:
                                cap = min(cap, max(0, retained))
                            elif link.get("status") == "watch" and _number(link.get("observation_cap")) is not None:
                                cap = min(cap, max(0, link["observation_cap"]))
                            if link.get("status") == "blocked":
                                allowed_by_capital, cap = False, 0
                            used.append(observation.get("id", created.isoformat()))
                    item["contexts"][_iso(stamp)] = {
                        "observed_at": _iso(stamp), "available_at": _iso(stamp + timedelta(minutes=1)),
                        "market_coverage": stats["count"] / len(universe),
                        "sector_coverage": sector.get("count", 0) / len(members),
                        "market_allowed": allowed, "sector_breadth": sector.get("breadth"),
                        "market_breadth": stats["breadth"], "market_median_return": stats["median"],
                        "previous_winner_premium": winner,
                        "capital_allowed": allowed_by_capital, "retained_cap": cap,
                        "capital_observation_ids": used,
                    }
            result["sessions"].setdefault(symbol, {})[day.isoformat()] = item
            fingerprints.append(item)
    result["summary"] = [f"冻结市场全集{len(universe)}只；候选{len(candidates)}只；仅入场日扫描全集，后两日只读候选分钟。",
                         f"模拟资金{policy.costs.account_equity:,.0f}元，单分钟参与率上限{policy.costs.max_volume_participation:.0%}，不连接交易账户。"]
    result["fingerprint"] = hashlib.sha256(json.dumps(fingerprints, sort_keys=True, default=str, allow_nan=False).encode()).hexdigest()[:24]
    return result
