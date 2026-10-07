"""Optional overseas observations, kept separate from domestic psychology."""
from __future__ import annotations

import math
from collections import Counter
from copy import deepcopy
from datetime import date, datetime, timedelta
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from app.data_providers import custom
from app.market_time import CN_TZ
from app.services import preferences
from app.services.market_game_overseas_models import OverseasContext, OverseasQuote

_INSTRUMENTS = {
    "^IXIC": ("纳斯达克综合指数", "US", "USD", "America/New_York"),
    "^KS11": ("韩国 KOSPI", "KR", "KRW", "Asia/Seoul"),
    "005930.KS": ("三星电子", "KR", "KRW", "Asia/Seoul"),
    "000660.KS": ("SK 海力士", "KR", "KRW", "Asia/Seoul"),
}
_LIMITATIONS = [
    "外围涨跌仅作风险环境参考; 行情可能延迟, 延迟时长未经确认, 不代表实时成交价格。",
    "没有完整海外交易日历, 96小时仅为保守时效上限; 无法保证休市前报价当前仍是最新。",
    "保留美国及韩国各自交易日和时区; 美国按纽约夏令时转换, 不把同一北京时间日期当作同一交易日。",
    "阶段未知时仅表示最近常规盘报价参考, 不认定市场已收盘或当前正在交易。",
    "三个因子有关联; 三星和海力士合为一组, 不与 KOSPI 或纳指重复累计分数及仓位倍率。",
    "指数正负1%、存储组正负2%为未校准研究阈值, 不是概率、收益预测或最优交易参数。",
    "科技、半导体及存储方向属于业务关联的观察映射, 不是因果识别或个股推荐。",
    "外围不改写冻结心理评分, 不单独确认入场, 不提供新增仓位或解除既有风控。",
]


def collect_overseas_quotes() -> dict:
    """Acquire only the selected declared capability; never try another source."""
    source = "yahoo_overseas"
    try:
        source = preferences.get_overseas_data_provider()
        if not custom.provider_has_dataset(source, "overseas"):
            return {"source": source, "fetched_at": None, "quotes": [], "reason": "所选数据源未提供外围行情能力"}
        provider = custom.get_provider(source)
        acquire = getattr(provider, "get_overseas_quotes", None)
        if not callable(acquire):
            return {"source": source, "fetched_at": None, "quotes": [], "reason": "所选数据源缺少外围行情接口"}
        result = acquire()
        if not isinstance(result, dict) or not isinstance(result.get("quotes"), list):
            return {"source": source, "fetched_at": None, "quotes": [], "reason": "外围数据源返回格式无法核验"}
        return {"source": source, "fetched_at": result.get("fetched_at"), "quotes": deepcopy(result["quotes"])}
    except Exception:
        return {"source": source, "fetched_at": None, "quotes": [], "reason": "外围数据源暂不可用, 未替换其他来源"}


def _instant(value) -> datetime:
    if not isinstance(value, str):
        raise ValueError("时间缺失")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("时间缺少时区")
    return parsed


def _number(value) -> bool:
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _empty(symbol: str, reason: str, state: str = "unavailable") -> dict:
    name, market, currency, _ = _INSTRUMENTS[symbol]
    return OverseasQuote(symbol=symbol, name=name, market=market, currency=currency,
                         state=state, reason=reason).model_dump(mode="json")


def _safe_url(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme in {"https", "http"} and parsed.hostname and not parsed.username and not parsed.password:
            return value
    except ValueError:
        pass
    return None


def _quote(symbol: str, raw: dict, fetched: datetime, cutoff: datetime) -> dict:
    name, market, currency, timezone = _INSTRUMENTS[symbol]
    try:
        row = OverseasQuote.model_validate(raw).model_dump(mode="json")
        if row["symbol"] != symbol or row["market"] != market or row["currency"] != currency:
            raise ValueError("标的市场不一致")
        row.update(name=name, source_url=_safe_url(row["source_url"]))
        # Invalid and missing states may carry partial vendor fields. Do not
        # transform partial facts into a computed return or a numerical zero.
        if row["state"] in {"unavailable", "unverified"}:
            return _empty(symbol, row["reason"], row["state"])
        if any(not _number(raw.get(key)) for key in ("price", "previous_close", "change_pct")):
            raise ValueError("数值缺失或无效")
        actual = row["price"] / row["previous_close"] - 1
        if not math.isclose(row["change_pct"], actual, rel_tol=1e-6, abs_tol=1e-8):
            raise ValueError("涨跌幅口径不一致")
        observed, available = _instant(row["observed_at"]), _instant(row["available_at"])
        if not observed <= available <= fetched <= cutoff:
            raise ValueError("数据时点不能用于本次观察")
        session = date.fromisoformat(row["session_date"])
        if row["session_date"] != session.isoformat() or session != observed.astimezone(ZoneInfo(timezone)).date():
            raise ValueError("交易日不一致")
        age = cutoff - observed
        if age > timedelta(hours=96):
            row.update(state="stale", reason="报价距本次截止超过96小时, 不参与外围因子")
        elif row["phase"] == "intraday" and age > timedelta(minutes=30):
            row.update(state="stale", reason="盘中报价已滞后超过30分钟, 不参与外围因子")
        # A source may deliberately mark a quote stale: never promote it.
        return row
    except (ValueError, TypeError, KeyError, ZeroDivisionError):
        return _empty(symbol, "行情数值、单位或信息时点无法核验, 不参与外围因子", "unverified")


def _direction(change: float, threshold: float) -> str:
    if change >= threshold or math.isclose(change, threshold, abs_tol=1e-12):
        return "support"
    if change <= -threshold or math.isclose(change, -threshold, abs_tol=1e-12):
        return "pressure"
    return "neutral"


def _factor(key: str, label: str, symbols: list[str], rows: dict[str, dict], threshold: float) -> dict:
    selected = [rows[symbol] for symbol in symbols]
    factor = {"id": key, "label": label, "state": "unknown", "change_pct": None, "symbols": symbols,
              "explanation": "所需行情不完整或未通过时点校验, 不参与判断"}
    if any(row["state"] != "ready" for row in selected):
        return factor
    if len({row["session_date"] for row in selected}) != 1:
        factor["explanation"] = "三星与海力士交易日不一致, 不合成为同一组信号"
        return factor
    changes = [row["change_pct"] for row in selected]
    change = sum(changes) / len(changes)
    state = "mixed" if min(changes) < 0 < max(changes) else _direction(change, threshold)
    factor.update(state=state, change_pct=change)
    if key == "memory_chain":
        factor["explanation"] = (
            "三星与海力士涨跌方向相反, 组内存在分歧; 展示等权均值, 不作一致方向确认" if state == "mixed" else
            "三星与海力士同交易日涨跌等权平均, 仅作为一组存储链参考, 不与韩国指数重复加权"
        )
    else:
        factor["explanation"] = ("最近常规盘涨跌参考; 达到正负1%时分别标记支撑或压力, 未达到阈值不指示明确方向")
    return factor


def _risk(factors: list[dict]) -> str:
    states = {factor["state"] for factor in factors}
    if "pressure" in states:
        return "caution"
    if "mixed" in states:
        return "mixed"
    if "support" in states:
        return "support"
    return "neutral" if "neutral" in states else "unknown"


def _psychology_link(psychology: dict | None, risk: str) -> str:
    dimensions = psychology.get("dimensions", []) if isinstance(psychology, dict) else []
    values = {row.get("id"): row.get("score") for row in dimensions if isinstance(row, dict)} if isinstance(dimensions, list) else {}
    appetite, panic = values.get("risk_appetite"), values.get("panic_pressure")
    appetite = appetite if _number(appetite) and 0 <= appetite <= 100 else None
    panic = panic if _number(panic) and 0 <= panic <= 100 else None
    if appetite is None or panic is None:
        return "冻结的风险偏好或恐慌评分缺失, 心理联动未知; 外围信息不用于补算或改写情绪分数。"
    if risk == "unknown":
        return "外围证据不足, 仅保留原报告心理背景; 无法判断外部环境是否形成共振。"
    if risk == "caution" and appetite >= 75:
        return "外围承压与冻结的高风险偏好并存, 需警惕追涨后的兑现压力; 国内量价和原入场条件仍须独立核验。"
    if risk == "caution" and panic >= 75:
        return "外围承压叠加冻结的高恐慌压力, 需观察是否继续撤退; 不能据此断言冰点已经见底。"
    if risk == "support" and panic >= 75:
        return "外围支撑可作为冻结恐慌背景下的修复线索, 仍需国内板块与个股承接确认, 不能直接抄底。"
    if risk == "support" and appetite >= 75:
        return "外围支撑与冻结的高风险偏好同向, 仍需防范拥挤追涨; 不提高原计划上限。"
    if risk == "mixed":
        return "外围存在分歧, 保留冻结心理背景并等待国内价格和承接确认, 不合成为确定方向。"
    return "外围仅补充原报告心理背景, 尚不构成明确共振; 不改变情绪评分, 原入场与风险条件仍须核验。"


def build_overseas_context(raw: dict | None, *, cutoff: datetime,
                           psychology: dict | None = None, historical: bool = False) -> dict:
    """Build deterministic context from acquired quotes; never fetch or write."""
    if not isinstance(cutoff, datetime) or cutoff.tzinfo is None or cutoff.utcoffset() is None:
        raise ValueError("外围分析截止时间必须包含时区")
    cutoff = cutoff.astimezone(CN_TZ)
    raw = raw if isinstance(raw, dict) else {}
    source = raw.get("source") if isinstance(raw.get("source"), str) and raw["source"] else "unavailable"
    limitations = list(_LIMITATIONS)
    fetched, quotes = None, []
    if historical:
        reason = "历史首次可得时间未核验, 不用本次取得的外围行情回填历史判断"
        quotes = [_empty(symbol, reason, "unverified") for symbol in _INSTRUMENTS]
        limitations.append(reason)
    else:
        try:
            fetched = _instant(raw.get("fetched_at"))
            if fetched > cutoff:
                raise ValueError("来源获取时间晚于本次截止")
            items = raw.get("quotes")
            if not isinstance(items, list) or len(items) > 64:
                raise ValueError("来源结构无法核验")
            counts = Counter(row.get("symbol") for row in items
                             if isinstance(row, dict) and isinstance(row.get("symbol"), str))
            matched = {row["symbol"]: row for row in items if isinstance(row, dict)
                       and isinstance(row.get("symbol"), str) and row["symbol"] in _INSTRUMENTS}
            for symbol in _INSTRUMENTS:
                if counts[symbol] > 1:
                    quotes.append(_empty(symbol, "同一标的出现重复记录, 全部排除", "unverified"))
                elif symbol not in matched:
                    quotes.append(_empty(symbol, "本次未取得该标的可用行情"))
                else:
                    quotes.append(_quote(symbol, matched[symbol], fetched, cutoff))
        except (ValueError, TypeError):
            fetched = None
            quotes = [_empty(symbol, "来源获取时间或返回格式无法核验, 本次不参与外围因子") for symbol in _INSTRUMENTS]
            limitations.append("外围数据获取失败或获取时点无效; 未使用其他市场或其他日期的数据替代。")
    rows = {row["symbol"]: row for row in quotes}
    factors = [
        _factor("us_tech", "美国科技环境", ["^IXIC"], rows, .01),
        _factor("korea_market", "韩国市场环境", ["^KS11"], rows, .01),
        _factor("memory_chain", "韩国存储链", ["005930.KS", "000660.KS"], rows, .02),
    ]
    risk = _risk(factors)
    ready = sum(row["state"] == "ready" for row in quotes)
    state = "historical_unverified" if historical else "ready" if ready == 4 else "limited" if ready else "unavailable"
    label = {"caution": "外围存在压力", "support": "外围出现支撑线索", "mixed": "外围方向存在分歧",
             "neutral": "外围未达方向阈值", "unknown": "外围因素待核验"}[risk]
    affected = []
    if any(factor["state"] != "unknown" for factor in factors[:2]):
        affected.append("科技")
    if factors[2]["state"] != "unknown":
        affected.extend(["半导体", "存储"])
    return OverseasContext.model_validate({
        "source": source, "fetched_at": fetched.astimezone(CN_TZ).isoformat() if fetched else None,
        "cutoff": cutoff.isoformat(), "state": state, "quotes": quotes, "factors": factors,
        "summary": f"{label}; {ready}/4项行情可作参考, 各市场时间独立标注。",
        "psychology_link": _psychology_link(psychology, risk), "risk_state": risk,
        "affected_sectors": affected, "limitations": limitations,
    }).model_dump(mode="json")
