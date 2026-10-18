"""持仓做 T 的确定性规则。价格为不复权元、成交量为股，分钟时间为结束时间。

不读取 provider/账户文件，不发通知，不生成成交。输入必须由服务层标准化；
单股票最多一个交易日的分钟窗口，且只能使用计算时已收到的完整分钟。
"""
from __future__ import annotations

import hashlib
import math
from datetime import date, datetime, time, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal
from itertools import pairwise
from statistics import median

from app.market_time import CN_TZ

DEFAULT_CONFIG = {
    "enabled": False, "min_net_profit": 20, "min_net_return": .002,
    "max_daily_plans": 6, "max_symbol_daily_plans": 2, "daily_quantity_ratio": .4,
    "cooldown_minutes": 15, "single_risk_budget": 100, "daily_risk_budget": 500,
    "max_extra_notional": 10000, "quote_max_age_seconds": 10,
    "position_max_age_seconds": 900, "minute_max_age_seconds": 75,
    "minute_grace_seconds": 15, "new_plan_cutoff": "14:00", "max_leg_minutes": 45,
    "commission_rate": .0003, "min_commission": 5, "stamp_tax_rate": .0005,
    "transfer_rate": .00001, "other_fee_rate": 0, "commission_includes_transfer": False,
    "commission_includes_other": True, "slippage_per_share": .01,
    "reference_includes_slippage": False, "fee_effective_from": "2023-08-28",
    "fee_effective_to": None, "fees_confirmed": False, "minute_timestamp_semantics": "unknown",
    "participation_rate": .01, "min_history_days": 20, "min_sector_coverage": .8,
    "market_filter": .004, "sector_filter": .006, "risk_cash_buffer": 20,
}


def as_cn(value) -> datetime:
    """仓库 naive 时间明确按北京时间解释，不调用宿主机默认时区。"""
    result = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    return result.replace(tzinfo=CN_TZ) if result.tzinfo is None else result.astimezone(CN_TZ)


def _decimal(value) -> Decimal:
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("价格或费用不是有限数字")
    return result


def _money(value: Decimal) -> Decimal:
    return value.quantize(Decimal(".01"), rounding=ROUND_HALF_UP)


def _config(config) -> dict:
    return {**DEFAULT_CONFIG, **(config or {})}


def order_fee(side: str, price, quantity: int, config=None, *, trade_date=None) -> Decimal:
    """一个收费组的累计费用。部分成交用累计金额/数量重算后取差额。

    不含滑点；实际成交均价已经体现执行偏移。最低收费只收一次。
    """
    cfg = _config(config)
    if side not in {"buy", "sell"} or quantity < 0:
        raise ValueError("成交方向或数量无效")
    if trade_date is not None:
        day = str(trade_date)[:10]
        if day < cfg["fee_effective_from"] or (cfg.get("fee_effective_to") and day > cfg["fee_effective_to"]):
            raise ValueError("费用配置不在成交日期的生效区间内")
    if quantity == 0:
        return Decimal(0)
    amount = _decimal(price) * quantity
    if amount <= 0:
        raise ValueError("成交金额必须为正")
    rates = ["commission_rate", "min_commission", "stamp_tax_rate", "transfer_rate", "other_fee_rate"]
    if any(_decimal(cfg[key]) < 0 for key in rates):
        raise ValueError("费用参数不能为负数")
    result = _money(max(amount * _decimal(cfg["commission_rate"]), _decimal(cfg["min_commission"])))
    if side == "sell":
        result += _money(amount * _decimal(cfg["stamp_tax_rate"]))
    if not cfg["commission_includes_transfer"]:
        result += _money(amount * _decimal(cfg["transfer_rate"]))
    if not cfg["commission_includes_other"]:
        result += _money(amount * _decimal(cfg["other_fee_rate"]))
    return result


def estimate_costs(buy_price, sell_price, quantity: int, config=None, *, trade_date=None) -> dict:
    """条件价格下的净价差，不是期望收益或成交承诺。"""
    cfg = _config(config)
    buy_fee = order_fee("buy", buy_price, quantity, cfg, trade_date=trade_date)
    sell_fee = order_fee("sell", sell_price, quantity, cfg, trade_date=trade_date)
    slippage = Decimal(0) if cfg["reference_includes_slippage"] else _decimal(cfg["slippage_per_share"]) * quantity * 2
    if slippage < 0:
        raise ValueError("执行偏移成本不能为负数")
    gross = (_decimal(sell_price) - _decimal(buy_price)) * quantity
    return {key: float(_money(value)) for key, value in {
        "buy_fee": buy_fee, "sell_fee": sell_fee, "slippage": slippage,
        "total_fees": buy_fee + sell_fee, "gross": gross,
        "net": gross - buy_fee - sell_fee - slippage,
    }.items()}


def quantity_rule(position: dict) -> tuple[int, int, int]:
    """普通限价申报的最低量、递增量和上限。只支持已核验的日期范围。

    来源：上交所交易规则2023/2026第3.3及科创板专章；深交所2026第3.3；
    北交所交易规则第3.3。2026科创板第6.7限价仍为10万；100万是盘后。
    """
    if position.get("security_type", "stock") != "stock":
        raise ValueError("首版只支持普通 A 股股票")
    day = str(position.get("trade_date", ""))[:10]
    if not day or day < "2023-04-10":
        raise ValueError("该日期的申报规则尚未支持")
    board, exchange = position.get("board"), position.get("exchange")
    if (board, exchange) in {("main", "SH"), ("main", "SZ"), ("chinext", "SZ")}:
        return 100, 100, 1000000
    if (board, exchange) == ("star", "SH"):
        return 200, 1, 100000
    if (board, exchange) == ("bse", "BJ"):
        return 100, 1, 1000000
    raise ValueError("交易所或板块规则未支持")


def legal_quantity(quantity, position: dict, *, side="buy") -> int:
    """向下取合法数量；买入不得借用卖出零股例外。"""
    minimum, step, maximum = quantity_rule(position)
    q = max(0, min(math.floor(quantity), maximum))
    sellable = position.get("effective_sellable", position.get("sellable_qty"))
    if side == "sell" and sellable is not None and 0 < sellable <= maximum and q >= sellable:
        return int(sellable)
    return 0 if q < minimum else minimum + (q - minimum) // step * step


def validate_quantity(position: dict, side: str, quantity: int) -> list[str]:
    if side not in {"buy", "sell"} or isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
        return ["申报方向或股数无效"]
    try:
        return [] if legal_quantity(quantity, position, side=side) == quantity else ["股数不满足该板块申报规则"]
    except ValueError as exc:
        return [str(exc)]


def continuous_session(now: datetime) -> bool:
    value = as_cn(now).time()
    return time(9, 30) <= value < time(11, 30) or time(13) <= value < time(14, 57)


def _price_cage(side: str, quote: dict, position: dict) -> float | None:
    fallback = ("ask", "bid", "price", "previous_close") if side == "buy" else ("bid", "ask", "price", "previous_close")
    base = next((float(quote[k]) for k in fallback if _positive(quote.get(k))), None)
    if base is None:
        return None
    pct = .05 if position.get("board") == "bse" else .02
    # 2026规则仍单列科创板2%价格笼子，不擅自套用主板十跳宽限。
    if position.get("board") == "star":
        return base * (1 + pct if side == "buy" else 1 - pct)
    return max(base * (1 + pct), base + .1) if side == "buy" else min(base * (1 - pct), base - .1)


def validate_execution(side: str, quantity: int, price, position: dict, quote: dict,
                       now: datetime, *, is_trading_day: bool | None) -> list[str]:
    """仅验证可申报条件，不确认成交。日历未知时不回退到周几。"""
    reasons = validate_quantity(position, side, quantity)
    if is_trading_day is not True:
        reasons.append("交易日历未确认交易日")
    if not continuous_session(now):
        reasons.append("不在首版支持的连续竞价时段")
    if quote.get("trade_status") != "trading":
        reasons.append("证券交易状态不可确认或不可交易")
    if not _positive(price):
        return [*reasons, "申报价格无效"]
    p = _decimal(price)
    if p % Decimal(".01") != 0:
        reasons.append("申报价格不符合最小价格单位 0.01 元")
    upper, lower = quote.get("limit_up_price"), quote.get("limit_down_price")
    if quote.get("no_price_limit") is not True and (not _positive(upper) or not _positive(lower)):
        reasons.append("当日实际涨跌停价格缺失")
    if (_positive(upper) and p > _decimal(upper)) or (_positive(lower) and p < _decimal(lower)):
        reasons.append("申报价格超过当日涨跌幅限制")
    cage = _price_cage(side, quote, position)
    if cage is None:
        reasons.append("有效申报价格范围缺少基准报价")
    elif (side == "buy" and float(p) > cage + 1e-9) or (side == "sell" and float(p) < cage - 1e-9):
        reasons.append("申报价格超出连续竞价有效范围")
    return reasons


def _positive(value) -> bool:
    return isinstance(value, (int, float, Decimal)) and not isinstance(value, bool) and math.isfinite(float(value)) and value > 0


def _fresh(value, now: datetime, seconds: float) -> bool:
    try:
        return -1 <= (now - as_cn(value)).total_seconds() <= seconds
    except (ValueError, TypeError):
        return False


def _bar_end(bar: dict) -> datetime:
    return as_cn(bar.get("end", bar.get("datetime")))


def _minute_ends(day: date, until: datetime) -> list[datetime]:
    values = []
    for hour, minute, count in [(9, 31, 120), (13, 1, 117)]:
        start = datetime.combine(day, time(hour, minute), CN_TZ)
        values.extend(start + timedelta(minutes=i) for i in range(count) if start + timedelta(minutes=i) <= until)
    return values


def _quality(bars, quote, env, position, account, now, cfg) -> list[str]:
    reasons = list(quote.get("data_errors", []))
    if not quote.get("source"):
        reasons.append("行情来源缺失")
    if not _fresh(quote.get("market_ts"), now, cfg["quote_max_age_seconds"]):
        reasons.append("报价过期或市场时间缺失")
    if not _fresh(quote.get("received_ts"), now, cfg["quote_max_age_seconds"]):
        reasons.append("报价接收时间过期或缺失")
    if not _positive(quote.get("bid")) or not _positive(quote.get("ask")):
        reasons.append("买卖报价缺失，仅可进行分钟观察")
    elif quote["ask"] < quote["bid"]:
        reasons.append("买卖报价倒挂")
    if not _positive(quote.get("price")):
        reasons.append("实时价格缺失")
    if quote.get("trade_status") != "trading":
        reasons.append("证券停牌或交易状态未知，暂停判断")
    if quote.get("is_trading_day") is not True:
        reasons.append("交易日历未确认交易日")
    if not _positive(quote.get("history_days")) or quote["history_days"] < cfg["min_history_days"] or not _positive(quote.get("median_minute_volume_shares")):
        reasons.append("历史波动与容量数据不足")
    for obj, name in [(position, "持仓"), (account, "账户")]:
        if not obj.get("reconciled") or obj.get("needs_reconciliation"):
            reasons.append(f"{name}数据待核对")
        if str(obj.get("trade_date"))[:10] != now.date().isoformat() or not _fresh(obj.get("snapshot_at"), now, cfg["position_max_age_seconds"]):
            reasons.append(f"{name}快照过期")
    if position.get("effective_sellable", position.get("sellable_qty")) is None:
        reasons.append("当日可卖数量未知")
    if account.get("effective_cash", account.get("available_cash")) is None:
        reasons.append("可用资金未知")
    for key, stamp, name in [("market_return_5m", "market_ts", "大盘"), ("sector_return_5m", "sector_ts", "板块")]:
        value = env.get(key)
        if not isinstance(value, (float, int)) or not math.isfinite(value):
            reasons.append(f"{name}五分钟行情缺失")
        if not _fresh(env.get(stamp), now, cfg["quote_max_age_seconds"]):
            reasons.append(f"{name}行情时间过期或缺失")
        elif (quote.get("market_ts") and _fresh(quote["market_ts"], now, cfg["quote_max_age_seconds"])
              and abs((as_cn(env[stamp]) - as_cn(quote["market_ts"])).total_seconds()) > cfg["quote_max_age_seconds"]):
            reasons.append(f"{name}与股票报价时间不匹配")
    if not _positive(env.get("coverage")) or not cfg["min_sector_coverage"] <= env["coverage"] <= 1:
        reasons.append("板块行情覆盖率不足")
    if not bars:
        return [*reasons, "完整分钟数据不足"]
    ends = [_bar_end(bar) for bar in bars]
    if len(set(ends)) != len(ends) or ends != _minute_ends(now.date(), ends[-1]):
        reasons.append("当日分钟数据缺口、重复或起始区间不完整")
    last_allowed = now.replace(second=0, microsecond=0)
    if now.second < cfg["minute_grace_seconds"]:
        last_allowed -= timedelta(minutes=1)
    expected = _minute_ends(now.date(), last_allowed)
    if (now - ends[-1]).total_seconds() > cfg["minute_max_age_seconds"] or (expected and ends[-1] < expected[-1]):
        reasons.append("完整分钟数据过期")
    for bar in bars:
        if any(not _positive(bar.get(k)) for k in ("open", "high", "low", "close")):
            reasons.append("分钟价格字段缺失或无效")
            break
        if not bar["low"] <= min(bar["open"], bar["close"]) <= max(bar["open"], bar["close"]) <= bar["high"]:
            reasons.append("分钟 OHLC 价格关系无效")
            break
        if any(not isinstance(bar.get(k), (int, float)) or not math.isfinite(bar[k]) or bar[k] < 0 for k in ("volume_shares", "amount")):
            reasons.append("分钟成交量额缺失或单位未标准化")
            break
        if (bar["volume_shares"] == 0) != (bar["amount"] == 0):
            reasons.append("分钟成交量额不一致")
            break
        if bar["volume_shares"] and not bar["low"] - .01 <= bar["amount"] / bar["volume_shares"] <= bar["high"] + .01:
            reasons.append("分钟成交量额单位或价格口径不一致")
            break
    return list(dict.fromkeys(reasons))


def _indicators(bars) -> dict:
    # 日累计VWAP跨午休；确认序列、ATR和趋势窗口只使用本段连续竞价。
    session = [bar for bar in bars if (_bar_end(bar).hour >= 13) == (_bar_end(bars[-1]).hour >= 13)]
    if len(session) < 23:
        raise ValueError("本段连续竞价完整分钟不足 23 根")
    volume, amount, vwaps = 0., 0., []
    for bar in bars:
        volume += bar["volume_shares"]
        amount += bar["amount"]
        vwaps.append(amount / volume if volume else None)
    trs = [max(bar["high"]-bar["low"], abs(bar["high"]-prev["close"]), abs(bar["low"]-prev["close"]))
           for prev, bar in pairwise(session)]
    atr = sum(trs[-20:]) / 20
    base_volume = median(bar["volume_shares"] for bar in session[-23:-3])
    if atr <= 0 or base_volume <= 0 or any(value is None for value in vwaps[-6:]):
        raise ValueError("波动、成交量或均价不可计算")
    band = max(2 * atr, .03)
    recent_bands = [max(2 * sum(trs[:len(trs)-offset][-20:]) / 20, .03) for offset in (2, 1, 0)]
    return {"vwap": vwaps[-1], "atr": atr, "band": band,
            "displacement": (session[-1]["close"]-session[-11]["close"]) / atr,
            "vwap_change_5m": vwaps[-1]-vwaps[-6],
            "support": min(bar["low"] for bar in session[-23:-3]),
            "resistance": max(bar["high"] for bar in session[-23:-3]),
            "volume_ratio": session[-1]["volume_shares"] / base_volume,
            "median_volume_shares": base_volume, "recent_vwaps": vwaps[-3:], "recent_bands": recent_bands}


def _round_price(value, *, up=False):
    return float(_decimal(value).quantize(Decimal(".01"), rounding=ROUND_CEILING if up else ROUND_FLOOR))


def _effective_cash(account):
    if account.get("effective_cash") is not None:
        return max(0., float(account["effective_cash"]))
    cash = float(account.get("available_cash") or 0)
    if not account.get("cash_net_of_frozen", True):
        cash -= float(account.get("broker_frozen_cash") or 0)
    return max(0., cash - float(account.get("reserved_cash") or 0))


def _effective_sellable(position):
    if position.get("effective_sellable") is not None:
        return max(0, int(position["effective_sellable"]))
    quantity = int(position.get("sellable_qty") or 0)
    if not position.get("sellable_net_of_frozen", True):
        quantity -= int(position.get("broker_frozen_qty") or 0)
    return max(0, quantity - int(position.get("reserved_qty") or 0))


def evaluate_candidate(bars: list[dict], quote: dict, environment: dict, position: dict,
                       account: dict, now: datetime, direction: str, config=None) -> dict:
    """纯候选评估。手工账户只按已登记数据通过，不标作券商验证。

    first_price/second_price 是条件区间；quantity只测算。服务接受计划时须再次
    校验并在账户事务中预留，不可凭本函数结果直接扣库存或记成交。
    """
    if direction not in {"buy_sell", "sell_buy"}:
        raise ValueError("未知做 T 方向")
    now, cfg = as_cn(now), _config(config or account.get("config"))
    version = "vwap_recovery_v0.1" if direction == "buy_sell" else "vwap_rejection_v0.1"
    result = {"account_id": account.get("id"), "symbol": position.get("symbol"), "direction": direction,
              "state": "none", "reasons": [], "evidence": {}, "first_price": None,
              "second_price": None, "stop_price": None, "cover_guard": None, "quantity": 0,
              "net_min": None, "net_max": None, "fees": {}, "created_at": now.isoformat(),
              "expires_at": (now+timedelta(seconds=60)).isoformat(), "strategy_version": version,
              "account_version": account.get("version"), "position_version": position.get("version"),
              "market": {**{key: value.isoformat() if isinstance(value, (date, datetime)) else value for key, value in quote.items()},
                         "calculated_at": now.isoformat()}, "source_mode": account.get("mode", "manual"),
              "probability": None, "required_cash": 0, "risk_estimate": 0}
    identity = f"{account.get('id')}|{position.get('symbol')}|{direction}|{now.isoformat()}|{version}"
    result["id"] = "pt_" + hashlib.sha256(identity.encode()).hexdigest()[:24]
    try:
        completed = sorted([bar for bar in bars if bar.get("complete", True) and _bar_end(bar) <= now
                            and _bar_end(bar).date() == now.date()
                            and (not bar.get("received_ts") or as_cn(bar["received_ts"]) <= now)], key=_bar_end)
        reasons = _quality(completed, quote, environment, position, account, now, cfg)
    except (ValueError, TypeError, KeyError, OverflowError):
        reasons, completed = ["分钟或快照字段格式无效"], []
    if reasons:
        result.update(state="paused", reasons=reasons)
        return result
    try:
        ind = _indicators(completed)
        quantity_rule(position)
    except ValueError as exc:
        result.update(state="paused", reasons=[str(exc)])
        return result
    result["evidence"] = {**ind, "market_return_5m": environment["market_return_5m"],
                          "sector_return_5m": environment["sector_return_5m"],
                          "window_metric": environment.get("window_metric", "period_return"),
                          "last_complete_minute": _bar_end(completed[-1]).isoformat()}
    recent, band, vwap = completed[-3:], ind["band"], ind["vwap"]
    closes = [bar["close"] for bar in recent]
    if direction == "buy_sell":
        excursion = any(bar["close"] <= value - 1.2 * width for bar, value, width in zip(recent, ind["recent_vwaps"], ind["recent_bands"], strict=True))
        checks = {"回落进入波动下轨": excursion,
                  "已形成支撑未明显跌破": min(bar["low"] for bar in recent) >= ind["support"] - .2*band,
                  "止跌确认": closes[2] > closes[1] > closes[0] and recent[2]["low"] >= recent[1]["low"] and closes[2] > recent[1]["high"],
                  "成交量确认": ind["volume_ratio"] >= 1.1,
                  "仍有均价回归空间": closes[2] <= vwap - .6*band,
                  "无强下跌": not (ind["displacement"] <= -3 and ind["vwap_change_5m"] < 0)}
        result.update(first_price=[_round_price(quote["ask"], up=True), _round_price(quote["ask"]+.1*band, up=True)],
                      stop_price=_round_price(ind["support"]-.3*band))
        market_ok = environment["market_return_5m"] > -cfg["market_filter"] and environment["sector_return_5m"] > -cfg["sector_filter"]
    else:
        excursion = any(bar["close"] >= value + 1.2 * width for bar, value, width in zip(recent, ind["recent_vwaps"], ind["recent_bands"], strict=True))
        checks = {"上冲进入波动上轨": excursion,
                  "已形成压力未明显突破": max(bar["high"] for bar in recent) <= ind["resistance"] + .2*band,
                  "回落确认": closes[2] < closes[1] < closes[0] and recent[2]["high"] <= recent[1]["high"] and closes[2] < recent[1]["low"],
                  "成交量确认": ind["volume_ratio"] >= 1.1,
                  "仍有均价回归空间": closes[2] >= vwap + .6*band,
                  "无强上涨": not (ind["displacement"] >= 3 and ind["vwap_change_5m"] > 0)}
        result.update(first_price=[_round_price(quote["bid"]-.1*band), _round_price(quote["bid"])],
                      stop_price=_round_price(ind["resistance"]+.3*band, up=True),
                      cover_guard=_round_price(ind["resistance"]+.6*band, up=True))
        market_ok = environment["market_return_5m"] < cfg["market_filter"] and environment["sector_return_5m"] < cfg["sector_filter"]
    result["second_price"] = [_round_price(vwap-.05*band), _round_price(vwap+.05*band, up=True)]
    result["evidence"]["conditions"] = checks
    if not all(checks.values()):
        result.update(state="watching" if excursion else "none", reasons=[f"等待：{key}" for key, passed in checks.items() if not passed])
        return result
    blockers = []
    if not cfg["enabled"]:
        blockers.append("账户做 T 监控未启用")
    if not position.get("enabled", True):
        blockers.append("该持仓做 T 监控未启用")
    if not cfg["fees_confirmed"]:
        blockers.append("费用合同口径尚未确认")
    if not market_ok:
        blockers.append("大盘或板块方向过滤未通过")
    if (_positive(quote.get("limit_up_price")) and quote["price"] >= quote["limit_up_price"]) or (
            _positive(quote.get("limit_down_price")) and quote["price"] <= quote["limit_down_price"]):
        blockers.append("触及涨跌停价，首版暂停新建计划")
    value = now.strftime("%H:%M")
    if not ("10:00" <= value < "11:15" or "13:05" <= value < cfg["new_plan_cutoff"]):
        blockers.append("不在允许新建计划的时间窗口")
    if account.get("daily_plans", 0) >= cfg["max_daily_plans"] or position.get("daily_plans", 0) >= cfg["max_symbol_daily_plans"]:
        blockers.append("当日计划次数达到上限")
    if position.get("cooldown_until") and now < as_cn(position["cooldown_until"]):
        blockers.append("计划冷却时间尚未结束")
    minimum, step, _ = quantity_rule(position)
    sellable, cash = _effective_sellable(position), _effective_cash(account)
    original_sellable = position.get("day_initial_sellable", position.get("sellable_qty", 0))
    if original_sellable is None:
        result.update(state="blocked", reasons=["当日库存预算基准未知，须先核对可卖数量"])
        return result
    capacity = min(ind["median_volume_shares"], quote["median_minute_volume_shares"]) * cfg["participation_rate"]
    upper = min(sellable, position.get("max_t_qty", 0),
                max(0, original_sellable * cfg["daily_quantity_ratio"] - position.get("daily_t_qty", 0)), capacity)
    if sellable < minimum:
        blockers.append("可卖库存不足")
    if direction == "buy_sell":
        upper = min(upper, max(0, position.get("max_extra_qty", 0) - max(0, position["qty"]-position["target_qty"])),
                    cfg["max_extra_notional"] / result["first_price"][1])
        buy, sell = result["first_price"][1], result["second_price"][0]
    else:
        core_space = position["qty"]-position["core_qty"]-position.get("external_sell_qty", 0)
        upper = min(upper, max(0, core_space))
        if core_space < minimum:
            blockers.append("核心持仓下限限制可卖数量")
        buy, sell = result["second_price"][1], result["first_price"][0]
    upper = legal_quantity(upper, position)
    risk_left = min(cfg["single_risk_budget"], max(0, cfg["daily_risk_budget"]-account.get("daily_risk_used", 0)))

    def requirements(q):
        costs = estimate_costs(buy, sell, q, cfg, trade_date=now.date())
        if direction == "buy_sell":
            required = buy*q + costs["buy_fee"] + cfg["risk_cash_buffer"]
            risk = max(0, buy-result["stop_price"])*q + costs["total_fees"] + costs["slippage"]
        else:
            cover = result["cover_guard"]*q + float(order_fee("buy", result["cover_guard"], q, cfg, trade_date=now.date()))
            required = max(0, cover + cfg["risk_cash_buffer"] - (sell*q-costs["sell_fee"]))
            risk = max(0, result["cover_guard"]-sell)*q + costs["total_fees"] + costs["slippage"]
        return costs, required, risk

    try:
        # 单调约束用二分，避免按每股线性扫描大持仓。
        lo, hi, best = 0, max(-1, (upper-minimum)//step), None
        while lo <= hi:
            mid = (lo+hi)//2
            q = minimum+mid*step
            cost, required, risk = requirements(q)
            if required <= cash and risk <= risk_left:
                best = q, cost, required, risk
                lo = mid+1
            else:
                hi = mid-1
        if best:
            q, costs, required, risk = best
            result.update(quantity=q, fees=costs, net_min=costs["net"], required_cash=round(required, 2), risk_estimate=round(risk, 2))
            if direction == "buy_sell":
                optimistic = estimate_costs(result["first_price"][0], result["second_price"][1], q, cfg, trade_date=now.date())
            else:
                optimistic = estimate_costs(result["second_price"][0], result["first_price"][1], q, cfg, trade_date=now.date())
            result["net_max"] = optimistic["net"]
            if costs["net"] < max(cfg["min_net_profit"], buy*q*cfg["min_net_return"]):
                blockers.append("费用后净价差不足")
            first_side = "buy" if direction == "buy_sell" else "sell"
            first_price = buy if direction == "buy_sell" else sell
            blockers.extend(validate_execution(first_side, q, first_price, position, quote, now, is_trading_day=quote["is_trading_day"]))
            # 远期第二腿不拿当前价格笼子误判，但需满足日期限价与两腿数量规则。
            blockers.extend(validate_quantity(position, "sell" if direction == "buy_sell" else "buy", q))
            target = sell if direction == "buy_sell" else buy
            if quote.get("no_price_limit") is not True and not quote["limit_down_price"] <= target <= quote["limit_up_price"]:
                blockers.append("第二笔目标价超过当日涨跌幅限制")
        else:
            if upper < minimum:
                blockers.append("库存、单次额度、临时敞口或流动性不足合法数量")
            else:
                _, required, risk = requirements(minimum)
                if required > cash:
                    blockers.append("资金不足")
                if risk > risk_left:
                    blockers.append("单次或当日累计风险预算不足")
    except (ValueError, KeyError, TypeError) as exc:
        blockers.append(f"费用或交易规则无法核验：{exc}")
    result.update(state="blocked" if blockers else "triggered", reasons=list(dict.fromkeys(blockers)))
    if not blockers:
        result["reasons"] = ["条件满足；建立计划时将再次校验并预留资源", "人工数据待券商核对" if account.get("mode") == "manual" else "模拟账户，不代表实盘可成交"]
    return result
