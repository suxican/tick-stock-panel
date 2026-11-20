"""异动监控 API — 竞价/盘中/偏移三类异动。

- /intraday: 盘中量价信号聚合 (enriched 当日信号列, 零新增采集)
- /overview: 偏移异动边缘总览 (交易所异动规则口径的接近度)
"""
from __future__ import annotations

from fastapi import APIRouter, Query, Request

from app.services.abnormal_moves import build_intraday, build_overview

router = APIRouter(prefix="/api/abnormal", tags=["abnormal"])


@router.get("/intraday")
def abnormal_intraday(
    request: Request,
    limit: int = Query(500, ge=1, le=2000),
):
    """盘中异动: 涨停/炸板/跌停翘板/跌停/新高/新低/放量 信号命中行。"""
    repo = request.app.state.repo
    return build_intraday(repo, limit=limit)


@router.get("/overview")
def abnormal_overview(
    request: Request,
    min_closeness: float = Query(0.5, ge=0.0, le=1.0),
    limit: int = Query(200, ge=1, le=1000),
):
    """异动边缘总览: 规则表 + 各窗口实时偏离 + 接近度排序。

    min_closeness: 0.5=观察 / 0.7=边缘 / 1.0=已触发。
    """
    repo = request.app.state.repo
    quote_service = getattr(request.app.state, "quote_service", None)
    return build_overview(repo, quote_service, min_closeness=min_closeness, limit=limit)


@router.get("/regulatory")
def abnormal_regulatory(request: Request):
    """交易所口径: 下一个交易时段适用的严重异动预测与重点监控名单 (读盘后留档, 不请求上游)。

    目标交易日: 交易日收盘前为当天, 之后或非交易日为下一交易日; 某项留档缺失为 null (不可用)。
    """
    from app.market_time import cn_now
    from app.services import holding_analysis
    from app.services.regulatory_reference import regulatory_listing

    repo = request.app.state.repo
    day = holding_analysis.trade_day(cn_now())
    if day is None:
        return {"target_day": None, "anomaly": None, "monitor": None, "anomaly_as_of": None, "monitor_as_of": None}
    code_map: dict[str, str] = {}
    try:
        counts: dict[str, int] = {}
        symbols = [str(s) for s in repo.get_instruments()["symbol"].to_list()]
        for symbol in symbols:
            counts[symbol[:6]] = counts.get(symbol[:6], 0) + 1
        # 六位代码在目录里唯一时才补后缀 (如 000001 同时是平安银行与上证指数则不猜)
        code_map = {s[:6]: s for s in symbols if counts[s[:6]] == 1}
    except Exception:  # noqa: BLE001 -- 目录不可用时保留六位代码, 名单照常显示
        code_map = {}
    return regulatory_listing(day, repo.store.data_dir, code_map)
