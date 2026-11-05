"""Deterministic, read-only holding research, separate from execution and accounting.

Thresholds are research defaults rather than validated return forecasts. All
prices exposed as triggers are anchored to a verified unadjusted daily basis.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
from collections import defaultdict
from copy import deepcopy
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import polars as pl

from app.indicators.levels import compute_levels
from app.market_time import CN_TZ
from app.services.data_integrity import CLOSE_CUTOFF
from app.services.huichun_calendar import BUNDLED_CALENDAR_PATH, load_market_calendar
from app.strategy import lots

logger = logging.getLogger(__name__)
MAX_HOLDINGS = 400
QUOTE_MAX_AGE_SECONDS = 60
MIN_SECTOR_COVERAGE = .8
_TECH_KEYS = ("support", "resistance", "ma20", "ma60", "atr14", "return5", "return20")


def _number(value, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        return None
    number = float(value)
    return number if math.isfinite(number) and (not positive or number > 0) else None


def _day(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None
    return None


def _stable(value):
    if isinstance(value, dict):
        return {str(k): _stable(v) for k, v in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_stable(v) for v in value]
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, (float, Decimal)):
        return float(value) if math.isfinite(value) else str(value)
    return value


def _hash(value):
    encoded = json.dumps(_stable(value), sort_keys=True, ensure_ascii=False,
                         separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _key(source, account_id, symbol):
    return "h_" + _hash([source, account_id, symbol])[:24]


def _active_facts(plans):
    # Monitor bookkeeping contains new quote/check timestamps every few
    # seconds. Only execution/resource facts invalidate a research source.
    fields = ("id", "account_id", "symbol", "direction", "quantity", "state", "version",
              "leg1_filled", "leg2_filled", "unmatched_qty", "exposure_qty",
              "reserved_cash", "reserved_qty")
    return sorted(({key: plan.get(key) for key in fields} for plan in plans),
                  key=lambda plan: (str(plan.get("account_id")), plan["id"]))


def _catalog(repo):
    return {str(row["symbol"]).upper(): row for row in repo.get_instruments().to_dicts()}


def read_inputs(repo, position_service, source, account_id=""):
    """Read one explicit source, with stable identity and a content version.

    An invalid batch makes the whole symbol unquantifiable. Neither snapshot
    time nor a subset of otherwise valid lots supplies missing holding facts.
    """
    if source not in {"lots", "position_t"}:
        raise ValueError("持仓来源必须为批次提醒或核对持仓")
    warnings, accounts, exposure_verified = [], [], True
    if position_service is not None:
        try:
            accounts = [{"id": a["id"], "name": a["name"]}
                        for a in position_service.store.list_accounts()]
        except Exception:  # noqa: BLE001 - lots research remains available without an account service
            logger.warning("持仓研究账户目录暂不可用", exc_info=True)
            if source == "position_t":
                raise ValueError("核对持仓账户暂不可用") from None
            warnings.append("核对持仓账户列表暂不可用")
            warnings.append("无法核对其他账户同股做 T 敞口，暂停采纳批次操作计划")
            exposure_verified = False
    if source == "position_t" and position_service is None:
        raise ValueError("持仓做 T 服务未启动，暂不可读取核对持仓")
    catalog = {}
    try:
        catalog = _catalog(repo)
    except Exception:  # noqa: BLE001 - retain holdings but block unverified security identities
        logger.warning("持仓研究证券目录暂不可用", exc_info=True)
        warnings.append("证券目录暂不可用，证券身份待核验")
    positions, fact = [], {}
    if source == "position_t":
        if not account_id:
            warnings.append("请先选择核对持仓账户")
        else:
            if account_id not in {a["id"] for a in accounts}:
                raise ValueError("所选持仓账户不存在")
            store = position_service.store
            account = store.get_account(account_id)
            raw_positions = store.list_positions(account_id)
            active = [p for p in store.list_plans(account_id)
                      if p.get("state") not in {"completed", "cancelled"}]
            fact = {"account": account, "positions": sorted(raw_positions, key=lambda p: p["symbol"]),
                    "active_plans": _active_facts(active)}
            for raw in raw_positions:
                symbol = str(raw.get("symbol") or "").strip().upper()
                qty, cost = _number(raw.get("qty"), positive=True), _number(raw.get("cost_price"), positive=True)
                valid = qty is not None and qty.is_integer() and cost is not None
                positions.append({
                    "key": _key(source, account_id, symbol), "symbol": symbol,
                    "name": raw.get("name") or catalog.get(symbol, {}).get("name") or symbol,
                    "qty": int(qty) if valid else None, "cost_price": cost if valid else None,
                    "cost_method": raw.get("cost_method", "manual_reference"),
                    "opened_on": None, "latest_added_on": None, "input_valid": valid,
                    "sellable_qty": raw.get("effective_sellable"),
                    "snapshot_at": raw.get("snapshot_at"), "reconciled": raw.get("reconciled", False),
                    "account_snapshot_at": account.get("snapshot_at"),
                    "account_reconciled": account.get("reconciled", False),
                    "snapshot_max_age_seconds": account.get("config", {}).get("position_max_age_seconds", 900),
                    "reserved_qty": raw.get("reserved_qty", 0),
                    "trade_date": raw.get("trade_date"),
                    "active_t_plan": any(p["symbol"] == symbol for p in active),
                    "warnings": [] if valid else ["持仓数量或参考成本无效，无法完整计算仓位"],
                })
    else:
        account_id = ""
        data_dir = Path(repo.store.data_dir)
        # The shared domain loader creates its directory on read. Avoid that
        # side effect for an uninitialized, purely read-only research source.
        folder = data_dir / "user_data" / "lots"
        files = list(folder.glob("lot_*.json")) if folder.is_dir() else []
        raw_lots = lots.load_all(data_dir) if files else []
        source_complete = len(files) == len(raw_lots)
        if not source_complete:
            warnings.append("部分批次损坏或不可读，无法确认完整仓位，请先核对批次数据")
        # Legacy records without created_at acquire a new default at every
        # domain read. That synthetic timestamp is not a source revision.
        fact = {"lots": sorted(({k: v for k, v in item.items() if k != "created_at"}
                                for item in raw_lots), key=lambda item: str(item.get("id", "")))}
        fact["source_complete"] = source_complete
        active = []
        lot_symbols = {str(lot.get("symbol") or "").strip().upper() for lot in raw_lots}
        if position_service is not None:
            try:
                active = [plan for account in accounts
                          for plan in position_service.store.list_plans(account["id"])
                          if plan.get("state") not in {"completed", "cancelled"}
                          and plan.get("symbol") in lot_symbols]
            except Exception:  # noqa: BLE001 - unknown cross-source exposure must block adoption
                logger.warning("批次研究无法核对同股做 T 敞口", exc_info=True)
                warnings.append("无法核对其他账户同股做 T 敞口，暂停采纳批次操作计划")
                exposure_verified = False
        fact["active_plans"] = _active_facts(active)
        groups = defaultdict(list)
        for raw in raw_lots:
            groups[str(raw.get("symbol") or "").strip().upper()].append(raw)
        for symbol, records in sorted(groups.items()):
            valid = source_complete and all((q := _number(item.get("qty"), positive=True)) is not None
                        and q.is_integer() and _number(item.get("cost_price"), positive=True) is not None
                        for item in records)
            dates = [_day(item.get("buy_date")) for item in records]
            qty = sum(int(item["qty"]) for item in records) if valid else None
            cost = (float(sum(Decimal(str(item["qty"])) * Decimal(str(item["cost_price"]))
                              for item in records) / qty) if valid else None)
            positions.append({
                "key": _key(source, account_id, symbol), "symbol": symbol,
                "name": catalog.get(symbol, {}).get("name") or symbol,
                "qty": qty, "cost_price": cost, "cost_method": "manual_reference",
                "opened_on": min(dates).isoformat() if all(dates) else None,
                "latest_added_on": max(dates).isoformat() if all(dates) else None,
                "source_lot_ids": sorted(str(item.get("id", "")) for item in records),
                "input_valid": valid, "sellable_qty": None,
                "active_t_plan": any(plan["symbol"] == symbol for plan in active),
                "warnings": (["部分批次损坏或不可读，完整仓位待核对"] if not source_complete else
                             [] if valid else ["同股批次含无效数量或成本，整股数量与成本待核对"]),
            })
    if len(positions) > MAX_HOLDINGS:
        raise ValueError(f"单次研究最多 {MAX_HOLDINGS} 只持仓，请先缩小来源范围；未截断持仓")
    for position in positions:
        instrument = catalog.get(position["symbol"], {})
        identity = instrument.get("asset_type") or instrument.get("type")
        if instrument and identity is None:
            try:
                identity = repo.resolve_asset_type(position["symbol"])
            except Exception:  # noqa: BLE001 - unknown security types must never be inferred from a code
                logger.warning("持仓研究证券身份解析失败", exc_info=True)
        position["asset_type"] = identity or "unknown"
        if position["asset_type"] != "stock":
            position["warnings"].append("首版仅分析证券目录已确认的 A 股股票")
    return {"source": source, "account_id": account_id, "accounts": accounts,
            "input_version": _hash({"source": source, "account_id": account_id, "facts": fact,
                                    "exposure_verified": exposure_verified,
                                    "identity": [(p["symbol"], p["asset_type"]) for p in positions]}),
            "positions": positions, "warnings": warnings, "exposure_verified": exposure_verified}


def _profile(raw):
    value = deepcopy(raw or {})
    horizon = value.get("horizon") if value.get("horizon") in {"short", "swing"} else "short"
    defaults = {"opened_on": None, "horizon": horizon, "thesis": "", "invalidation": "",
                "max_loss_pct": None, "deep_loss_pct": 15, "new_position_days": 3,
                "max_holding_days": 5 if horizon == "short" else 20, "primary_sector_id": ""}
    defaults.update(value)
    return defaults


def _calendar(now, opened_dates):
    # Read bounds only to formulate a covered request; the shared loader still
    # validates the entire calendar and its official-source evidence.
    metadata = json.loads(BUNDLED_CALENDAR_PATH.read_text(encoding="utf-8-sig"))
    first, last = _day(metadata.get("coverage_start")), _day(metadata.get("coverage_end"))
    if not first or not last or not first <= now.date() <= last:
        raise ValueError("Calendar does not cover research date")
    start = max(first, now.date() - timedelta(days=260))
    covered_openings = [d for d in opened_dates if first <= d <= now.date()]
    if covered_openings:
        start = min(start, min(covered_openings))
    days = load_market_calendar(BUNDLED_CALENDAR_PATH, start=start,
                                end=min(last, now.date() + timedelta(days=400)))
    completed = [d for d in days if d < now.date() or (d == now.date() and now.time() >= CLOSE_CUTOFF)]
    return days, completed[-1] if completed else None


def _quote_time(record, now):
    stamp = _number(record.get("timestamp"))
    if stamp is None:
        return None
    try:
        market_at = datetime.fromtimestamp(stamp / 1000, CN_TZ)
    except (ValueError, OSError, OverflowError):
        return None
    return market_at if 0 <= (now - market_at).total_seconds() <= QUOTE_MAX_AGE_SECONDS else None


def _snapshot_fresh(value, now, max_age):
    try:
        stamp = datetime.fromisoformat(value) if isinstance(value, str) else value
        return (isinstance(stamp, datetime) and stamp.tzinfo is not None
                and 0 <= (now - stamp.astimezone(CN_TZ)).total_seconds() <= max_age)
    except (ValueError, TypeError):
        return False


def _daily_valid(row):
    day = _day(row.get("date"))
    close = _number(row.get("close"), positive=True)
    high, low = _number(row.get("high"), positive=True), _number(row.get("low"), positive=True)
    if not day or close is None or high is None or low is None or not low <= close <= high:
        return False
    stamp = row.get("quote_ts")
    if stamp is not None:
        value = _number(stamp, positive=True)
        if value is None:
            return False
        try:
            at = datetime.fromtimestamp(value / 1000, CN_TZ)
        except (ValueError, OSError, OverflowError):
            return False
        if at.date() != day or at.time() < CLOSE_CUTOFF:
            return False
    return True


def _history_map(repo, symbols, start, end, warnings):
    groups = defaultdict(list)
    columns = ["symbol", "date", "open", "high", "low", "close", "raw_close",
               "raw_high", "raw_low", "quote_ts", "change_pct"]
    for offset in range(0, len(symbols), MAX_HOLDINGS):
        try:
            frame = repo.get_daily_batch(symbols[offset:offset + MAX_HOLDINGS], start, end, columns=columns)
            for row in frame.to_dicts():
                day = _day(row.get("date"))
                if day and start <= day <= end:
                    groups[row["symbol"]].append(row)
        except Exception:  # noqa: BLE001 - one unavailable batch cannot fabricate a zero-valued history
            logger.warning("持仓研究批量日线读取失败", exc_info=True)
            warnings.append("部分日线读取失败或数据能力不可用，相应持仓暂停采纳")
    return {symbol: sorted(rows, key=lambda row: _day(row["date"])) for symbol, rows in groups.items()}


def _technical(rows, sessions, reference):
    values = dict.fromkeys(_TECH_KEYS)
    level_sources = {"support": "未知", "resistance": "未知"}
    if reference is None or not rows:
        return values, False, None, ["缺少已完成交易日日线"], level_sources
    by_date = defaultdict(list)
    for row in rows:
        by_date[_day(row["date"])].append(row)
    expected = [d for d in sessions if d <= reference][-60:]
    # Never remove invalid bars then pretend the resulting window is continuous.
    recent = expected[-20:]
    if len(recent) < 20 or any(len(by_date[d]) != 1 or not _daily_valid(by_date[d][0]) for d in recent):
        return values, False, by_date.get(reference, [None])[0], ["最近20个完整交易日日线缺失、重复或未核准"], level_sources
    last = by_date[reference][0]
    raw, close = _number(last.get("raw_close"), positive=True), _number(last.get("close"), positive=True)
    if raw is None or close is None:
        return values, False, last, ["缺少可验证的原始价与复权价映射，不能生成原始价触发位"], level_sources
    scale = raw / close
    for field in ("high", "low"):
        source, target = _number(last.get(field), positive=True), _number(last.get(f"raw_{field}"), positive=True)
        if source is None or target is None or not math.isclose(source * scale, target, abs_tol=.011):
            return values, False, last, ["原始价与复权价映射不一致，不能生成原始价触发位"], level_sources
    bars = [by_date[d][0] for d in recent]
    closes = [float(row["close"]) for row in bars]
    values.update(support=min(float(row["low"]) for row in bars) * scale,
                  resistance=max(float(row["high"]) for row in bars) * scale,
                  ma20=sum(closes) / 20 * scale,
                  return5=closes[-1] / closes[-6] - 1)
    level_sources.update(support="20日区间最低价", resistance="20日区间最高价")
    levels = compute_levels(pl.DataFrame(bars))
    for side, key in (("support", "support"), ("resistance", "resistance")):
        candidates = [level for kind in ("sr", "extreme") for level in levels.get(kind, [])
                      if level.get("side") == side and _number(level.get("value"), positive=True)]
        if not candidates:
            candidates = [level for level in levels.get("pivot", [])
                          if level.get("side") == side and level.get("rank") == 1
                          and _number(level.get("value"), positive=True)]
        if candidates:
            closest = min(candidates, key=lambda item: abs(item["value"] - close))
            values[key] = closest["value"] * scale
            level_sources[key] = str(closest["label"])
    ranges = [max(float(bars[i]["high"]) - float(bars[i]["low"]),
                  abs(float(bars[i]["high"]) - closes[i - 1]),
                  abs(float(bars[i]["low"]) - closes[i - 1])) for i in range(6, 20)]
    values["atr14"] = sum(ranges) / 14 * scale
    older = [d for d in sessions if d <= reference][-21:]
    if len(older) == 21 and all(len(by_date[d]) == 1 and _daily_valid(by_date[d][0]) for d in older):
        values["return20"] = closes[-1] / float(by_date[older[0]][0]["close"]) - 1
    notes = []
    if len(expected) == 60 and all(len(by_date[d]) == 1 and _daily_valid(by_date[d][0]) for d in expected):
        values["ma60"] = sum(float(by_date[d][0]["close"]) for d in expected) / 60 * scale
    else:
        notes.append("MA60缺少连续60个有效交易日，长期趋势证据不足")
    return {key: round(value, 8) if value is not None else None for key, value in values.items()}, True, last, notes, level_sources


def _observed_evidence(row, primary, stock_change, *, basis_valid):
    """Describe computed facts without exporting costs, quantities or user prose."""
    evidence = []
    age_labels = {"new": "新建仓", "holding": "持有中", "overdue": "超过研究持有期限", "unknown": "未知"}
    if row["age_days"] is None:
        evidence.append("持仓天数未知，无法判断建仓阶段；核对时间不作建仓日")
    else:
        evidence.append(f"已持有{row['age_days']}个交易日，阶段为{age_labels[row['age_state']]}（建仓当日记零）")
    pnl_labels = {"deep_loss": "深套", "loss": "浮亏", "flat": "接近成本", "profit": "浮盈"}
    if row["pnl_ratio"] is None:
        evidence.append("参考价格盈亏未知，数量或成本不完整时不计算部分仓位盈亏")
    else:
        evidence.append(f"原始价格相对参考成本的价差{row['pnl_ratio']:+.2%}，状态为{pnl_labels[row['pnl_state']]}")
    comparable = row["price"]["value"] if basis_valid else row["price"]["reference_close"]
    label = "现价（原始）" if basis_valid else "完整日线原始参考收盘"
    if comparable is None:
        evidence.append("原始价格与技术基准未知，无法比较现价和均线")
    else:
        evidence.append(f"{label}{comparable:.4f}元，技术参考日为{row['price']['reference_date'] or '未知'}")
    for key, name in (("ma20", "MA20"), ("ma60", "MA60")):
        value = row["technicals"][key]
        if value is None:
            evidence.append(f"{name}未知，连续日线或价格基准证据不足")
        elif comparable is None:
            evidence.append(f"{name} {value:.4f}元，原始参考价格未知，暂不比较")
        else:
            relation = "高于" if comparable > value else "低于" if comparable < value else "等于"
            evidence.append(f"{name} {value:.4f}元；{label}{relation}{name}，均使用技术参考日原始价格基准")
    for key, name in (("support", "支撑"), ("resistance", "压力")):
        value = row["technicals"][key]
        if value is None:
            evidence.append(f"研究{name}未知，不能构造原始价格触发位")
        else:
            evidence.append(f"研究{name}{value:.4f}元，来源为{row['level_sources'][key]}，已换算至技术参考日原始价格基准")
    if primary is None or stock_change is None:
        evidence.append("参考板块与个股的同口径涨跌对比未知，关联或同期行情证据不足")
    else:
        basis = "实时快照" if primary["basis"] == "quote" else "完整日线"
        difference = (stock_change - primary["change_ratio"]) * 100
        evidence.append(f"参考板块{primary['name']}（来源{primary['source_id']}，{basis}）同口径涨跌："
                        f"个股{stock_change:+.2%} / 板块{primary['change_ratio']:+.2%}，差{difference:+.2f}个百分点")
    return evidence


def _memberships(service, symbols, catalog, warnings):
    try:
        targets = service.membership_snapshot() if service else []
    except Exception:  # noqa: BLE001 - catalog failure is explicit and cannot imply empty membership
        logger.warning("持仓研究板块成员读取失败", exc_info=True)
        warnings.append("板块成员目录读取失败，关联板块证据不足")
        return []
    aliases = defaultdict(set)
    for symbol in catalog:
        aliases[symbol.split(".")[0]].add(symbol)
    groups = []
    for target in targets:
        members = set()
        for raw in target.get("members", []):
            member = str(raw or "").strip().upper()
            if member not in catalog and len(aliases.get(member, set())) == 1:
                member = next(iter(aliases[member]))
            members.add(member)
        if members.intersection(symbols):
            groups.append({**target, "members": sorted(members)})
    return groups


def _sector_rows(groups, quotes, daily, reference, now):
    result = []
    for target in groups:
        quote_changes, daily_changes = [], []
        for member in target["members"]:
            record = quotes.get(member, {})
            previous, current = _number(record.get("prev_close"), positive=True), _number(record.get("last_price"), positive=True)
            if _quote_time(record, now) and previous is not None and current is not None:
                quote_changes.append(current / previous - 1)
            rows = [r for r in daily.get(member, []) if _day(r["date"]) == reference]
            if len(rows) == 1 and _daily_valid(rows[0]) and (change := _number(rows[0].get("change_pct"))) is not None:
                daily_changes.append(change)
        total = len(target["members"])
        use_quotes = total > 0 and len(quote_changes) / total >= MIN_SECTOR_COVERAGE
        selected = quote_changes if use_quotes else daily_changes
        # Preserve the observed denominator even if neither timing basis is usable.
        if len(selected) < len(quote_changes):
            selected = quote_changes
        coverage = len(selected) / total if total else 0
        valid = bool(target.get("available", True) and total and coverage >= MIN_SECTOR_COVERAGE)
        result.append({"key": target["key"], "name": target.get("name", target["key"]),
                       "kind": target.get("kind", "concept"), "source_id": target.get("source_id", ""),
                       "field": target.get("field"), "level": target.get("level"),
                       "member_count": total, "valid_count": len(selected), "coverage_ratio": coverage,
                       "change_ratio": sum(selected) / len(selected) if valid else None, "valid": valid,
                       "basis": "quote" if selected is quote_changes else "daily", "members": target["members"]})
    return result


def _plan(row, ready, reference, sessions):
    tech, profile = row["technicals"], row["profile"]
    future = [d for d in sessions if reference and d > reference]
    window = profile["max_holding_days"]
    expires = future[window - 1] if window > 0 and len(future) >= window else None
    ready = bool(ready and expires)
    risk = row["pnl_state"] == "deep_loss" or row["trend"] == "down" or row["age_state"] == "overdue"
    max_loss = _number(profile.get("max_loss_pct"), positive=True)
    if max_loss is not None and row["pnl_ratio"] is not None and row["pnl_ratio"] <= -max_loss / 100:
        risk = True
    priority = "risk" if risk else "protect" if row["pnl_state"] == "profit" else "observe"
    if not ready:
        summary = "研究条件尚未齐备，先补全证据并重新生成计划"
    elif risk:
        summary = "优先复核持有逻辑与风险预算，观察反弹兑现和破位减仓条件"
    elif priority == "protect":
        summary = "以保留趋势收益和限制利润回撤为主，按确认条件分步处理"
    else:
        summary = "观察建仓逻辑与支撑确认，按预定期限复核持有必要性"
    invalidation = profile.get("invalidation") or "请先填写研究失效条件"
    invalidation += "；持仓或价格基准变化、除权除息、数据过期时重新研究"
    bear_level = tech["support"]
    if ready and max_loss is not None and max_loss < 100 and row["cost_price"] is not None:
        bear_level = max(bear_level, row["cost_price"] * (1 - max_loss / 100))
    scenarios = [
        {"key": "base", "title": "基准情景", "action": "保持观察并复核持有逻辑" if not risk else "反弹时评估分步降低风险敞口",
         "conditions": ["价格保持研究支撑区域", "关联板块没有持续转弱", "持仓与风险预算已核对"],
         "price_trigger": None, "invalidation": invalidation},
        {"key": "bull", "title": "走强情景", "action": "确认突破后继续跟踪；浮盈仓复核分步止盈" if not risk else "确认反弹力度后评估分步减仓",
         "conditions": ["价格突破研究压力并保持", "关联板块同步改善", "成交与仓位条件人工复核"],
         "price_trigger": {"op": ">=", "value": round(tech["resistance"], 4)} if ready else None,
         "invalidation": invalidation},
        {"key": "bear", "title": "走弱情景", "action": "触及研究风控价后评估减仓或退出，先核对可卖与在途计划",
         "conditions": ["价格触及研究支撑与用户风险预算中较严格的价位", "持有逻辑或风险预算需要复核"],
         "price_trigger": {"op": "<=", "value": round(bear_level, 4)} if ready else None,
         "invalidation": invalidation},
    ]
    return {"summary": summary, "priority": priority, "scenarios": scenarios,
            "expires_on": expires.isoformat() if expires else None, "can_adopt": ready}


def analyze_holdings(repo, quote_service, sector_service, position_service, source,
                     account_id="", profiles=None, now=None):
    """Batch analysis from local standardized caches; no provider calls or writes."""
    now = now or datetime.now(CN_TZ)
    if now.tzinfo is None:
        raise ValueError("研究时间必须包含时区")
    now = now.astimezone(CN_TZ)
    inputs = read_inputs(repo, position_service, source, account_id)
    warnings = list(inputs["warnings"])
    sessions, reference = [], None
    try:
        opened_dates = [_day((profiles or {}).get(p["key"], {}).get("opened_on") or p["opened_on"])
                        for p in inputs["positions"]]
        sessions, reference = _calendar(now, [d for d in opened_dates if d])
    except (ValueError, OSError):
        warnings.append("交易日历未覆盖研究时点，不能判断完整交易日与持仓天数")
    quotes = {}
    if quote_service is not None:
        try:
            status = quote_service.status()
            if status.get("enabled") and not status.get("paused") and status.get("realtime_allowed"):
                quotes = quote_service.get_market_records()
            else:
                warnings.append("实时行情未启用或已暂停，仅使用完整日线")
        except Exception:  # noqa: BLE001 - missing realtime data must not prevent daily-only research
            logger.warning("持仓研究实时行情读取失败", exc_info=True)
            warnings.append("实时行情暂不可用，仅使用完整日线")
    else:
        warnings.append("实时行情服务不可用，仅使用完整日线")
    symbols = {p["symbol"] for p in inputs["positions"] if p["asset_type"] == "stock"}
    try:
        catalog = _catalog(repo)
    except Exception:  # noqa: BLE001 - identity failure already appears in input warnings
        logger.warning("持仓研究板块证券目录暂不可用", exc_info=True)
        catalog = {}
    groups = _memberships(sector_service, symbols, catalog, warnings) if symbols else []
    daily = _history_map(repo, sorted(symbols), reference - timedelta(days=180), reference, warnings) if reference and symbols else {}
    sector_symbols = sorted({s for group in groups for s in group["members"]} - symbols)
    if reference and sector_symbols:
        daily.update(_history_map(repo, sector_symbols, reference, reference, warnings))
    sector_rows = _sector_rows(groups, quotes, daily, reference, now)
    rows = []
    for position in inputs["positions"]:
        try:
            row = _analyze_row(position, profiles, daily, quotes, sector_rows, sessions, reference, now)
        except Exception:  # noqa: BLE001 - isolate one malformed symbol without losing other holdings
            logger.warning("单只持仓研究失败，已隔离", exc_info=True)
            failed = {**position, "input_valid": False,
                      "warnings": [*position["warnings"], "该股研究输入异常，暂停判断"]}
            row = _analyze_row(failed, {}, {}, {}, [], sessions, reference, now)
        if not inputs["exposure_verified"]:
            row["warnings"].append("无法核对其他账户同股做 T 敞口，暂停采纳批次操作计划")
            row["plan"]["can_adopt"] = False
            for scenario in row["plan"]["scenarios"]:
                scenario["price_trigger"] = None
        rows.append(row)
    after = read_inputs(repo, position_service, source, account_id)
    if after["input_version"] != inputs["input_version"]:
        message = "研究期间持仓来源或在途计划发生变化，请重新生成"
        warnings.append(message)
        for row in rows:
            row["warnings"].append(message)
            row["plan"]["can_adopt"] = False
            row["constraints"]["can_size"] = False
            for scenario in row["plan"]["scenarios"]:
                scenario["price_trigger"] = None
    return {"source": source, "account_id": inputs["account_id"], "accounts": inputs["accounts"],
            "input_version": inputs["input_version"], "generated_at": now.isoformat(),
            "as_of_date": reference.isoformat() if reference else None,
            "rows": rows, "warnings": list(dict.fromkeys(warnings))}


def _analyze_row(position, profiles, daily, quotes, sector_rows, sessions, reference, now):
    profile = _profile((profiles or {}).get(position["key"]))
    notes = list(position["warnings"])
    opened = _day(profile.get("opened_on") or position["opened_on"])
    if opened is None or opened > now.date() or not sessions or opened < sessions[0] or opened not in sessions:
        opened, age = None, None
        notes.append("请核对首次建仓交易日；核对快照时间不能替代建仓日")
    else:
        age = max(0, sum(opened <= d <= now.date() for d in sessions) - 1)
    technicals, complete, last, technical_notes, level_sources = _technical(daily.get(position["symbol"], []), sessions, reference)
    notes.extend(technical_notes)
    quote = quotes.get(position["symbol"], {})
    stamp = _quote_time(quote, now)
    value = _number(quote.get("last_price"), positive=True) if stamp else None
    if value is not None:
        price = {"value": value, "date": stamp.date().isoformat(), "market_at": stamp.isoformat(), "source": "quote", "fresh": True}
    else:
        value = _number(last.get("raw_close"), positive=True) if last else None
        price = {"value": value, "date": _day(last["date"]).isoformat() if last else None,
                 "market_at": None, "source": "daily" if value is not None else "unknown",
                 "fresh": bool(last and _day(last["date"]) == reference and _daily_valid(last))}
        notes.append("无新鲜且带真实市场时间的报价，现价使用完整日线原始收盘价")
    price["reference_close"] = _number(last.get("raw_close"), positive=True) if last else None
    price["reference_date"] = _day(last["date"]).isoformat() if last else None
    basis_valid = complete
    if stamp and value is not None and last:
        previous = _number(quote.get("prev_close"), positive=True)
        raw_reference = _number(last.get("raw_close"), positive=True)
        # On the same completed day, the quote's last price rather than previous
        # close shares the reference basis. Cross-session actions need revalidation.
        anchor = value if stamp.date() == reference else previous
        if anchor is None or raw_reference is None or not math.isclose(anchor, raw_reference, abs_tol=.011):
            basis_valid = False
            notes.append("报价与日线原始价格基准未确认一致，除权除息或跨日基准变化后须重算")
    cost = position["cost_price"]
    pnl = value / cost - 1 if value is not None and cost is not None and cost > 0 else None
    deep = _number(profile.get("deep_loss_pct"), positive=True) or 15
    pnl_state = ("unknown" if pnl is None else "deep_loss" if pnl <= -deep / 100
                 else "loss" if pnl < -.01 else "profit" if pnl > .01 else "flat")
    age_state = ("unknown" if age is None else "overdue" if age > profile["max_holding_days"]
                 else "new" if age <= profile["new_position_days"] else "holding")
    ma20, ma60 = technicals["ma20"], technicals["ma60"]
    raw_close = _number(last.get("raw_close"), positive=True) if last else None
    trend = "unknown"
    if ma20 is not None and raw_close is not None:
        trend = ("up" if ma60 is not None and raw_close > ma20 > ma60 else "down"
                 if ma60 is not None and raw_close < ma20 < ma60 else "range")
    related = [deepcopy(sector) for sector in sector_rows if position["symbol"] in sector["members"]]
    for sector in related:
        sector.pop("members")
    primary_id = profile.get("primary_sector_id")
    candidates = [s for s in related if s["valid"] and (not primary_id or s["key"] == primary_id)]
    candidates.sort(key=lambda s: (s["kind"] != "industry", s["source_id"], s["key"]))
    primary = candidates[0] if candidates else None
    strength = "unknown"
    stock_change = None
    if primary:
        stock_change = (value / previous - 1 if primary["basis"] == "quote" and stamp and value is not None
                        and (previous := _number(quote.get("prev_close"), positive=True)) is not None
                        else _number(last.get("change_pct")) if last and primary["basis"] == "daily" else None)
        if stock_change is not None:
            difference = stock_change - primary["change_ratio"]
            strength = "stronger" if difference > .01 else "weaker" if difference < -.01 else "inline"
    else:
        notes.append("关联板块缺失、指定板块不可用或行情覆盖不足80%，板块强弱待确认")
    if not str(profile.get("invalidation") or "").strip():
        notes.append("请先填写研究失效条件")
    if position["active_t_plan"]:
        notes.append("该股存在未完成做 T 计划，先核对敞口再采纳其他操作计划")
    if position["asset_type"] != "stock":
        complete = False
    if len([d for d in sessions if reference and d > reference]) < profile["max_holding_days"]:
        notes.append("交易日历未覆盖计划持有期限，暂不能确定计划到期日")
        complete = False
    ready = bool(position["input_valid"] and complete and basis_valid and price["fresh"]
                 and opened and primary and str(profile.get("invalidation") or "").strip()
                 and not position["active_t_plan"])
    if position["sellable_qty"] is None:
        notes.append("可卖数量未知；研究计划不提供精确卖出股数")
    max_age = _number(position.get("snapshot_max_age_seconds"), positive=True) or 900
    can_size = (ready and _number(position["sellable_qty"]) is not None
                and 0 <= position["sellable_qty"] <= (position["qty"] or 0)
                and position["sellable_qty"] == int(position["sellable_qty"])
                and position.get("reconciled") and position.get("account_reconciled")
                and position.get("reserved_qty", 0) == 0
                and _snapshot_fresh(position.get("snapshot_at"), now, max_age)
                and _snapshot_fresh(position.get("account_snapshot_at"), now, max_age)
                and position.get("trade_date") == now.date().isoformat())
    row = {**position, "profile": profile, "opened_on": opened.isoformat() if opened else None,
           "pnl_ratio": pnl, "pnl_state": pnl_state, "age_days": age, "age_state": age_state,
           "trend": trend, "relative_strength": strength, "price": price,
           "technicals": technicals, "sectors": related, "level_sources": level_sources,
           "evidence": ["盈亏为原始价格相对登记参考成本的价差，不含完整会计收益",
                        "支撑压力复用既有关键价位计算（筹码/极值优先，枢轴补充；缺失时使用20日区间），均线及ATR按参考日原始价格基准换算",
                        "参考成本未校正历史公司行动；除权除息、送转或成本调整后需人工核对",
                        "阈值和持仓期限为研发初值，条件计划不代表盈利概率或投资建议"],
           "warnings": list(dict.fromkeys(notes)),
           "constraints": {"sellable_qty": position["sellable_qty"],
                           "active_t_plan": position["active_t_plan"], "can_size": bool(can_size)}}
    row["evidence"].extend(_observed_evidence(row, primary, stock_change, basis_valid=basis_valid))
    row["plan"] = _plan(row, ready, reference, sessions)
    for internal in ("input_valid", "reconciled", "trade_date", "snapshot_at", "sellable_qty", "active_t_plan",
                     "account_snapshot_at", "account_reconciled", "snapshot_max_age_seconds", "reserved_qty"):
        row.pop(internal, None)
    return row
