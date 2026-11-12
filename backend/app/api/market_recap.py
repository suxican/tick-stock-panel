"""AI 大盘复盘 API — 流式复盘 + 报告持久化 + 龙虎榜 + 盘前风向标。

路由前缀: /api/market-recap

端点:
  POST /analyze                AI 流式大盘复盘(NDJSON)
  GET  /reports                历史复盘列表
  POST /reports                保存一条复盘报告
  DELETE /reports/{report_id}  删除一条复盘报告
  GET  /dragon-tiger           龙虎榜三榜 (fuyao 专有, 历史按日缓存)
  GET  /auction-benchmark      盘前风向标 (fuyao 专有, 含当日/次日真实收益)
  GET  /bellwethers            盘中风向标 (T-1 选定, 对照当前行情)
"""
from __future__ import annotations

import logging
from datetime import date as date_cls

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.services import auction_benchmark, dragon_tiger, market_recap_reports, preferences
from app.services.market_recap import recap_market_stream
from app.services.ndjson_heartbeat import with_heartbeat

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/market-recap", tags=["market-recap"])


class AnalyzeRequest(BaseModel):
    """AI 大盘复盘请求。"""
    as_of: str | None = None  # 可选:复盘日期(YYYY-MM-DD),缺省取最新有数据日
    focus: str = ""           # 可选:用户追加的复盘关注点


@router.get("/dragon-tiger")
def get_dragon_tiger(
    request: Request,
    date: str | None = Query(default=None, description="复盘目标日 YYYY-MM-DD, 缺省取最近已发布交易日"),
):
    """龙虎榜三榜 (全部/机构/游资)。fuyao 专有, 未配置时 state=source_unavailable。

    非交易日/当日未发布由服务层自动回退到上一交易日 (state=fallback_prev)。
    """
    target = None
    if date:
        try:
            target = date_cls.fromisoformat(date)
        except ValueError:
            raise HTTPException(400, f"date 格式应为 YYYY-MM-DD, 收到: {date}")
    return dragon_tiger.get_dragon_tiger(
        request.app.state.repo.store.data_dir, target
    )


@router.get("/auction-benchmark")
def get_auction_benchmark(
    request: Request,
    date: str | None = Query(default=None, description="复盘目标日 YYYY-MM-DD, 缺省取最近交易日"),
):
    """盘前风向标 (同花顺竞价筛选名单 + 当日/次日真实收益)。

    fuyao 专有, 未配置时 state=source_unavailable; 非交易日由服务层自动回退。
    """
    target = None
    if date:
        try:
            target = date_cls.fromisoformat(date)
        except ValueError:
            raise HTTPException(400, f"date 格式应为 YYYY-MM-DD, 收到: {date}")
    return auction_benchmark.get_auction_benchmark(
        request.app.state.repo.store.data_dir, target
    )


@router.post("/analyze")
async def analyze_market(request: Request, req: AnalyzeRequest):
    """AI 大盘复盘 — NDJSON 流式返回。

    装配市场总览(指数/涨跌/连板/封板/板块/情绪雷达)→ 复盘提示词 →
    流式调用 LLM → 逐 chunk 以 NDJSON 推给前端(每行一个 JSON)。

    协议:
      {"type":"meta","as_of","emotion_score","emotion_label","summary"}
      {"type":"delta","content":"..."}
      {"type":"error","message":"..."}
      {"type":"done"}
    """
    from datetime import date as date_cls

    repo = request.app.state.repo
    quote_service = getattr(request.app.state, "quote_service", None)
    depth_service = getattr(request.app.state, "depth_service", None)

    as_of = None
    if req.as_of:
        try:
            as_of = date_cls.fromisoformat(req.as_of)
        except ValueError:
            raise HTTPException(400, f"as_of 格式应为 YYYY-MM-DD,收到: {req.as_of}")

    async def stream_gen():
        async for chunk in with_heartbeat(
            recap_market_stream(
                repo, quote_service, depth_service, as_of, req.focus,
                sector_service=getattr(request.app.state, "sector_monitor_service", None),
            ),
        ):
            yield chunk + "\n"

    return StreamingResponse(
        stream_gen(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ================================================================
# 盘中剧本核验(10:00 后判定今天倾向上一篇剧本的哪个情景)
# ================================================================

@router.get("/intraday-check")
def get_intraday_check(request: Request):
    """今日最近一次盘中核验结果; 今日尚未核验时 result=null。"""
    from app.market_time import cn_today
    from app.services.market_recap_intraday import latest_result

    return {"result": latest_result(cn_today().isoformat())}


@router.post("/intraday-check")
def run_intraday_check(request: Request):
    """手动执行一次盘中核验(归档, 不推送; 定时任务才推送)。"""
    from app.services.market_recap_intraday import run_and_record

    return {"result": run_and_record(request.app.state.repo, push=False)}


# ================================================================
# 盘中风向标(T-1 选定的情绪/龙头/中军, 对照当前行情; 区别于竞价的盘前风向标)
# ================================================================

@router.get("/bellwethers")
def get_bellwethers(
    request: Request,
    scope: str = Query(default="market", pattern="^(market|holdings|watchlist)$",
                       description="market=全市场; holdings=批次提醒持仓所属板块; watchlist=自选所属板块"),
    group_id: str | None = Query(default=None, description="自选分组 ID, 仅 scope=watchlist 时生效"),
):
    """盘中风向标快照; 关键数据缺失时 status=unavailable 并给出原因。"""
    from app.services import market_bellwether

    state = request.app.state
    snapshot = market_bellwether.get_snapshot(
        state.repo, scope=scope, group_id=group_id if scope == "watchlist" else None,
        quote_service=getattr(state, "quote_service", None),
        depth_service=getattr(state, "depth_service", None),
    )
    # 后台记录器未启动(如无行情服务)时不附加时间线, 页面照常显示当前状态
    tracker = getattr(state, "bellwether_tracker", None)
    return tracker.timeline.annotate(snapshot) if tracker is not None else snapshot


# ================================================================
# 报告 CRUD(历史复盘持久化)
# ================================================================

class SaveReportRequest(BaseModel):
    """保存一条 AI 大盘复盘报告。"""
    as_of: str
    focus: str = ""
    content: str
    summary: str = ""
    emotion_score: int | None = None
    emotion_label: str = ""
    # 复盘数据快照(meta.snapshot 原样回传), 历史报告据此重绘表格/图表; 旧客户端不传为 None
    snapshot: dict | None = None
    push: bool = False  # 是否显式外发推送(manual 模式下需显式传 true)


@router.get("/reports")
def list_reports(request: Request):
    """获取全部历史复盘(按时间降序,后端已裁剪到上限)。"""
    return {"reports": market_recap_reports.list_reports()}


@router.post("/reports")
def save_report(request: Request, req: SaveReportRequest):
    """保存一条复盘报告。req.push=True 或 review_push_mode=auto 时才推送到外部渠道。

    正文末尾的 ```plan 结构化剧本被剥离为 report["plan"], 供下一篇复盘系统核验。
    """
    from app.services.market_recap_plan import split_plan_block

    content, plan = split_plan_block(req.content)
    report = market_recap_reports.save_report({
        "as_of": req.as_of,
        "focus": req.focus,
        "content": content,
        "plan": plan,
        "summary": req.summary,
        "emotion_score": req.emotion_score,
        "emotion_label": req.emotion_label,
        "snapshot": req.snapshot,
    })
    # 推送门控: manual 模式需显式 push=True; auto 模式保持归档即推。
    # 内部 try/except 静默降级, 不影响归档返回值。
    if req.push or preferences.get_review_push_mode() == "auto":
        from app.jobs.daily_pipeline import _maybe_push_review
        _maybe_push_review(content, {
            "as_of": req.as_of,
            "emotion_label": req.emotion_label,
        })
    return {"ok": True, "report": report}


@router.delete("/reports/{report_id}")
def delete_report(request: Request, report_id: str):
    """删除一条复盘报告。"""
    ok = market_recap_reports.delete_report(report_id)
    return {"ok": ok}
