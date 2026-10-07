"""Deterministic daily competition evidence, kept separate from allocation rules.

Returns/breadth/shares are fractions, amount is CNY. Thresholds describe an
unvalidated research rule, not book-derived probabilities or net money flows.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from datetime import datetime
from statistics import median

from app.services.market_game_ecology_models import MarketEcology

VERSION = "ecology-v1"
_COVERAGE = .95


def _number(value):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def _stamp(value):
    try:
        stamp = datetime.fromisoformat(value)
        return stamp if stamp.tzinfo is not None else None
    except (TypeError, ValueError):
        return None


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:24]


def _stocks(snapshot):
    rows = [row for row in snapshot.get("stocks", []) if isinstance(row, dict) and row.get("symbol")]
    counts = Counter(row["symbol"] for row in rows)
    return sorted((row for row in rows if counts[row["symbol"]] == 1), key=lambda row: row["symbol"])


def _members(rows, scope):
    if scope != "current_observation":
        return {}
    return {row["symbol"]: sorted({name.strip() for name in row.get("sectors", [])
                                  if isinstance(name, str) and name.strip()}) for row in rows}


def _cross_section(snapshot):
    rows = _stocks(snapshot)
    valid = [row for row in rows if (amount := _number(row.get("amount"))) is not None and amount >= 0]
    total = sum(row["amount"] for row in valid)
    coverage = len(valid) / len(snapshot.get("stocks", [])) if snapshot.get("stocks") else 0
    hhi = sum((row["amount"] / total) ** 2 for row in valid) if total > 0 and coverage >= _COVERAGE else None
    return rows, valid, total, hhi, coverage


def _sector_rows(rows, total, members, market_return):
    groups = {}
    for row in rows:
        names = members.get(row["symbol"]) or ["未映射"]
        weight = 1 / len(names)
        for name in names:
            item = groups.setdefault(name, {"amount": 0., "count": 0, "valid_count": 0, "up": 0, "returns": 0.})
            item["amount"] += row["amount"] * weight
            item["count"] += 1
            change = _number(row.get("change_pct"))
            if change is not None:
                item["valid_count"] += 1
                item["returns"] += change
                item["up"] += change > 0
    result = []
    for name, item in groups.items():
        sufficient = item["valid_count"] > 0 and item["valid_count"] / item["count"] >= _COVERAGE
        breadth = item["up"] / item["valid_count"] if sufficient else None
        avg = item["returns"] / item["valid_count"] if sufficient else None
        relative = avg - market_return if avg is not None and market_return is not None else None
        status = "板块证据待确认"
        if name == "未映射":
            status = "缺少板块映射"
        elif breadth is not None and relative is not None:
            if breadth >= .6 and relative > 0:
                status = "上涨覆盖与相对表现偏强"
            elif breadth < .4 and relative < 0:
                status = "上涨覆盖与相对表现偏弱"
            else:
                status = "内部表现分化"
        result.append({"name": name, "member_count": item["count"], "amount_share": item["amount"] / total,
                       "share_change": None, "breadth": breadth, "avg_return": avg, "relative_return": relative,
                       "status": status, "evidence": ["重叠板块仅分摊成交额份额; 板块上涨覆盖与平均涨跌按有效成分股等权计算。"]})
    return sorted(result, key=lambda item: (-item["amount_share"], item["name"]))


def analyze_ecology(snapshot: dict, prior_snapshots=()) -> dict:
    """Read only the supplied cutoff-bounded snapshot and at most 20 archives.

    Prior snapshots must be original archived inputs, never reconstructed using
    today's memberships. Only the immediately preceding observed session with
    an identical stock universe and membership map can supply share changes.
    """
    as_of, cutoff = str(snapshot.get("as_of", "")), str(snapshot.get("cutoff", ""))
    metrics = snapshot.get("metrics") or {}
    rows, amount_rows, total, hhi, amount_coverage = _cross_section(snapshot)
    scope = snapshot.get("metadata_scope", "unknown")
    members = _members(rows, scope)
    universe = _digest([row["symbol"] for row in rows])
    membership = _digest(members) if members else None
    coverage = _number(snapshot.get("coverage"))
    if coverage is not None and not 0 <= coverage <= 1:
        coverage = None
    current_good = bool(rows) and coverage is not None and coverage >= _COVERAGE and snapshot.get("quality") != "unavailable"
    current_time = _stamp(cutoff)
    current_good = current_good and current_time is not None
    if not current_good:
        hhi = None
    changes = [_number(row.get("change_pct")) for row in rows]
    returns = [value for value in changes if value is not None]
    market_return = sum(returns) / len(returns) if rows and len(returns) / len(rows) >= _COVERAGE else None
    sectors = _sector_rows(amount_rows, total, members, market_return) if current_good and amount_coverage >= _COVERAGE and members and total > 0 else []
    prior_history = sorted((row for row in snapshot.get("history", []) if isinstance(row, dict) and str(row.get("date", "")) < as_of), key=lambda row: row["date"])
    history = prior_history[-20:]
    current_count = _number(metrics.get("stock_count"))
    baseline = []
    for row in history:
        amount, count, covered = (_number(row.get(key)) for key in ("amount", "stock_count", "coverage"))
        if amount is not None and amount > 0 and count and current_count and covered is not None and covered >= _COVERAGE and abs(count / current_count - 1) <= .05:
            baseline.append(amount)
    activity = None
    current_amount = _number(metrics.get("amount"))
    if current_good and (snapshot.get("calendar") or {}).get("verified") and len(history) == len(baseline) == 20 and len({row["date"] for row in history}) == 20 and current_amount is not None and current_amount >= 0:
        activity = current_amount / median(baseline)
    comparison_date, concentration_change = None, None
    previous_date = str(prior_history[-1]["date"]) if prior_history else None
    archives = [prior for prior in prior_snapshots if isinstance(prior, dict) and prior.get("as_of") == previous_date]
    # Duplicate archived generations are ambiguous: do not select a convenient
    # one or treat repeated observations as independent confirmation.
    if current_good and len(archives) == 1:
        prior = archives[0]
        prior_time = _stamp(prior.get("cutoff"))
        prior_rows, prior_amounts, prior_total, prior_hhi, prior_amount_coverage = _cross_section(prior)
        prior_coverage = _number(prior.get("coverage"))
        same_universe = [row["symbol"] for row in prior_rows] == [row["symbol"] for row in rows]
        if (prior_time is not None and prior_time < current_time and prior.get("as_of", "") < as_of
                and prior_coverage is not None and prior_coverage >= _COVERAGE and prior.get("quality") != "unavailable"
                and same_universe and prior_amount_coverage >= _COVERAGE):
            if hhi is not None and prior_hhi is not None:
                concentration_change = hhi - prior_hhi
                comparison_date = previous_date
            prior_members = _members(prior_rows, prior.get("metadata_scope"))
            if members and prior_members == members and prior_total > 0:
                previous_sectors = {item["name"]: item for item in _sector_rows(prior_amounts, prior_total, prior_members, None)}
                for item in sectors:
                    if item["name"] in previous_sectors:
                        item["share_change"] = item["amount_share"] - previous_sectors[item["name"]]["amount_share"]
    breadth = _number(metrics.get("breadth_up"))
    previous_breadth = _number((snapshot.get("previous") or {}).get("breadth_up"))
    state, label = "unconfirmed", "竞争环境待确认"
    evidence = []
    if activity is not None and breadth is not None:
        evidence.extend([f"成交额为前20个可比交易日中位数的{activity:.2f}倍。", f"有效日线样本上涨占比{breadth:.1%}。"])
        if activity >= 1.1 and breadth >= .6:
            state, label = "expanding", "交易参与扩散增强"
        elif activity <= .9 and breadth <= .35:
            state, label = "contracting", "交易参与收缩"
        elif (activity < 1 and concentration_change is not None and concentration_change > 0
              and previous_breadth is not None and breadth < previous_breadth):
            state, label = "concentrated", "缩量集中候选"
        elif .9 <= activity <= 1.1 and sum(abs(item["share_change"] or 0) for item in sectors if item["name"] != "未映射") >= .1:
            state, label = "rotation", "板块份额轮动候选"
    if concentration_change is not None:
        evidence.append(f"相同股票集合的成交集中度较{comparison_date}变化{concentration_change:+.6f}。")
    limitations = [
        "仅描述有效日线样本的竞争特征; 成交额不是新增资金或净流入, 无法证明严格存量市场。",
        "研究阈值尚未经样本外校准; 不参与入场授权、仓位增加或原计划改写。",
        "历史股票全集、首次公开时间尚未获PIT认证; 当前板块映射仅代表本次观察。",
        "成交活跃度按覆盖率与样本数量近似比较; 历史完整股票集合未归档时不声称完全同池。",
        "份额变化须有上一交易日原始归档及相同股票与板块映射, 缺失不按零变化处理。",
    ]
    if scope != "current_observation":
        limitations.append("历史回看不使用当前板块映射; 板块竞争信息保持缺失。")
    if not sectors:
        limitations.append("有效成交额或本次板块映射不足, 未计算板块份额。")
    result = {
        "version": VERSION, "state": state, "label": label, "as_of": as_of, "cutoff": cutoff,
        "metadata_scope": scope, "universe_version": universe, "membership_version": membership,
        "coverage": coverage, "comparison_date": comparison_date,
        "metrics": [
            {"id": "amount_activity", "label": "成交活跃度", "value": activity, "unit": "multiple", "sample_size": len(baseline), "reason": "前20个交易日均须有有效成交额、至少95%覆盖及可比样本数。"},
            {"id": "amount_hhi", "label": "成交集中度", "value": hhi, "unit": "hhi", "sample_size": len(amount_rows), "reason": "有效股票成交额份额平方和; 越高表示越集中, 不等于资金流入。"},
            {"id": "concentration_change", "label": "集中度变化", "value": concentration_change, "unit": "hhi", "sample_size": int(concentration_change is not None), "reason": "仅比较相邻交易日、相同股票集合的原始归档。"},
            {"id": "breadth_up", "label": "上涨覆盖", "value": breadth if current_good else None, "unit": "ratio", "sample_size": len(returns), "reason": "有效日线样本上涨占比。"},
            {"id": "amount_coverage", "label": "成交额字段覆盖", "value": amount_coverage, "unit": "ratio", "sample_size": len(amount_rows), "reason": "缺失与异常成交额不会替换为零。"},
        ],
        "sectors": sectors, "evidence": evidence,
        "interpretation": f"{label}。结合板块广度、份额与后续承接核验, 不直接推断交易者身份。",
        "limitations": limitations,
    }
    return MarketEcology.model_validate(result).model_dump()
