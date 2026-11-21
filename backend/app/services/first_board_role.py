"""首板候选在所属行业/概念中的角色: 龙头与中军。

龙头 = 某个概念内今日最先触板 (并列也算), 且该概念今日涨停成员不少于 3 只, 依据交易所
事件流的触板时间, 事件流不可靠时待确认, 行业不参与龙头; 中军 = 板块内流通市值或当日
成交额排名进入前 N (行业与概念都算)。成员沿用首板联动的当前归属 (AttentionData.groups),
只服务盘中观察过滤, 不得注入历史研究。任一板块命中即算; 行情覆盖不足的板块既不判是也
不判否, 由调用方按待确认处理。
"""
from __future__ import annotations

import math
from collections.abc import Collection, Iterable, Mapping
from datetime import datetime

import polars as pl

ROLE_LABELS = {"leader": "龙头", "core": "中军"}
MIN_VALID_MEMBERS = 5
MIN_COVERAGE = 0.8
MIN_LIMIT_UP_MEMBERS = 3  # 概念内涨停成员至少这么多才算有板块效应, 才可能有龙头


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def float_shares_map(instruments: pl.DataFrame) -> dict[str, float]:
    """维表最新流通股本; 当日盘中排名使用最新股本即可, 缺失或非正值不参与。"""
    if instruments is None or instruments.is_empty() or not {"symbol", "float_shares"}.issubset(instruments.columns):
        return {}
    output = {}
    for symbol, shares in instruments.select("symbol", "float_shares").iter_rows():
        value = _number(shares)
        if isinstance(symbol, str) and value is not None and value > 0:
            output[symbol.strip().upper()] = value
    return output


def _quote_map(current: pl.DataFrame) -> dict[str, dict]:
    """唯一且涨幅有限的报价; 重复代码整只剔除, 不挑其中一条。"""
    if current.is_empty() or not {"symbol", "change_pct"}.issubset(current.columns):
        return {}
    if current.schema["change_pct"] == pl.Boolean:
        return {}
    columns = [column for column in ("symbol", "change_pct", "raw_close", "amount") if column in current.columns]
    counts: dict[str, int] = {}
    rows: dict[str, dict] = {}
    for row in current.select(columns).iter_rows(named=True):
        symbol = str(row["symbol"] or "").strip().upper()
        counts[symbol] = counts.get(symbol, 0) + 1
        change = _number(row["change_pct"])
        if symbol and change is not None:
            price, amount = _number(row.get("raw_close")), _number(row.get("amount"))
            rows[symbol] = {"change": change, "price": price if price and price > 0 else None,
                            "amount": amount if amount is not None and amount >= 0 else None}
    return {symbol: row for symbol, row in rows.items() if counts[symbol] == 1}


def _rank(values: dict[str, float], symbol: str, member_count: int) -> int | None:
    """并列按同名次; 有效样本或覆盖率不足时不给名次。"""
    own = values.get(symbol)
    if own is None or len(values) < MIN_VALID_MEMBERS or len(values) / member_count < MIN_COVERAGE:
        return None
    return 1 + sum(value > own for value in values.values())


def _unknown(reason: str) -> dict:
    return {"leader": None, "core": None, "matches": [], "reason": reason}


def classify_roles(current: pl.DataFrame, groups: list[dict], float_shares: dict[str, float],
                   symbols: Iterable[str], *, core_top_n: int,
                   touch: Mapping[str, datetime] | None = None, opened: Collection[str] = (),
                   touch_reliable: bool = False) -> dict[str, dict]:
    """返回 {symbol: {leader, core, matches, reason}}, leader/core 为 True/False/None。

    touch 为今日首次触板时间, opened 为最近状态是开板的股票, touch_reliable 表示事件流今日有数据且无缺口;
    不可靠时龙头一律待确认, 不退回涨幅排名。
    """
    wanted = {str(symbol).strip().upper() for symbol in symbols}
    output = {symbol: _unknown("缺少可确认的行业或概念归属，角色待确认") for symbol in wanted}
    if not wanted or not groups:
        return output
    quotes = _quote_map(current)
    touched = {str(symbol).strip().upper(): at for symbol, at in (touch or {}).items()}
    opened_set = {str(symbol).strip().upper() for symbol in opened}
    by_symbol: dict[str, list[dict]] = {symbol: [] for symbol in wanted}
    for group in groups:
        members = group.get("members") or []
        hits = wanted.intersection(members)
        if not hits:
            continue
        member_quotes = {member: quotes[member] for member in members if member in quotes}
        changes = {member: quote["change"] for member, quote in member_quotes.items()}
        caps = {member: quote["price"] * float_shares[member] for member, quote in member_quotes.items()
                if quote["price"] is not None and member in float_shares}
        amounts = {member: quote["amount"] for member, quote in member_quotes.items() if quote["amount"] is not None}
        size = len(members)
        limit_up = [member for member in members if member in touched and member not in opened_set]
        for symbol in hits:
            change_rank = _rank(changes, symbol, size)
            cap_rank, amount_rank = _rank(caps, symbol, size), _rank(amounts, symbol, size)
            ranks = [rank for rank in (cap_rank, amount_rank) if rank is not None]
            core = (True if any(rank <= core_top_n for rank in ranks)
                    else False if len(ranks) == 2 else None)
            leader, touch_at = None, None
            if group.get("kind") == "concept" and touch_reliable and symbol in touched:
                # 龙头 = 概念内今日最先触板, 且该概念涨停成员够多; 已开板的成员不计入, 也不能当基准。
                leader = (symbol in limit_up and len(limit_up) >= MIN_LIMIT_UP_MEMBERS
                          and touched[symbol] <= min(touched[member] for member in limit_up))
                touch_at = touched[symbol].isoformat() if leader else None
            by_symbol[symbol].append({
                "id": group.get("id"), "kind": group.get("kind"), "name": group.get("name"),
                "member_count": size, "valid_count": len(changes),
                "leader": leader, "core": core, "limit_up_count": len(limit_up) if leader is not None else None,
                "touch_at": touch_at,
                "change_rank": change_rank, "cap_rank": cap_rank, "amount_rank": amount_rank,
            })
    for symbol, evidence in by_symbol.items():
        if not evidence:
            continue
        concepts = [item for item in evidence if item["kind"] == "concept"]
        leader_reason = None
        if not touch_reliable:
            leader, leader_reason = None, "交易所事件流不可用或有缺口，龙头待确认"
        elif symbol not in touched:
            leader, leader_reason = None, "该股尚无触板记录，龙头待确认"
        elif not concepts:
            leader, leader_reason = None, "缺少可确认的概念归属，龙头待确认"
        else:
            leader = any(item["leader"] for item in concepts)
        cores = [item["core"] for item in evidence]
        core = True if True in cores else None if None in cores else False
        matches = [{key: item[key] for key in ("id", "kind", "name", "change_rank", "cap_rank", "amount_rank")}
                   | {"roles": [role for role in ("leader", "core") if item[role] is True],
                      "limit_up_count": item["limit_up_count"] if item["leader"] is True else None,
                      "touch_at": item["touch_at"] if item["leader"] is True else None}
                   for item in evidence if item["leader"] is True or item["core"] is True]
        if matches:
            reason = f"在 {len(matches)} 个板块中命中龙头或中军" + (f"；{leader_reason}" if leader_reason else "")
        else:
            unknown = sum(item["core"] is None for item in evidence)
            parts = [leader_reason] if leader_reason else []
            if unknown:
                parts.append(f"{len(evidence)} 个所属板块中有 {unknown} 个行情或股本覆盖不足，角色待确认")
            reason = "；".join(parts) if parts else f"{len(evidence)} 个所属板块中均不是龙头或中军"
        output[symbol] = {"leader": leader, "core": core, "matches": matches, "reason": reason}
    return output


def role_gate(role: dict, selected: list[str] | None) -> tuple[bool, bool, str | None]:
    """返回 (命中, 确定未命中, 阻断原因); selected 为 None 表示观察全部个股。"""
    if not selected:
        return True, False, None
    values = [role.get(name) for name in selected]
    if True in values:
        return True, False, None
    labels = "/".join(ROLE_LABELS[name] for name in selected)
    if all(value is False for value in values):
        return False, True, f"不属于所选角色（{labels}）"
    return False, False, f"所选角色（{labels}）待确认：{role.get('reason') or '板块数据不足'}"
