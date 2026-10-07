"""Pure, point-in-time rules for conditional 1-3-session research plans.

All ratios are decimals and all prices use the snapshot's original-price scale.
Thresholds below are versioned research assumptions, not calibrated probabilities.
The snapshot builder owns time filtering, provenance and trading eligibility;
this module never fetches data or consumes retrospective phase labels.
"""
# Chinese punctuation below belongs to user-facing prose, not identifiers.
# ruff: noqa: RUF001
from __future__ import annotations

import copy
import math
from bisect import bisect_left, bisect_right
from collections import defaultdict

from app.services.market_game_models import RiskConfig
from app.services.market_game_psychology import analyze_psychology

RULE_VERSION = "1.1.0"
DEFAULT_RISK = RiskConfig().model_dump()
MIN_REWARD_RISK = 1.0
RESEARCH_COST_BUFFER = 0.003


def _number(value, low=None, high=None) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if (not math.isfinite(result) or (low is not None and result < low)
            or (high is not None and result > high)):
        return None
    return result


def _risk_config(risk: dict | None) -> dict:
    return RiskConfig.model_validate(risk or {}).model_dump()


def _metrics(raw: dict) -> dict:
    result = {}
    for key in ("breadth_up", "above_ma20", "seal_rate", "index_above_ma20"):
        result[key] = _number(raw.get(key), 0, 1)
    for key in ("median_return", "index_ret20", "index_ret5", "index_return", "previous_limit_premium"):
        result[key] = _number(raw.get(key))
    for key in ("limit_up", "limit_down", "amount_ratio"):
        result[key] = _number(raw.get(key), 0)
    return result


def _market_state(current: dict, previous: dict) -> tuple[dict, dict]:
    breadth, prior_breadth = current["breadth_up"], previous["breadth_up"]
    median, prior_median = current["median_return"], previous["median_return"]
    index_return, above = current["index_ret20"], current["above_ma20"]
    if index_return is None or above is None:
        trend = "未知"
    elif index_return > 0 and above >= 0.55:
        trend = "上行"
    elif index_return < -0.01 and above <= 0.4:
        trend = "下行"
    else:
        trend = "震荡"

    comparable = all(value is not None for value in (breadth, prior_breadth, median, prior_median))
    panic = comparable and prior_breadth <= 0.3 and prior_median <= -0.01
    repairing = comparable and breadth >= 0.4 and breadth - prior_breadth >= 0.15 and median - prior_median >= 0.01 and median >= -0.005
    down, prior_down = current["limit_down"], previous["limit_down"]
    premium, prior_premium = current["previous_limit_premium"], previous["previous_limit_premium"]
    short_index_risk = (
        (current["index_return"] is not None and current["index_return"] <= -0.02)
        or (current["index_ret5"] is not None and current["index_ret5"] <= -0.05)
        or (current["index_ret5"] is not None and current["index_ret5"] <= -0.03
            and current["index_above_ma20"] is not None and current["index_above_ma20"] <= 0.25)
    )
    loss_veto = premium is not None and premium < -0.01
    # Independent adverse evidence cannot be cancelled by a favourable seal
    # rate or winner premium. Missing loss counts do not certify a repair.
    down_not_worsening = down is not None and prior_down is not None and down <= prior_down
    loss_easing = (
        down is not None and prior_down is not None and prior_down >= 5 and down <= prior_down * 0.8
    ) or (
        premium is not None and prior_premium is not None
        and prior_premium < 0 and premium >= 0 and premium - prior_premium >= 0.01
    )
    panic_repair = bool(panic and repairing and loss_easing and down_not_worsening
                        and premium is not None and premium >= 0 and not short_index_risk)

    def hot(metrics):
        return (metrics["breadth_up"] is not None and metrics["amount_ratio"] is not None
                and metrics["breadth_up"] >= 0.75 and metrics["amount_ratio"] >= 1.2)

    heat = hot(current) or hot(previous)
    seal, prior_seal = current["seal_rate"], previous["seal_rate"]
    deteriorating = (
        seal is not None and prior_seal is not None and seal <= 0.6 and prior_seal - seal >= 0.1
    ) or (premium is not None and premium < 0) or (
        comparable and median < 0 and prior_median > 0 and breadth < 0.5
    )
    crowded_exit = bool(heat and deteriorating)
    loss_controlled = (down is not None and down <= 10 and premium is not None and premium >= 0
                       and seal is not None and seal >= 0.65 and not short_index_risk)
    trend_pullback = bool(
        trend == "上行" and breadth is not None and 0.35 <= breadth <= 0.65
        and median is not None and -0.02 <= median <= 0.01 and loss_controlled
        and not crowded_exit
    )
    if breadth is None or median is None:
        phase, emotion = "待确认", "未知"
    elif crowded_exit or loss_veto or short_index_risk:
        phase, emotion = "退潮", "转弱"
    elif panic_repair:
        phase, emotion = "修复", "修复"
    elif breadth <= 0.3 and median < -0.01:
        phase, emotion = "冰点", "低迷"
    elif breadth < 0.4 and median < 0 and (
        (down is not None and down >= 10) or (premium is not None and premium < 0)
    ):
        phase, emotion = "退潮", "偏弱"
    elif hot(current) and median >= 0.01:
        phase, emotion = "高潮", "偏热"
    elif (comparable and prior_breadth <= 0.45 and prior_median <= 0
          and breadth >= 0.6 and median > 0 and loss_controlled and trend != "下行"):
        phase, emotion = "启动", "改善"
    elif trend == "上行" and breadth >= 0.5 and median >= -0.005 and loss_controlled:
        phase, emotion = "主升", "分歧" if trend_pullback else "偏强"
    else:
        phase, emotion = "待确认", "中性"
    if phase in {"退潮", "冰点"}:
        panic_repair = trend_pullback = False
    if not comparable:
        change = "缺少同口径前日数据，暂不判断边际变化"
    else:
        change = f"上涨占比较前日变化 {(breadth - prior_breadth) * 100:+.1f} 个百分点；中位涨跌幅变化 {(median - prior_median) * 100:+.2f} 个百分点"
    state = {
        "trend": trend, "phase": phase, "emotion": emotion,
        "crowding": "偏高" if heat else ("未见高热证据" if current["amount_ratio"] is not None else "未知"),
        "change": change,
    }
    matches = {
        "panic_repair": panic_repair, "trend_pullback": trend_pullback,
        "crowded_exit": crowded_exit, "panic": bool(panic), "heat": bool(heat),
        "short_index_risk": bool(short_index_risk), "loss_veto": bool(loss_veto),
    }
    return state, matches


def _facts(current: dict, previous: dict) -> list[str]:
    facts = []
    for key, label, percentage in (
        ("breadth_up", "上涨占比", True), ("median_return", "市场中位涨跌幅", True),
        ("above_ma20", "站上20日均线占比", True), ("index_ret20", "指数20日收益", True),
        ("index_return", "指数当日收益", True), ("index_ret5", "指数5日收益", True),
        ("limit_down", "跌停家数", False), ("seal_rate", "封板率", True),
        ("previous_limit_premium", "昨日涨停股当日溢价", True),
        ("amount_ratio", "成交额相对前5日均额倍数", False),
    ):
        value, prior = current[key], previous.get(key)
        if value is None:
            facts.append(f"{label}缺失，未按零值处理")
            continue
        text = f"{label} {value:.2%}" if percentage else f"{label} {value:.2f}"
        if prior is not None:
            text += f"（前日 {prior:.2%}）" if percentage else f"（前日 {prior:.2f}）"
        facts.append(text)
    return facts


def _hypotheses(matches: dict, state: dict, facts: list[str]) -> list[dict]:
    specs = (
        ("panic_repair", "恐慌后的首次修复", matches["panic"],
         "前期恐慌可能带来滞后卖出；广度修复与亏钱效应收敛共同出现后，才观察承接。",
         "反弹也可能只是短暂回补，尚不能据此判断趋势反转或主力吸筹。",
         ["下一交易日上涨占比与亏钱效应继续改善", "候选与所属板块同步走强，价格进入触发区且可以买入"],
         ["跌停重新扩散或昨日强势股溢价再次转负", "候选跌破失效参考价或板块修复失去跟随"]),
        ("trend_pullback", "主线趋势中的分歧修复", state["trend"] == "上行",
         "趋势与板块相对强势仍在时，分歧可能让短线参与者提前退出；只关注有承接的候选。",
         "分歧也可能是退潮的开始；现有量价数据不能确认卖方身份或真实筹码成本。",
         ["板块强度及上涨广度保持，候选收复并守住触发参考区", "市场亏钱效应未扩散，开盘价格未超过触发上限"],
         ["板块由相对强势转为整体走弱", "个股破坏失效参考价，或市场跌停扩散"]),
        ("crowded_exit", "一致追涨后的兑现风险", matches["heat"],
         "高热度叠加封板或溢价转弱，可能意味着追涨需求与退出供给开始失衡；暂停新增风险。",
         "热度也可能得到持续增量承接；仅凭高涨幅或放量，不能确认见顶或主力出货。",
         ["高热之后的负溢价或封板弱化持续", "复核已有仓位的原退出条件，不追入高开加速股票"],
         ["板块扩散、封板和次日溢价恢复，撤销兑现风险假设", "新的有效快照重新满足其他模式，才重新分配风险"]),
    )
    return [{
        "id": key, "title": title,
        "status": "matched" if matches[key] else ("watch" if watch else "inactive"),
        "facts": list(facts), "interpretation": interpretation, "alternative": alternative,
        "confirm": confirm, "invalidation": invalidation,
    } for key, title, watch, interpretation, alternative, confirm, invalidation in specs]


def _stocks(snapshot: dict) -> list[dict]:
    # Snapshot producers guarantee unique symbols. Duplicates fail closed if they
    # disagree, so repeated entries cannot inflate breadth or receive two weights.
    unique, conflicted = {}, set()
    for row in snapshot.get("stocks", []):
        if not isinstance(row, dict) or not isinstance(row.get("symbol"), str):
            continue
        symbol = row["symbol"]
        if not symbol:
            continue
        if symbol in unique and unique[symbol] != row:
            conflicted.add(symbol)
        unique[symbol] = row
    return [unique[symbol] for symbol in sorted(unique) if symbol not in conflicted]


def _memberships(row: dict) -> set[str]:
    values = row.get("sectors")
    values = list(values) if isinstance(values, list) else []
    values.append(row.get("sector"))
    return {value.strip() for value in values if isinstance(value, str) and value.strip()}


def _sectors(stocks: list[dict], median: float | None) -> tuple[list[dict], dict[str, set[str]]]:
    groups = defaultdict(list)
    members = defaultdict(set)
    for row in stocks:
        change = _number(row.get("change_pct"), -1, 1)
        if change is None:
            continue
        for sector in _memberships(row):
            groups[sector].append((change, _number(row.get("ret5"))))
            members[sector].add(row["symbol"])
    result = []
    for name, rows in groups.items():
        # Very broad stock-connect/style universes do not identify a tradable
        # short-term theme. This versioned cutoff also bounds overlap work.
        if not 3 <= len(rows) <= 600:
            continue
        mean = sum(row[0] for row in rows) / len(rows)
        breadth = sum(row[0] > 0 for row in rows) / len(rows)
        history = [row[1] for row in rows if row[1] is not None]
        coverage = len(history) / len(rows)
        persistent = len(history) >= 3 and coverage >= 0.8 and sum(history) / len(history) > 0
        strong = median is not None and mean >= max(0, median + 0.005) and breadth >= 0.6 and persistent
        result.append({
            "name": name, "avg_return": round(mean, 6), "breadth": round(breadth, 6),
            "status": "关注" if strong else "观察",
            "reason": f"本地可用样本 {len(rows)} 只，上涨占比 {breadth:.0%}，5日收益覆盖 {coverage:.0%}；"
                      + ("当日相对强势且5日平均收益为正" if strong else "尚未同时满足相对强势、广度与持续性条件"),
        })
    result.sort(key=lambda row: (row["status"] != "关注", -row["avg_return"], row["name"]))
    return result, dict(members)


def _risk_groups(sectors: list[dict], members: dict[str, set[str]]) -> dict[str, str]:
    groups = {row["name"]: row["name"] for row in sectors}
    names = list(groups)
    for index, name in enumerate(names):
        for other in names[index + 1:]:
            left, right = members[name], members[other]
            shared = len(left & right)
            if shared / len(left | right) >= 0.5 or shared / min(len(left), len(right)) >= 0.7:
                old, new = groups[other], groups[name]
                groups = {key: new if value == old else value for key, value in groups.items()}
    for sector in sectors:
        related = [name for name in names if name != sector["name"] and groups[name] == groups[sector["name"]]]
        if related:
            sector["reason"] += f"；与{'、'.join(related)}成员重叠，仓位共用一个板块风险上限"
    return groups


def _candidate(row: dict, mode: str, sector: dict, risk: dict) -> dict | None:
    if (row.get("eligible") is not True or row.get("is_st") is not False
            or any(row.get(flag) is not False for flag in ("limit_up", "limit_down", "one_price"))):
        return None
    values = {key: _number(row.get(key)) for key in (
        "ref_close", "raw_low", "ma5", "ma20", "ret5", "ret20", "change_pct",
        "amount", "atr_pct", "close_location", "drawdown5", "listing_days", "history_days",
    )}
    if any(value is None for value in values.values()):
        return None
    price, low, ma5, ma20 = (values[key] for key in ("ref_close", "raw_low", "ma5", "ma20"))
    if (not 0 < low <= price or ma5 <= 0 or ma20 <= 0
            or values["listing_days"] < 60 or values["history_days"] < 21
            or values["amount"] < risk["min_amount"] or not 0 < values["atr_pct"] <= 0.1
            or not 0.6 <= values["close_location"] <= 1 or not 0 <= values["drawdown5"] <= 0.12
            or not -0.02 <= values["change_pct"] <= 0.07):
        return None
    if mode == "panic_repair":
        if (values["change_pct"] <= 0 or values["change_pct"] < sector["avg_return"]
                or values["ret5"] < -0.08 or price < ma5 * 0.985):
            return None
    elif (price < ma20 or price < ma5 * 0.99 or values["ret20"] <= 0
          or not -0.03 <= values["ret5"] <= 0.18 or values["drawdown5"] < 0.02
          or values["change_pct"] < max(-0.01, sector["avg_return"] - 0.015)):
        return None

    trigger_low = round(max(price, ma5) * 1.001, 2)
    trigger_high = round(trigger_low * 1.015, 2)
    invalidation = round(low * 0.997, 2)
    if invalidation <= 0 or trigger_low <= invalidation or trigger_low > price * 1.04:
        return None
    # This is an observed five-session high, not a forecast or fair value.
    # Evaluate from the least favourable allowed entry, after a research cost
    # buffer. Position sizing separately stresses gaps and delayed exits.
    pressure = math.floor(price / (1 - values["drawdown5"]) * 100) / 100
    reward = pressure / trigger_high - 1 - RESEARCH_COST_BUFFER
    loss = (trigger_high - invalidation) / trigger_high + RESEARCH_COST_BUFFER
    reward_risk = reward / loss
    if reward_risk < MIN_REWARD_RISK:
        return None
    # Stress exceeds the visible stop distance: overnight gaps, T+1 and costs
    # make a reference stop price an unreliable bound on realised loss.
    stress = math.ceil((max(0.06, 2 * values["atr_pct"],
                             (trigger_high - invalidation) / trigger_high + 0.03) + 0.003) * 1e6) / 1e6
    return {
        "symbol": row["symbol"], "name": row.get("name") or row["symbol"],
        "sector": sector["name"], "mode": mode, "role": "相对强势观察",
        "reference_price": price, "trigger_low": trigger_low, "trigger_high": trigger_high,
        "invalidation_price": invalidation, "max_position": 0.0,
        "stress_loss_pct": stress, "holding_days": 3,
        "score": None, "pressure_price": pressure, "reward_risk_ratio": round(reward_risk, 4),
        "evidence": [
            f"当日涨跌幅 {values['change_pct']:.2%}，5日收益 {values['ret5']:.2%}",
            f"当日较所属板块均值 {values['change_pct'] - sector['avg_return']:+.2%}",
            f"日内收盘位置 {values['close_location']:.0%}，5日高点回撤 {values['drawdown5']:.2%}",
            f"成交额 {values['amount'] / 1e8:.2f} 亿元；上市 {values['listing_days']:.0f} 个自然日",
            "参考价格均为目标日原始价格标尺；失效参考取当日低点下方0.3%缓冲",
            f"5日高点压力参考 {pressure:.2f}，扣除研究成本缓冲后空间/结构损失比 {reward_risk:.2f}；不是预期收益",
        ],
        "trigger": [
            f"仅下一交易日观察，价格进入 {trigger_low:.2f}–{trigger_high:.2f} 并获得承接后才考虑",
            "同时满足市场模式与板块确认；开盘超过上限、无可成交卖盘或状态变化则取消",
            "这是待确认计划，日线数据不能证明盘中触发或成交",
        ],
        "invalidation": [f"价格跌破 {invalidation:.2f} 的结构参考位", "所属板块整体转弱或市场亏钱效应扩散"],
        "exit_rules": [
            f"接近 {pressure:.2f} 的近期高点压力区时复核承接与获利退出；不把突破作为必然结果",
            "结构失效或模式失效时，按可执行的最早时点退出；不通过补仓改变原交易理由",
            "T+1 约束：当日新买仓位不能当日卖出；停牌、跌停或缺少买盘会延迟退出",
            "触发日计为持有第1个交易日，最迟第3个交易日计划退出；未触发则不建仓",
            "止损参考不是保证成交价；隔夜跳空、费用和退出延迟可能使实际损失超过压力预算",
        ],
    }


def _candidate_pool(stocks: list[dict], sectors: list[dict], mode: str | None, risk: dict) -> list[dict]:
    if not mode:
        return []
    sector_map = {row["name"]: row for row in sectors if row["status"] == "关注"}
    order = {name: index for index, name in enumerate(sector_map)}
    pool = []
    strengths = {}
    for row in stocks:
        for name in sorted(_memberships(row) & sector_map.keys(), key=order.__getitem__):
            sector = sector_map[name]
            candidate = _candidate(row, mode, sector, risk)
            if candidate is not None:
                pool.append(candidate)
                strengths[row["symbol"]] = (_number(row["change_pct"]) - sector["avg_return"], _number(row["ret5"]))
                break
    reference = [sorted(value[index] for value in strengths.values()) for index in (0, 1)]
    for candidate in pool:
        ranks = [100 * (bisect_left(reference[index], value) + bisect_right(reference[index], value))
                 / (2 * len(pool)) for index, value in enumerate(strengths[candidate["symbol"]])]
        candidate["score"] = round(sum(ranks) / len(ranks), 2)
        candidate["evidence"].append("评分为当前候选池内相对板块日收益与5日收益的等权秩，单候选或全池同值为50分；不是胜率")
    pool.sort(key=lambda row: (-row["score"], -sector_map[row["sector"]]["avg_return"],
                               row["stress_loss_pct"], row["symbol"]))
    return pool


def _display_sectors(sectors: list[dict], members: dict[str, set[str]], pool: list[dict]) -> list[dict]:
    # Screen the full sector universe first: three locked-limit hot groups must
    # not hide a fourth group with usable candidates. Deduplicate only now.
    usable = {row["sector"] for row in pool}
    selected = []
    for row in sorted(sectors, key=lambda item: item["name"] not in usable):
        group = members[row["name"]]
        if any(len(group & members[prior["name"]]) / len(group | members[prior["name"]]) > 0.8
               for prior in selected):
            continue
        selected.append(row)
        if len(selected) == 3:
            break
    return selected


def _allocate(pool: list[dict], risk_groups: dict[str, str], cap: float, risk: dict) -> list[dict]:
    result, sector_used = [], defaultdict(float)
    total = stress_used = 0.0
    for candidate in pool:
        sector = risk_groups[candidate["sector"]]
        weight = min(risk["single_cap"], risk["risk_per_trade"] / candidate["stress_loss_pct"],
                     max(0, cap - total), max(0, risk["sector_cap"] - sector_used[sector]),
                     max(0, risk["portfolio_risk_budget"] - stress_used) / candidate["stress_loss_pct"])
        weight = math.floor(weight * 1e6) / 1e6
        if weight <= 0:
            continue
        candidate["max_position"] = weight
        result.append(candidate)
        total += weight
        stress_used += weight * candidate["stress_loss_pct"]
        sector_used[sector] += weight
        if len(result) >= risk["max_candidates"]:
            break
    return result


def _scenarios(maximum: float) -> list[dict]:
    result = []
    for horizon in (1, 2, 3):
        for scenario, condition, action, factor in (
            ("走强", "市场广度、板块承接与候选确认同时改善", "仅在原触发区和组合上限内执行，拒绝高开追入", 1),
            ("分歧", "板块仍相对强势，但市场广度或溢价未同步改善", "暂缓新增；已有可卖仓位按原规则收缩风险", 0.5),
            ("走弱", "跌停扩散、板块失去承接或候选结构失效", "取消未触发计划；已有仓位在符合T+1及流动性约束时退出", 0),
        ):
            if horizon == 2:
                action = "不重新启用昨日未触发的入场计划；" + action
            elif horizon == 3:
                action = "本期计划到期，不延长超短线持有；有仓位时按可成交条件退出"
                factor = 0
            result.append({
                "horizon": horizon, "label": f"第 {horizon} 个交易日", "scenario": scenario,
                "condition": condition, "action": action,
                "max_position": math.floor(maximum * factor * 1e6) / 1e6,
            })
    return result


def analyze_snapshot(snapshot: dict, risk: dict | None = None) -> dict:
    """Return a deterministic research report without touching inputs or storage."""
    config = _risk_config(risk)
    current = _metrics(snapshot.get("metrics") or {})
    previous = _metrics(snapshot.get("previous") or {})
    quality = snapshot.get("quality")
    if quality not in {"ready", "limited", "unavailable"}:
        quality = "unavailable"
    if current["breadth_up"] is None or current["median_return"] is None:
        quality = "unavailable"
    state, matches = _market_state(current, previous)
    mode = "panic_repair" if matches["panic_repair"] else ("trend_pullback" if matches["trend_pullback"] else None)
    trend_cap = 0.4 * min(1, max(0, current["index_ret20"] or 0) / 0.04)
    cap = min(config["total_cap"], 0.2 if mode == "panic_repair" else trend_cap) if mode else 0.0
    short_index_missing = current["index_return"] is None or current["index_ret5"] is None
    if short_index_missing:
        cap = min(cap, 0.1)
    if quality == "limited":
        cap = min(cap, 0.1)
    if quality == "unavailable" or matches["crowded_exit"] or state["phase"] in {"退潮", "冰点"}:
        cap = 0.0
    stocks = _stocks(snapshot)
    all_sectors, members = _sectors(stocks, current["median_return"])
    pool = _candidate_pool(stocks, all_sectors, mode, config) if cap > 0 else []
    sectors = _display_sectors(all_sectors, members, pool)
    risk_groups = _risk_groups(sectors, members)
    # Reassign an overlapping membership to a retained sector before allocating.
    pool = _candidate_pool(stocks, sectors, mode, config) if cap > 0 else []
    candidates = _allocate(pool, risk_groups, cap, config)
    maximum = round(sum(row["max_position"] for row in candidates), 6)
    stress_loss = sum(row["max_position"] * row["stress_loss_pct"] for row in candidates)
    reason = (
        "数据不足，暂停生成新增仓位" if quality == "unavailable" else
        "短期指数显著转弱，风险否决优先于中期趋势，暂停新增仓位" if matches["short_index_risk"] else
        "昨日强势股负反馈或退潮风险成立，封板率不能抵消，暂停新增仓位" if state["phase"] == "退潮" else
        "一致兑现风险成立，暂停新增仓位并复核已有仓位退出条件" if matches["crowded_exit"] else
        "组合压力预算为零，暂停新增仓位" if config["portfolio_risk_budget"] == 0 else
        "仓位配置上限为零，暂停新增仓位" if any(config[key] == 0 for key in ("total_cap", "single_cap", "sector_cap")) else
        "没有同时满足模式、板块与交易资格的候选，新增仓位为零" if not candidates else
        "仓位下限为零；仅在下一交易日全部确认条件满足后，按单股、板块和压力损失预算分配"
    )
    limitations = list(snapshot.get("limitations") or [])
    limitations.extend([
        f"{RULE_VERSION} 是尚未验证的研究规则，阈值和仓位不是已校准概率或最优参数",
        "散户心理及资金行为是公开量价的假设解释，不能确认投资者身份、真实筹码成本或主力意图",
        "缺少账户持仓输入，仓位仅表示本期模型的新增敞口上限，不能推导账户应卖出的数量",
        "压力损失包含隔夜缓冲和成本假设，实际跳空、T+1与涨跌停可能使损失超过预算",
        "候选价格仅适用于未发生除权等价格标尺变化的下一交易日，变化后需重新生成计划",
        "周期阶段只由当前及前日可见证据判断，可以跳转或待确认，不假定固定循环天数",
        "板块按本地可用成员聚合，排除超过600只的大组；高度重叠板块去重或共用风险预算",
        "板块5日收益要求至少3个样本且覆盖80%；先筛全板块候选，再选最多3个可观察方向",
        "压力价只取已知5日高点；按触发上限和0.3%研究成本缓冲计算空间/结构损失比，至少1.0才观察，不代表预期收益",
        "趋势风险上限随指数20日正收益在0至4%区间线性增加至40%；全部数值待样本外验证",
        "组合压力损失为各候选仓位乘压力损失率的合计，另受独立预算限制；实际亏损仍可能超出",
    ])
    if short_index_missing:
        limitations.append("指数当日或5日收益缺失，短期风险无法完整核验，新增敞口上限降为10%")
    if quality == "limited":
        limitations.append("输入覆盖有限，市场风险上限降为10%；缺项没有按零值补齐")
    if snapshot.get("metadata_scope") == "historical_unverified":
        limitations.append("历史交易资格与板块归属未经时点核验，仅用于历史研究，不作为当时可交易名单")
    elif snapshot.get("metadata_scope") == "current_observation":
        limitations.append("板块与交易资格来自本次生成时的当前观察，不代表行情日期收盘时已经可见")
    return {
        "as_of": snapshot.get("as_of"), "cutoff": snapshot.get("cutoff"),
        "input_version": snapshot.get("input_version"), "quality": quality,
        "research_only": True, "summary": f"{state['trend']}背景，{state['phase']}。{reason}。",
        "market_state": state,
        "allocation": {
            "min": 0.0, "max": maximum, "total_cap": cap,
            "single_cap": config["single_cap"], "sector_cap": config["sector_cap"],
            "risk_per_trade": config["risk_per_trade"], "reason": reason,
            "portfolio_risk_budget": config["portfolio_risk_budget"],
            "estimated_stress_loss": round(stress_loss, 9),
        },
        "hypotheses": _hypotheses(matches, state, _facts(current, previous)),
        "scenarios": _scenarios(maximum), "sectors": sectors, "candidates": candidates,
        "evidence": copy.deepcopy(snapshot.get("evidence") or []),
        "psychology": analyze_psychology(snapshot),
        "limitations": list(dict.fromkeys(limitations)), "rule_version": RULE_VERSION,
    }
