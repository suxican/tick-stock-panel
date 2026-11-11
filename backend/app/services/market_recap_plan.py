"""AI 复盘「明日剧本」的结构化与次日系统核验。

问题: 剧本由 AI 写、次日也由 AI 自评, 自评不可靠且阈值无依据。本模块:
1. 规定一套可由系统计算的触发指标词表 (PLAN_METRICS), 提示词要求 AI 在正文末尾
   附一个 ```plan JSON 代码块, 条件只能用词表指标;
2. 归档时把代码块解析为 report["plan"] 并从正文剥离 (split_plan_block);
3. 下一篇复盘时用复盘日真实数据逐条核验 (evaluate_plan), 结果写进快照 plan_check;
4. 汇总归档中的历次核验得到主判断命中率与剧本覆盖率 (plan_stats)。

另含开盘半小时成交额 (09:30–10:00 分钟K合计) 的计算: 分区覆盖不完整
(如盘中只同步了一部分) 时返回 None, 宁缺勿错。
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date, time
from pathlib import Path

import polars as pl

logger = logging.getLogger(__name__)

# ───────────────────────── 触发指标词表 ─────────────────────────
# key → (标签, 单位, 是否全天指标)。全天指标在盘中快照时尚未定型, 核验记为待定。
PLAN_METRICS: dict[str, tuple[str, str, bool]] = {
    "amount_30m": ("开盘半小时成交额", "亿", False),
    "amount": ("全天成交额", "亿", True),
    "up_count": ("上涨家数", "家", True),
    "limit_up": ("涨停家数", "家", True),
    "limit_down": ("跌停家数", "家", True),
    "broken": ("炸板家数", "家", True),
    "seal_rate": ("封板率", "%", True),
    "max_boards": ("最高连板", "板", True),
    "promo_rate": ("连板晋级率", "%", True),
    "boards": ("连板数", "板", True),  # 需 symbol: 某只股票当日连板数(未涨停=0)
}
# 快照 metrics 的 key 与词表 key 不同的映射
_SNAPSHOT_KEY = {"amount": "amount_yi"}
_OPS = {">": lambda a, b: a > b, ">=": lambda a, b: a >= b,
        "<": lambda a, b: a < b, "<=": lambda a, b: a <= b}
_OP_TEXT = {">": ">", ">=": "≥", "<": "<", "<=": "≤"}
MAX_SCENARIOS = 3
MAX_CONDITIONS = 3

_PLAN_BLOCK_RE = re.compile(r"\n?```plan\s*\n(.*?)(?:\n```|\Z)", re.S)


# ───────────────────────── 解析与校验 ─────────────────────────

def validate_plan(raw: object) -> dict | None:
    """清洗 AI 给出的剧本 JSON; 非法条件丢弃, 无有效情景返回 None。"""
    if not isinstance(raw, dict) or not isinstance(raw.get("scenarios"), list):
        return None
    scenarios: list[dict] = []
    primary_seen = False
    for sc in raw["scenarios"][:MAX_SCENARIOS]:
        if not isinstance(sc, dict):
            continue
        conds = []
        for c in (sc.get("conditions") or [])[:MAX_CONDITIONS]:
            if not isinstance(c, dict):
                continue
            metric, op, value = c.get("metric"), c.get("op"), c.get("value")
            if metric not in PLAN_METRICS or op not in _OPS:
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            cond = {"metric": metric, "op": op, "value": float(value)}
            if metric == "boards":
                sym = str(c.get("symbol") or "").strip().upper()
                if not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", sym):
                    continue
                cond["symbol"] = sym
                cond["name"] = str(c.get("name") or "")[:20]
            conds.append(cond)
        if not conds:
            continue
        primary = bool(sc.get("primary")) and not primary_seen
        primary_seen = primary_seen or primary
        scenarios.append({
            "name": str(sc.get("name") or f"情景{len(scenarios) + 1}")[:20],
            "primary": primary,
            # all: 条件同时成立; any: 任一成立(对应表格里的"或")
            "match": "any" if sc.get("match") == "any" else "all",
            "conditions": conds,
        })
    return {"scenarios": scenarios} if scenarios else None


def split_plan_block(content: str) -> tuple[str, dict | None]:
    """正文 → (剥离 ```plan 代码块后的正文, 校验后的剧本或 None)。"""
    if not content:
        return content, None
    m = _PLAN_BLOCK_RE.search(content)
    if not m:
        return content, None
    clean = (content[:m.start()] + content[m.end():]).rstrip() + "\n"
    try:
        plan = validate_plan(json.loads(m.group(1)))
    except (ValueError, TypeError):
        logger.info("recap plan block is not valid JSON; dropped")
        plan = None
    return clean, plan


# ───────────────────────── 开盘半小时成交额 ─────────────────────────

_OPEN_START, _OPEN_END = time(9, 30), time(10, 0)
_OPEN_MINUTES = 31          # 09:30..10:00 含首尾
_MIN_SYMBOL_COVERAGE = 0.9  # 每分钟覆盖的标的数 ≥ 当日有成交个股数的 90%
_OPEN_CACHE: dict[str, tuple[tuple[int, int, int], float | None]] = {}


def opening_amount_yi(data_dir: Path, day: str, expected_symbols: int) -> float | None:
    """某日 09:30–10:00 全市场分钟成交额合计(亿); 分区缺失或覆盖不完整返回 None。

    expected_symbols: 当日有成交的个股数(涨+跌家数)。每分钟覆盖须 ≥ 其 90% ——
    分区可能只同步了部分标的(实测 1995/5573), 只做分区内自比会把残缺当完整。
    """
    if not expected_symbols or expected_symbols <= 0:
        return None
    p = Path(data_dir) / "kline_minute" / f"date={day}" / "part.parquet"
    try:
        st = p.stat()
    except OSError:
        return None
    sig = (st.st_mtime_ns, st.st_size, int(expected_symbols))
    hit = _OPEN_CACHE.get(str(p))
    if hit and hit[0] == sig:
        return hit[1]
    value: float | None = None
    try:
        per_min = (
            pl.scan_parquet(p)
            .select(["symbol", "datetime", "amount"])
            .filter(pl.col("datetime").dt.time().is_between(_OPEN_START, _OPEN_END))
            .group_by("datetime")
            .agg(pl.col("amount").sum().alias("amount"), pl.col("symbol").n_unique().alias("n"))
            .collect()
        )
        if per_min.height == _OPEN_MINUTES and per_min["n"].min() >= expected_symbols * _MIN_SYMBOL_COVERAGE:
            value = float(per_min["amount"].sum()) / 1e8
    except Exception as e:  # noqa: BLE001
        logger.debug("opening amount failed for %s: %s", day, e)
    _OPEN_CACHE[str(p)] = (sig, value)
    return value


# ───────────────────────── 次日核验 ─────────────────────────

def _fmt_num(v: float, unit: str) -> str:
    return f"{v:.0f}{unit}" if unit != "%" else f"{v:.1f}%"


def condition_label(c: dict) -> str:
    label, unit, _ = PLAN_METRICS[c["metric"]]
    if c["metric"] == "boards":
        label = f"{c.get('name') or c['symbol']}连板数"
    return f"{label} {_OP_TEXT[c['op']]} {_fmt_num(c['value'], unit)}"


def _symbol_boards(repo, day: date, symbols: list[str]) -> dict[str, int] | None:
    """复盘日各标的连板数(未涨停为 0); enriched 缓存不覆盖时返回 None。"""
    getter = getattr(repo, "get_enriched_range", None)
    if getter is None or not symbols:
        return None
    try:
        df = getter(day, day, symbols=symbols, columns=["symbol", "date", "consecutive_limit_ups"])
    except Exception as e:  # noqa: BLE001
        logger.debug("plan check: enriched boards unavailable: %s", e)
        return None
    if df is None:
        return None
    got = {r["symbol"]: int(r.get("consecutive_limit_ups") or 0) for r in df.to_dicts()}
    # 缓存覆盖该日但标的当日无行(停牌等) → 无法判定, 不当作 0
    return got


def evaluate_plan(plan: dict, snapshot: dict, repo, *, from_as_of: str) -> dict:
    """用复盘日(snapshot.as_of)实际数据核验上一篇剧本。

    条件状态: hit / miss / pending(数据未定型或缺失)。
    情景状态(match=all): 任一 miss → miss; 否则任一 pending → pending; 否则 hit。
    情景状态(match=any): 任一 hit → hit; 否则任一 pending → pending; 否则 miss。
    """
    day = date.fromisoformat(snapshot["as_of"])
    intraday = bool(snapshot.get("intraday"))
    today = {m["key"]: m.get("today") for m in snapshot.get("metrics") or []}
    syms = sorted({c["symbol"] for sc in plan["scenarios"] for c in sc["conditions"] if c["metric"] == "boards"})
    boards = None if intraday else _symbol_boards(repo, day, syms)

    scenarios = []
    for sc in plan["scenarios"]:
        conds = []
        for c in sc["conditions"]:
            _, unit, full_day = PLAN_METRICS[c["metric"]]
            if full_day and intraday:
                actual = None
            elif c["metric"] == "boards":
                actual = None if boards is None else boards.get(c["symbol"])
            else:
                actual = today.get(_SNAPSHOT_KEY.get(c["metric"], c["metric"]))
            status = "pending" if actual is None else ("hit" if _OPS[c["op"]](actual, c["value"]) else "miss")
            conds.append({
                "label": condition_label(c),
                "actual": actual,
                "actual_text": "—" if actual is None else _fmt_num(actual, unit),
                "status": status,
            })
        states = {c["status"] for c in conds}
        match = sc.get("match", "all")
        if match == "any":
            status = "hit" if "hit" in states else ("pending" if "pending" in states else "miss")
        else:
            status = "miss" if "miss" in states else ("pending" if "pending" in states else "hit")
        scenarios.append({"name": sc["name"], "primary": sc["primary"], "match": match,
                          "status": status, "conditions": conds})
    return {"from_as_of": from_as_of, "as_of": snapshot["as_of"], "scenarios": scenarios}


def plan_stats(checks: list[dict]) -> dict:
    """历次核验 → 主判断命中率 / 剧本覆盖率。仅统计已定型(无待定)的核验。"""
    primary_total = primary_hits = covered_total = covered = 0
    for chk in checks:
        scs = chk.get("scenarios") or []
        if not scs:
            continue
        prim = next((s for s in scs if s.get("primary")), None)
        if prim and prim.get("status") != "pending":
            primary_total += 1
            primary_hits += prim.get("status") == "hit"
        if all(s.get("status") != "pending" for s in scs):
            covered_total += 1
            covered += any(s.get("status") == "hit" for s in scs)
    return {
        "primary_total": primary_total, "primary_hits": primary_hits,
        "covered_total": covered_total, "covered": covered,
    }


def archived_checks(reports: list[dict], before: str) -> list[dict]:
    """归档报告中复盘日早于 before 的历次核验(同一复盘日取最后生成的一篇)。"""
    latest: dict[str, dict] = {}
    for r in reports:
        as_of = str(r.get("as_of") or "")
        if not as_of or as_of >= before:
            continue
        if as_of not in latest or str(r.get("created_at") or "") > str(latest[as_of].get("created_at") or ""):
            latest[as_of] = r
    out = []
    for r in latest.values():
        chk = (r.get("snapshot") or {}).get("plan_check")
        if chk:
            out.append(chk)
    return out


# ───────────────────────── 提示词片段 ─────────────────────────

def plan_vocabulary_block(snapshot: dict | None) -> str:
    """给模型的触发指标词表 + 今日基准值(阈值必须以此为参照)。"""
    metrics = {m["key"]: m for m in (snapshot or {}).get("metrics") or []}
    lines = ["## 明日剧本可用的触发指标(结构化代码块只能用这些 metric)", "| metric | 含义 | 今日 | 前5日均 |", "|---|---|---|---|"]
    for key, (label, unit, _) in PLAN_METRICS.items():
        if key == "boards":
            continue
        m = metrics.get(_SNAPSHOT_KEY.get(key, key)) or {}
        today = "—" if m.get("today") is None else _fmt_num(m["today"], unit)
        avg = "—" if m.get("avg5") is None else _fmt_num(m["avg5"], unit)
        lines.append(f"| {key} | {label}({unit}) | {today} | {avg} |")
    lines.append("| boards | 某只股票当日连板数(需 symbol, 未涨停=0) | 见下方连板股代码 | — |")
    syms = []
    for t in (snapshot or {}).get("ladder") or []:
        if t.get("boards", 0) >= 2:
            syms += [f"{s.get('name')}={s.get('symbol')}({t['boards']}板)" for s in t.get("stocks") or []]
    if syms:
        lines.append("连板股代码: " + "、".join(syms[:15]))
    return "\n".join(lines)


def plan_check_prompt_block(check: dict | None, stats: dict | None) -> str:
    """系统核验结果 → 提示词文本(模型只解释原因, 不得改判)。"""
    if not check:
        return ""
    mark = {"hit": "✅命中", "miss": "❌落空", "pending": "➖待定"}
    lines = [f"## 昨日剧本系统核验(来自 {check['from_as_of']} 的剧本, 已由系统按实际数据判定, 不得改判)"]
    for sc in check["scenarios"]:
        joiner = " 或 " if sc.get("match") == "any" else "; "
        conds = joiner.join(f"{c['label']} 实际 {c['actual_text']} {mark[c['status']]}" for c in sc["conditions"])
        lines.append(f"- {sc['name']}{'(主判断)' if sc['primary'] else ''}: {mark[sc['status']]} — {conds}")
    if stats and stats.get("primary_total"):
        lines.append(f"- 历史主判断命中 {stats['primary_hits']}/{stats['primary_total']}")
    return "\n".join(lines)
