"""资讯分析页的数据入口。"""

from datetime import date

from fastapi import APIRouter, HTTPException, Request

from app.extensions import BACKEND_EXTENSION_API_VERSION, BackendExtensionRegistrar
from app.market_time import cn_today
from app.services.news_catalysts import get_catalysts

EXTENSION_ID = "official.news"
EXTENSION_API_VERSION = BACKEND_EXTENSION_API_VERSION

router = APIRouter(prefix="/api/news", tags=["news"])


@router.get("/catalysts")
def catalysts(request: Request, date: date | None = None) -> dict:
    """某交易日的催化分组与盘面核对; 缺省取北京今天, 非交易日回退到最近交易日。"""
    if date is not None and date > cn_today():
        raise HTTPException(422, "不能查询未来日期")
    return get_catalysts(request.app.state.repo, date)


def setup(registrar: BackendExtensionRegistrar) -> None:
    registrar.include_router(router)
