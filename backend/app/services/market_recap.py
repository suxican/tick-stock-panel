"""AI 大盘复盘 —— 流式 LLM 复盘生成。

复刻 stock_analyzer.py 的 NDJSON 流式协议(meta/delta/error/done),
将「市场总览」聚合数据 + 复盘快照(近期对比/情绪周期/连板梯队/涨停主线,
见 market_recap_snapshot) + 上一篇复盘的剧本交给 LLM, 生成交易员视角的五节复盘。
数字由前端用快照画成表格/图表, LLM 正文只写判断与计划。

数据来源:services.market_overview_builder.build_market_overview
(与 GET /api/overview/market 同源,保证复盘与看板数据口径一致)。
情绪分只用 overview["regime_state"](regime 口径, 与 Regime 页同值), 不用看板「盘面温度」emotion。

流式协议(与 stock_analyzer / financial_analyzer 一致,前端解析无差异):
    {"type":"meta", "as_of", "emotion_score", "emotion_label", "summary", "snapshot"}
    {"type":"delta","content":"..."}   逐 chunk 文本
    {"type":"error","message":"..."}
    {"type":"done"}
"""
from __future__ import annotations

import json
import logging
from datetime import date
from typing import AsyncIterator

from app.services.market_overview_builder import build_market_overview

logger = logging.getLogger(__name__)


# 指数简称映射:摘要里用简称(上/深/创/科),全称太长列表放不下。与前端 INDEX_SHORT 对齐。
_INDEX_SHORT = {
    "上证指数": "上",
    "深证成指": "深",
    "创业板指": "创",
    "科创综指": "科",
    "科创50": "科",
}

# ================================================================
# 系统提示词(交易员复盘 + 固定五节模板)
# ================================================================

_SYSTEM_PROMPT = """你是一位有 15 年 A 股短线与波段实战经验的交易员,为自己写盘后复盘,目的是校验昨天的判断、定位情绪周期、制定明天的计划。

读者会同时看到页面上的数据面板(核心指标今日/昨日/5日均对比表、成交额与涨停走势图、涨停主线表、连板梯队),所以**正文不复述面板数字**,只写判断、因果和计划。

## 输出结构(严格按以下各节,有持仓输入时 6 节、否则 5 节,Markdown,不加标题行和其他章节,正文 400-800 字)

## 🎯 定调
一行:**【周期:<阶段> · 状态:偏强/中性/偏弱】** + 一句核心矛盾(不超过 40 字)。周期优先采用输入的"情绪周期"阶段;你判断不同时,在句中给出理由。盘中快照须注明"盘中"。

## ✅ 昨日预判验证
- 输入含"昨日剧本系统核验"时:页面已展示核验表,本节**不画表、不改判**,只用 1-3 句说明主判断命中或落空的原因,以及明天要修正什么。
- 只有"上一交易日复盘的剧本"文本时:表格 `| 预判 | 实际 | 结果 |`,结果只用 ✅兑现 / ❌证伪 / ➖待定,逐条对照;表后一句话说明偏差原因。
- 没有上一篇复盘时只写一句"无上一交易日复盘可验证"。

## 🔥 主线与龙头
表格 `| 方向 | 阶段 | 龙头/梯队 | 判断 |`,最多 4 行。阶段用 首日/发酵/高潮/分歧/退潮(结合"连续 N 日位列前10")。判断不超过 25 字,说清资金是否确认(龙虎榜)、有无补涨或掉队。表后一行"退潮/流出方向:……"。

## 🗺️ 明日剧本
表格 `| 情景 | 触发条件 | 应对 |`,2-3 行(强于预期/符合预期/弱于预期),在情景名后用"(主判断)"标出你认为最可能的一个。触发条件只能用输入"明日剧本可用的触发指标"里的指标,表格中写中文含义(如"开盘半小时成交额 ≥ 5500亿"),不写英文 metric 名;优先用开盘半小时成交额(开盘后即可验证);阈值必须参照该表的今日值与前5日均给出,不得凭空设定。应对写参与方向与参与强度(如"主线核心加大参与""只做分歧转一致""降低参与度、以观望为主"),不点名推荐买入具体个股。

## 📦 持仓联动
仅当输入含"持仓联动"时输出本节,否则整节省略。页面已展示持仓表与程序条件,本节只做解释,2-4 句:哪些持仓与今天的主线判断同向、哪些最弱,明日剧本的各情景分别会怎样影响它们。**不得新增或改动价位、数量和操作动作**,提到条件时只能原样引用输入中的条件;标注"只看市场联动"的持仓只谈联动,不谈仓位处理。

## ⛔ 失效条件
1-3 条列表,出现即说明今天的定调错了。

然后单独一行:
> ⚠️ 本内容由 AI 基于公开行情数据生成,仅供个人复盘与计划参考,不构成投资建议。

**最后**输出一个 plan 代码块,把明日剧本表格结构化,供系统次日自动核验(页面不显示)。格式:
```plan
{"scenarios":[{"name":"强于预期","primary":false,"conditions":[{"metric":"amount_30m","op":">=","value":6000},{"metric":"limit_up","op":">=","value":90}]},{"name":"符合预期","primary":true,"conditions":[{"metric":"boards","symbol":"603366.SH","name":"日出东方","op":">=","value":5}]},{"name":"弱于预期","primary":false,"match":"any","conditions":[{"metric":"amount_30m","op":"<","value":4800},{"metric":"limit_up","op":"<","value":70}]}]}
```
规则:metric 只能取触发指标表中的值;op 只能是 > >= < <=;value 是数字,单位与表中一致(亿/家/%/板);同一情景内条件默认同时成立才算命中,表格里写"或"的情景加 "match":"any"(任一成立即命中);"区间"写成两个条件(如 ≥4800 与 <5500);每个情景 1-3 个条件,与表格触发条件一一对应、不得省略;恰好一个情景 primary 为 true;boards 必须带 symbol(用输入中的代码)和 name。

## 准则
1. 只输出结论,不写分析过程和"我先看……"之类的元话语。
2. 数字只为支撑判断而引用,每节不超过 3 个;引用时指标名必须与输入完全一致(如"封板率"不得写成"炸板率"),不自行换算新指标。
3. 量能只依据输入中的两市成交额及其对比判断,不要用其他指标推断"放量/缩量"。
4. 数据日期与复盘日不一致的(如龙虎榜回退到上一期),不得当作复盘日数据。
5. 没有新闻数据时不编造具体消息;如需提及催化,用一句推断放进主线判断里。
6. 表格单元格内不换行,不使用竖线字符。

现在请基于下方数据进行复盘。"""


def effective_system_prompt() -> str:
    """实际下发给 LLM 的系统提示词: 用户自定义优先, 空/未配置回退内置默认模板。

    读取走 preferences 的 mtime 缓存, 每次复盘调用一次, 开销可忽略。
    自定义值是保存时刻的快照 —— 后续版本升级默认模板不会自动同步到已自定义用户
    (设置页 UI 有提示); 脏值由 setter 层拦截, 这里只做空值回退双保险。
    """
    from app.services import preferences

    return preferences.get_market_recap_system_prompt() or _SYSTEM_PROMPT


# ================================================================
# 用户消息构建(精简切片,控制 token)
# ================================================================

def _fmt_pct(v, suffix="%") -> str:
    if v is None:
        return "—"
    return f"{v:+.2f}{suffix}" if suffix else f"{v:.2f}"


def _build_indices_block(overview: dict) -> str:
    """指数行情精简块。"""
    indices = overview.get("indices") or []
    if not indices:
        return "(暂无指数)"
    lines = []
    for idx in indices:
        name = idx.get("name") or idx.get("symbol")
        price = idx.get("last_price")
        chg = idx.get("change_pct")
        price_s = f"{price:.2f}" if price is not None else "—"
        lines.append(f"- {name}: {price_s}  {_fmt_pct(chg)}")
    return "\n".join(lines)


def _build_breadth_block(overview: dict) -> str:
    b = overview.get("breadth") or {}
    amt = overview.get("amount") or {}
    lim = overview.get("limit") or {}
    tr = overview.get("trend") or {}
    act = overview.get("activity") or {}

    total_amount = amt.get("total") or 0
    # 成交额单位换算为亿元(原始为元)
    amount_yi = total_amount / 1e8 if total_amount else 0

    lines = [
        f"- 上涨/下跌/平盘: {b.get('up',0)} / {b.get('down',0)} / {b.get('flat',0)}"
        f"  (上涨占比 {b.get('up_pct',0):.1f}%)",
        f"- 涨停/炸板/跌停: {lim.get('limit_up',0)} / {lim.get('broken',0)} / {lim.get('limit_down',0)}"
        f"  (封板率 {lim.get('seal_rate',0):.0f}%, 最高连板 {lim.get('max_boards',0)})",
    ]
    if lim.get("tiers"):
        tiers_str = "、".join(f"{t['boards']}板×{t['count']}" for t in lim["tiers"][:5])
        lines.append(f"- 连板梯队: {tiers_str}")
    lines.append(f"- 两市成交额: {amount_yi:.0f} 亿元(与昨日/5日均对比见「近期对比」)")
    lines.append(
        f"- 均线站位: MA5 {tr.get('above_ma5_pct',0):.0f}% / "
        f"MA20 {tr.get('above_ma20_pct',0):.0f}% / MA60 {tr.get('above_ma60_pct',0):.0f}%"
    )
    # 不再提供"个股平均量比": 它是个股量比的等权均值, 不代表市场成交额变化,
    # 曾导致报告在放量日得出"量能收缩"的错误结论。量能统一看成交额对比。
    lines.append(f"- 平均换手: {act.get('avg_turnover',0):.2f}%")
    return "\n".join(lines)


def _build_sector_block(rank: dict, label: str) -> str:
    """板块排名精简块(领涨/领跌 top5)。"""
    if not rank:
        return f"### {label}\n(暂无数据)"
    def _fmt(items):
        if not items:
            return "—"
        return "、".join(
            f"{it.get('name')}({(it.get('avg_pct') or 0)*100:+.2f}%,领涨:{it.get('leader',{}).get('name','—')})"
            for it in items[:5]
        )
    return (
        f"- 领涨{label}: {_fmt(rank.get('leading'))}\n"
        f"- 领跌{label}: {_fmt(rank.get('lagging'))}"
    )


_SUBSCORE_LABELS = {"profit": "赚钱", "speculation": "投机", "resilience": "抗跌", "trend": "趋势"}


def _regime_state(overview: dict) -> dict:
    """复盘唯一情绪分: 总览的 regime 口径状态(见 services.regime_state)。"""
    return overview.get("regime_state") or {}


def _build_emotion_block(overview: dict) -> str:
    rs = _regime_state(overview)
    if rs.get("score") is None:
        return "(暂无)"
    basis = "盘中估算, 收盘后以盘后计算为准" if rs.get("source") == "estimate" else "盘后口径"
    lines = [f"- 情绪分: {rs['score']} ({rs.get('label') or '—'}, {basis})"]
    subs = rs.get("subscores") or {}
    dims = "、".join(f"{label}{subs[k]}" for k, label in _SUBSCORE_LABELS.items() if subs.get(k) is not None)
    if dims:
        lines.append(f"- 情绪分项: {dims}")
    return "\n".join(lines)


def _build_user_prompt(overview: dict, news: list[dict], focus: str, lhb_context: str = "",
                       bench_context: str = "", snapshot_context: str = "",
                       prev_plan: dict | None = None, plan_check_context: str = "",
                       plan_vocab_context: str = "", holdings_context: str = "") -> str:
    """构建用户消息:复盘日期 + 近期对比/周期/梯队/主线(快照) + 市场数据精简切片
    + 龙虎榜(可选) + 盘前风向标(可选) + 上一篇剧本 + 新闻 + 关注点。"""
    as_of = overview.get("as_of") or "今日"

    parts: list[str] = [f"复盘日期: {as_of}", ""]
    if snapshot_context:
        parts.extend([snapshot_context, ""])
    parts.extend([
        "## 主要指数",
        _build_indices_block(overview),
        "",
        "## 盘面数据",
        _build_breadth_block(overview),
        "",
        "## 市场情绪",
        _build_emotion_block(overview),
        "",
        "## 概念板块排名",
        _build_sector_block(overview.get("concept_rank"), "概念"),
        "",
        "## 行业板块排名",
        _build_sector_block(overview.get("industry_rank"), "行业"),
    ])

    # 龙虎榜资金动向 (fuyao 数据源, 摘要自带数据日期; 无数据源/失败时为空不占段)
    if lhb_context:
        parts.extend([
            "",
            "## 龙虎榜资金动向",
            lhb_context,
        ])

    # 盘前风向标 (fuyao 竞价筛选名单 + 当日实际表现对照; 失败为空不占段)
    if bench_context:
        parts.extend([
            "",
            "## 盘前风向标(竞价)",
            bench_context,
        ])

    if plan_check_context:
        # 有结构化剧本的系统核验时, 模型只解释原因, 不再自评
        parts.extend(["", plan_check_context])
    else:
        parts.extend(["", "## 上一交易日复盘的剧本(待验证)", _prev_plan_text(prev_plan)])
    if plan_vocab_context:
        parts.extend(["", plan_vocab_context])
    if holdings_context:
        parts.extend(["", holdings_context])

    if news:
        news_lines = []
        for i, n in enumerate(news[:8], 1):
            title = (n.get("title") or "").strip()
            snippet = (n.get("snippet") or "").strip()
            source = (n.get("source") or "").strip()
            pub = (n.get("published_date") or "").strip()
            meta = " / ".join(p for p in (source, pub) if p)
            news_lines.append(f"{i}. {title} ({meta})\n   {snippet}" if meta else f"{i}. {title}\n   {snippet}")
        parts.extend(["", "## 近期市场新闻", "\n".join(news_lines)])
    else:
        parts.extend([
            "",
            "## 近期市场新闻",
            "(暂无新闻数据:本功能新闻检索能力将在后续版本接入。"
            "消息催化一节请直接从量价异动给出可能的催化逻辑结论,不要编造具体消息,也不要复述本说明。)",
        ])

    from app.services.ai_provider import build_focus_instruction
    focus_instruction = build_focus_instruction(focus, report_name="大盘复盘报告")
    if focus_instruction:
        parts.extend(["", focus_instruction])

    return "\n".join(parts)


# ================================================================
# 上一篇复盘的剧本(供"昨日预判验证")
# ================================================================

# 新模板"明日剧本/失效条件", 旧模板"后续观察要点/风险提示"
_PLAN_HEADINGS = ("剧本", "失效", "后续观察", "观察要点")
_PREV_PLAN_MAX_CHARS = 1500


def _extract_plan_sections(content: str) -> str:
    """从复盘 Markdown 中截取计划类二级小节(到下一个二级标题或免责声明为止)。"""
    sections: list[list[str]] = []
    current: list[str] | None = None
    for line in (content or "").splitlines():
        if line.startswith("## "):
            current = [line] if any(h in line for h in _PLAN_HEADINGS) else None
            if current is not None:
                sections.append(current)
        elif line.startswith("> ⚠️"):
            current = None
        elif current is not None:
            current.append(line)
    text = "\n\n".join("\n".join(sec).strip() for sec in sections)
    return text[:_PREV_PLAN_MAX_CHARS]


def strip_holdings_section(content: str) -> str:
    """去掉「📦 持仓联动」二级小节(推送外发前调用, 个人持仓不出本机)。"""
    out, skipping = [], False
    for line in (content or "").splitlines(keepends=True):
        if line.startswith("## "):
            skipping = "持仓联动" in line
        if not skipping:
            out.append(line)
    return "".join(out)


def _load_reports() -> list[dict]:
    try:
        from app.services import market_recap_reports

        return market_recap_reports.list_reports()
    except Exception as e:  # noqa: BLE001
        logger.debug("load archived recaps failed: %s", e)
        return []


def find_prev_report(as_of: str, reports: list[dict] | None = None) -> dict | None:
    """复盘日之前最近一篇归档复盘; 同一日多篇(盘中 + 盘后)取最后生成的一篇。"""
    reports = _load_reports() if reports is None else reports
    candidates = [r for r in reports if str(r.get("as_of") or "") < as_of and r.get("content")]
    if not candidates:
        return None
    return max(candidates, key=lambda r: (str(r.get("as_of")), str(r.get("created_at") or "")))


def find_prev_plan(as_of: str, reports: list[dict] | None = None) -> dict | None:
    """复盘日之前最近一篇归档复盘的剧本文本; 无则 None。"""
    prev = find_prev_report(as_of, reports)
    if not prev:
        return None
    plan = _extract_plan_sections(prev["content"])
    return {"as_of": prev.get("as_of"), "plan": plan} if plan else None


def attach_plan_check(snapshot: dict | None, repo, reports: list[dict]) -> None:
    """系统核验上一交易日的结构化剧本, 结果与历史命中率写入快照(就地)。

    只核验紧邻的上一交易日的剧本: 中间缺了复盘时, 旧剧本针对的不是复盘日。
    """
    if not snapshot:
        return
    from app.services.market_recap_plan import archived_checks, evaluate_plan, plan_stats

    as_of = snapshot["as_of"]
    dates = snapshot.get("dates") or []
    prev = find_prev_report(as_of, reports)
    check = None
    if prev and prev.get("plan") and len(dates) >= 2 and dates[-2] == prev.get("as_of"):
        try:
            check = evaluate_plan(prev["plan"], snapshot, repo, from_as_of=prev["as_of"])
        except Exception as e:  # noqa: BLE001
            logger.warning("plan check failed for %s: %s", as_of, e)
    snapshot["plan_check"] = check
    stats = plan_stats(archived_checks(reports, as_of) + ([check] if check else []))
    snapshot["plan_stats"] = stats if (stats["primary_total"] or stats["covered_total"]) else None


def _prev_plan_text(prev_plan: dict | None) -> str:
    if not prev_plan:
        return "(无上一交易日复盘)"
    return f"(来自 {prev_plan['as_of']} 的复盘)\n{prev_plan['plan']}"


# ================================================================
# 摘要生成(供 meta 事件 / 历史报告 summary)
# ================================================================

def _recap_summary(overview: dict) -> str:
    """一句话摘要(供 meta 事件与历史列表展示)。

    指数用简称(上/深/创/科),与前端摘要条一致,避免列表里全称放不下。
    """
    indices = overview.get("indices") or []
    rs = _regime_state(overview)
    lim = overview.get("limit") or {}
    amt = overview.get("amount") or {}
    total_amount = (amt.get("total") or 0) / 1e8

    idx_str = "、".join(
        f"{_INDEX_SHORT.get(i.get('name') or '', i.get('name') or '')}{(i.get('change_pct') or 0):+.2f}%"
        for i in indices[:4]
    ) or "指数缺失"
    return (
        f"{idx_str} | 情绪{rs.get('score', '—')}({rs.get('label') or '—'}) | "
        f"涨停{lim.get('limit_up',0)} | 成交{total_amount:.0f}亿"
    )


# ================================================================
# 流式主入口
# ================================================================

async def recap_market_stream(
    repo,
    quote_service=None,
    depth_service=None,
    as_of: date | None = None,
    focus: str = "",
    news: list[dict] | None = None,
    sector_service=None,
) -> AsyncIterator[str]:
    """流式大盘复盘:yield 出每个 NDJSON 事件。

    sector_service: 板块成员服务(app.state.sector_monitor_service), 持仓联动用于
    判断批次持仓所属板块; 缺失时持仓板块关联降级, 复盘主流程不受影响。

    Args:
        repo: KlineRepository(必填)。
        quote_service / depth_service: 可选,数据装配依赖。
        as_of: 复盘日期,None 取最新有数据日。
        focus: 用户追加的复盘关注点。
        news: 预检索的新闻列表(P1 不传,留 None 走降级说明;P3 由 news_search 注入)。
    """
    # 1. 装配市场总览
    overview = build_market_overview(repo, quote_service, depth_service, as_of)
    as_of_str = overview.get("as_of")

    if not as_of_str:
        yield json.dumps({
            "type": "error",
            "message": "暂无市场数据,请先在「数据」页同步日 K 与指数后再复盘",
        }, ensure_ascii=False)
        return

    rs = _regime_state(overview)

    # 数据快照: 近期对比/周期/梯队/主线, 同时供 prompt 与前端结构化面板; 失败只降级
    from app.services.market_recap_holdings import holdings_prompt_block
    from app.services.market_recap_plan import plan_check_prompt_block, plan_vocabulary_block
    from app.services.market_recap_snapshot import build_recap_snapshot, snapshot_prompt_block

    try:
        snapshot = build_recap_snapshot(repo, overview)
    except Exception as e:  # noqa: BLE001
        logger.warning("recap snapshot failed for %s: %s", as_of_str, e)
        snapshot = None
    reports = _load_reports()
    attach_plan_check(snapshot, repo, reports)
    if snapshot:
        # 批次提醒持仓 × 盘面: 条件由程序给出, 失败只降级为无持仓段
        from app.market_time import cn_now
        from app.services.market_recap_holdings import build_holdings_linkage

        try:
            snapshot["holdings"] = build_holdings_linkage(
                repo, snapshot, sector_service=sector_service, quote_service=quote_service, now=cn_now(),
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("holdings linkage failed for %s: %s", as_of_str, e)
            snapshot["holdings"] = None

    # 2. meta 事件(前端据此先渲染信号灯/看板与数据面板)
    yield json.dumps({
        "type": "meta",
        "as_of": as_of_str,
        "emotion_score": rs.get("score"),
        "emotion_label": rs.get("label") or "—",
        "summary": _recap_summary(overview),
        "snapshot": snapshot,
    }, ensure_ascii=False)

    # 3+4. 构建 prompt + 流式调用 LLM(整体 try-except,任何异常 yield error,避免前端卡死)
    try:
        from app.services.ai_provider import stream_ai_text

        # 龙虎榜摘要 (fuyao 专有): 拉取失败/未配置 → 空串, 复盘主流程不受影响
        from app.services import dragon_tiger as dragon_tiger_svc

        # 龙虎榜/风向标按复盘日取数(而非"最新一期"), 补做历史复盘时不串日期
        recap_day = date.fromisoformat(as_of_str)
        lhb_ctx = dragon_tiger_svc.build_recap_context(repo.store.data_dir, recap_day)

        # 盘前风向标摘要 (fuyao 专有): 失败/未配置 → 空串
        from app.services import auction_benchmark as auction_benchmark_svc

        bench_ctx = auction_benchmark_svc.build_recap_context(repo.store.data_dir, recap_day)
        user_prompt = _build_user_prompt(
            overview, news or [], focus, lhb_ctx, bench_ctx,
            snapshot_context=snapshot_prompt_block(snapshot),
            prev_plan=find_prev_plan(as_of_str, reports),
            plan_check_context=plan_check_prompt_block(
                (snapshot or {}).get("plan_check"), (snapshot or {}).get("plan_stats"),
            ),
            plan_vocab_context=plan_vocabulary_block(snapshot),
            holdings_context=holdings_prompt_block((snapshot or {}).get("holdings")),
        )
        got_content = False
        async for delta in stream_ai_text(
            [
                {"role": "system", "content": effective_system_prompt()},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.5,
            # 不限制输出(推理模型思考 token 计入预算, 见 ai_provider.stream_ai_text)
            max_tokens=None,
            prefer_final_answer=True,
        ):
            got_content = True
            yield json.dumps({"type": "delta", "content": delta}, ensure_ascii=False)

    except Exception as e:  # noqa: BLE001
        logger.exception("AI market recap failed for %s: %s", as_of_str, e)
        yield json.dumps({"type": "error", "message": f"AI 复盘失败: {e}"}, ensure_ascii=False)
        return

    if not got_content:
        logger.warning("AI market recap ended with empty content for %s", as_of_str)
        yield json.dumps({"type": "error", "message": "AI 未返回正文(输出被截断), 请重试"}, ensure_ascii=False)
        return
    yield json.dumps({"type": "done"}, ensure_ascii=False)


async def recap_market_once(
    repo,
    quote_service=None,
    depth_service=None,
    as_of: date | None = None,
    focus: str = "",
    news: list[dict] | None = None,
) -> tuple[str | None, dict]:
    """非流式版本(供定时任务调用):累积全部 delta,返回 (content, meta)。

    content 为完整 Markdown 文本;失败时为 None。
    meta 含 as_of / emotion_score / emotion_label / summary(即使失败也尽量回填)。
    """
    content_parts: list[str] = []
    meta: dict = {"as_of": as_of.isoformat() if as_of else None}
    async for evt in recap_market_stream(repo, quote_service, depth_service, as_of, focus, news):
        try:
            obj = json.loads(evt)
        except Exception:  # noqa: BLE001
            continue
        t = obj.get("type")
        if t == "meta":
            meta = obj
        elif t == "delta":
            content_parts.append(obj.get("content", ""))
        elif t == "error":
            logger.warning("market recap error event: %s", obj.get("message"))
            return None, meta
    return "".join(content_parts), meta
