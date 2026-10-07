"""Bounded institutional disclosure evidence, separate from price behavior inference.

Reuses the existing optional dragon_tiger service and its cache/provider routing.
The source has no publication timestamp: fetched_at records this observation only,
and must never be used to backdate what a historical plan could have known.
"""
from __future__ import annotations

import math
import re
from datetime import date, datetime, time
from pathlib import Path

from app.market_time import CN_TZ
from app.services import dragon_tiger

_SYMBOL = re.compile(r"[0-9]{6}\.(?:SH|SZ|BJ)")
_LIMIT = 100
_ORG_FIELDS = ("org_net_value", "org_net_rate", "org_buy_num", "org_sell_num")
_LIMITATIONS = (
    "仅包含龙虎榜已披露机构席位,不代表全部机构持仓或全市场资金方向。",
    "这是盘后披露证据,不是盘中实时资金流;机构席位不能用于识别量化交易身份。",
    "来源未提供可核验的披露时间;本次获取时间不能证明历史分析时点已经可知。",
    "单日榜与三日榜分别展示,统计窗口可能重叠,不可叠加净额或席位数。",
    "净额沿用现有龙虎榜服务的元口径;零值与未披露字段分开保留。",
    "机构净额占比的来源单位尚未核验,该字段暂不展示,不根据数值大小推断或缩放。",
)


def _date(value: object) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = date.fromisoformat(value)
        return parsed if parsed.isoformat() == value else None
    except ValueError:
        return None


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return float(value) if math.isfinite(value) else None
    except OverflowError:
        return None


def _count(value: object) -> int | None:
    number = _number(value)
    return int(number) if number is not None and number >= 0 and number.is_integer() else None


def _normalize_row(raw: object, actual: date) -> dict | None:
    if not isinstance(raw, dict):
        return None
    symbol = raw.get("thscode")
    if not isinstance(symbol, str) or _SYMBOL.fullmatch(symbol) is None:
        return None
    days = raw.get("range_days")
    if isinstance(days, bool) or not isinstance(days, int) or days not in (1, 3):
        return None
    # Some provider/cache versions carry a row date. It must agree with org,
    # not be silently relabelled with the all-board or requested date.
    if "trade_date" in raw and _date(raw["trade_date"]) != actual:
        return None
    values = {
        key: (_count(raw.get(key)) if key.endswith("_num") else _number(raw.get(key)))
        for key in _ORG_FIELDS
    }
    # The legacy source forwards org_net_rate without a documented unit.
    # Preserve the optional field but do not infer its scale from its value.
    values["org_net_rate"] = None
    if all(value is None for value in values.values()):
        return None
    name = raw.get("name")
    return {
        "symbol": symbol,
        "name": name[:100] if isinstance(name, str) and name.strip() else symbol,
        "trade_date": actual.isoformat(),
        "range_days": days,
        **values,
    }


def fetch_institutional_disclosures(data_dir: Path, *, now: datetime, target: date) -> dict:
    """Read one org-board observation on explicit refresh, never a live hot path.

    ``now`` must be timezone aware. ``target`` is the requested disclosure date,
    not an assertion that the evidence was available on that date. The optional
    source/cache may return an earlier org-board date; it stays visibly labelled.
    No sums across securities or reporting windows are calculated here.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must include a timezone")
    now = now.astimezone(CN_TZ)
    result = {
        "source": "fuyao/dragon_tiger/org",
        "source_label": "龙虎榜已披露机构席位",
        "requested_date": target.isoformat(),
        "trade_date": None,
        "fetched_at": now.isoformat(),
        "available_at": None,
        "point_in_time_verified": False,
        "realtime": False,
        "state": "no_data",
        "status": "暂无可用机构披露",
        "rows": [],
        "omitted_count": 0,
        "limitations": list(_LIMITATIONS),
    }

    def fail(state: str, status: str) -> dict:
        result.update(state=state, status=status)
        return result

    if target > now.date():
        return fail("invalid_data", "请求日期晚于本次观察日期,无法取得披露证据")
    try:
        payload = dragon_tiger.get_dragon_tiger(data_dir, target)
    except Exception:  # Optional evidence must not break behavior observations.
        return fail("no_data", "机构披露读取失败,本次未取得可用证据")
    if not isinstance(payload, dict):
        return fail("invalid_data", "机构披露响应格式无效")
    source_state = payload.get("state")
    if source_state == "source_unavailable":
        return fail("source_unavailable", "机构披露来源不可用,请在数据源配置中检查")
    if source_state == "no_data":
        return fail("no_data", "机构披露暂未取得,可能尚未发布或来源暂不可用")
    if source_state not in ("ok", "fallback_prev"):
        return fail("invalid_data", "机构披露来源状态无法确认")
    org = payload.get("org")
    if not isinstance(org, dict):
        return fail("invalid_data", "缺少机构子榜,不能用总榜代替机构披露")
    actual = _date(org.get("trade_date"))
    if actual is None or actual > target or actual > now.date():
        return fail("invalid_data", "机构子榜日期缺失或晚于请求日期,未展示其数值")
    if actual == now.date() and now.time() < time(15):
        return fail("invalid_data", "本交易日尚未收盘,未采用标记为当日盘后披露的数据")
    result["trade_date"] = actual.isoformat()
    board_all = payload.get("all")
    other_dates = [payload.get("trade_date")]
    if isinstance(board_all, dict):
        other_dates.append(board_all.get("trade_date"))
    if any(value is not None and _date(value) != actual for value in other_dates):
        result["limitations"].append("总榜与机构子榜日期不一致,本面板仅采用机构子榜实际日期。")

    raw_rows = org.get("stock_items")
    if not isinstance(raw_rows, list):
        return fail("invalid_data", "机构子榜明细格式无效")
    unique: dict[tuple[str, int], dict] = {}
    conflicts: set[tuple[str, int]] = set()
    for raw in raw_rows:
        row = _normalize_row(raw, actual)
        if row is None:
            continue
        key = (row["symbol"], row["range_days"])
        if key in conflicts:
            continue
        if key in unique and unique[key] != row:
            # Conflicting records lack a reliable source identifier. Choosing
            # either or summing them would invent evidence, so omit both.
            del unique[key]
            conflicts.add(key)
        else:
            unique[key] = row
    rows = sorted(
        unique.values(),
        key=lambda row: (
            row["org_net_value"] is None,
            -abs(row["org_net_value"] or 0),
            row["symbol"], row["range_days"],
        ),
    )[:_LIMIT]
    result.update(rows=rows, omitted_count=len(raw_rows) - len(rows))
    if result["omitted_count"]:
        result["limitations"].append(
            f"省略{result['omitted_count']}条缺少有效机构字段、标识或统计窗口异常、重复冲突或超出展示上限的记录。"
        )
    if not rows:
        return fail("no_data", "该期没有可展示的有效机构席位记录,不等于机构净额为零")
    fallback = source_state == "fallback_prev" or actual < target
    result.update(
        state="fallback_prev" if fallback else "ok",
        status=f"已回退至{actual.isoformat()}机构披露" if fallback else "机构披露已取得",
    )
    return result
