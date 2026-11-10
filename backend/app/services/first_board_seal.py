"""涨停价候选的封板质量: 只读五档封单旁路已有的判定, 不拉盘口、不改买入门槛。

封单量沿用盘口口径, 单位为手; 封单额 = 手 × 100 × 原价, 单位为元。
"""
from __future__ import annotations

import logging
import math
from datetime import date

logger = logging.getLogger(__name__)
# 与封单轮询间隔的硬上限一致: 盘中超过它仍未更新的判定不能当作当前封单。
MAX_AGE_SECONDS = 300.0
_UNREADY = "五档盘口封单数据未就绪; 请在数据源设置确认盘口能力, 或等待下一次轮询"


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def load_sealed(depth_service, as_of: date) -> dict:
    """一次评估读取一次涨停方向的封单表; 任何失败都降级为未就绪。"""
    if depth_service is None:
        return {"ready": False, "age": None, "entries": {}, "reason": _UNREADY}
    try:
        if not depth_service.is_sealed_ready(as_of):
            return {"ready": False, "age": None, "entries": {}, "reason": _UNREADY}
        entries = depth_service.get_sealed_map(as_of, is_down=False)
        return {"ready": True, "age": _number(depth_service.get_sealed_age(as_of)),
                "entries": entries if isinstance(entries, dict) else {}, "reason": ""}
    except Exception:  # noqa: BLE001 - 可选旁路不得中断首板评估
        logger.warning("首板封单数据读取失败, 封板质量待确认", exc_info=True)
        return {"ready": False, "age": None, "entries": {}, "reason": "封单数据读取失败, 封板质量待确认"}


def seal_quality(sealed: dict, symbol: str, *, price: float | None, amount: float | None,
                 live: bool) -> dict:
    """status: real 真封 / fake 卖一仍有挂单 / pending 待确认 / unavailable 数据不可用。

    amount 为当日成交额(元); 封成比 = 封单额 / 成交额。缺任一输入时对应字段为 None。
    """
    result = {"status": "unavailable", "vol_lots": None, "sealed_amount": None, "seal_ratio": None,
              "age_seconds": sealed.get("age"), "reason": sealed.get("reason") or _UNREADY}
    if not sealed.get("ready"):
        return result
    age = sealed.get("age")
    if live and age is not None and age > MAX_AGE_SECONDS:
        result["reason"] = f"封单数据已超过 {MAX_AGE_SECONDS:.0f} 秒未更新, 不作为当前判定"
        return result
    entry = sealed.get("entries", {}).get(symbol)
    if not isinstance(entry, dict):
        result.update(status="pending", reason="该股尚未进入盘口封单轮询")
        return result
    flag = entry.get("sealed")
    vol = _number(entry.get("vol"))
    vol = vol if vol is not None and vol >= 0 else None
    price, amount = _number(price), _number(amount)
    sealed_amount = vol * 100 * price if vol is not None and price is not None and price > 0 else None
    result.update(
        status="real" if flag is True else "fake" if flag is False else "pending",
        vol_lots=vol, sealed_amount=sealed_amount,
        seal_ratio=sealed_amount / amount if sealed_amount is not None and amount is not None and amount > 0 else None,
        reason=("涨停价上卖一无挂单" if flag is True else "涨停价上卖一仍有挂单, 封单不稳" if flag is False
                else "盘口尚未给出封单判定"),
    )
    return result
