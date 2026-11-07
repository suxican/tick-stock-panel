"""AI 个股分析服务 — 技术价位 + 五维证据核验 + 条件化操作计划。

职责:
  组合一只股票的 K 线(含已算好的技术指标)+ 财务表 + 关键价位 →
  拼装综合分析与操作建议提示词 → 流式调用 LLM → 逐 chunk 吐给前端。

与 financial_analyzer.py 的区别(刻意区分,非复用):
  - 数据源:已同步 K 线、技术指标与财务表,不把量价推断冒充新闻或资金流
  - 输出:证据与缺口、技术状态、条件化买卖计划(财务分析仅做财务质量评价)

不知道: HTTP、前端、配置持久化。
"""
from __future__ import annotations

import json
import logging
import math
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path

import polars as pl

from app.indicators.levels import compute_levels, summarize_levels
from app.market_time import cn_today
from app.services.financial_sync import get_financial_df

logger = logging.getLogger(__name__)

# 注入最近多少根日 K(技术面分析样本)
_KLINE_WINDOW = 90
# 注入财务表的最近期数
_MAX_PERIODS = 4

_FINANCIAL_COLS = {
    "metrics": ["eps_basic", "bps", "ocfps", "roe", "roa", "gross_margin", "net_margin",
                "revenue_yoy", "net_income_yoy", "debt_to_asset_ratio", "operating_cash_to_revenue"],
    "income": ["revenue", "operating_profit", "net_income", "net_income_attributable",
               "net_income_deducted", "rd_expense", "financial_expense"],
    "balance_sheet": ["total_assets", "total_liabilities", "total_current_assets",
                      "total_current_liabilities", "cash_and_equivalents", "goodwill",
                      "accounts_receivable", "inventory", "short_term_borrowing",
                      "long_term_borrowing", "equity_attributable"],
    "cash_flow": ["net_operating_cash_flow", "net_investing_cash_flow",
                  "net_financing_cash_flow", "capex", "net_cash_change"],
}


# ================================================================
# 数据加载
# ================================================================

def _load_kline(repo, symbol: str) -> pl.DataFrame:
    """读取该标的最近 N 根日 K(已含技术指标 / 信号)。

    repo: KlineRepository;走内存缓存,性能可控。
    """
    end = cn_today()
    start = end - timedelta(days=_KLINE_WINDOW * 2)  # 多取一些保证交易日够
    # 按资产类型分流: ETF/指数走独立 enriched 存储 (无财务数据, 提示词已有兜底)
    df = repo.get_daily_asset(repo.resolve_asset_type(symbol), symbol, start, end)
    if df.is_empty():
        return df
    return df.tail(_KLINE_WINDOW)


def _clean_rows(df: pl.DataFrame, keep_cols: list[str]) -> list[dict]:
    """把 DataFrame 转成 JSON 安全的 dict 列表(只保留关键列 + 清洗 NaN/Inf + date→字符串)。

    polars 的 date 列会变成 Python datetime.date,json.dumps 无法直接序列化,
    必须转成 ISO 字符串,否则 json.dumps 抛 TypeError 让整个流静默失败。
    """
    import datetime
    cols = [c for c in keep_cols if c in df.columns]
    sub = df.select(cols)
    rows = []
    for rec in sub.to_dicts():
        clean = {}
        for k, v in rec.items():
            if isinstance(v, float):
                clean[k] = None if not math.isfinite(v) else round(v, 4)
            elif isinstance(v, (datetime.date, datetime.datetime)):
                clean[k] = v.isoformat()
            else:
                clean[k] = v
        rows.append(clean)
    return rows


def _load_financials(data_dir: Path, symbol: str) -> dict[str, list[dict]]:
    """复用已同步的四张报表,为盈利、现金流和负债核验提供有限期数的数据。"""
    out: dict[str, list[dict]] = {}
    for table, fields in _FINANCIAL_COLS.items():
        df = get_financial_df(data_dir, table)
        if df.is_empty() or "symbol" not in df.columns:
            out[table] = []
            continue
        df = df.filter(pl.col("symbol") == symbol)
        if df.is_empty():
            out[table] = []
            continue
        if "period_end" in df.columns:
            df = df.sort("period_end", descending=True, nulls_last=True)
        out[table] = _clean_rows(
            df.head(_MAX_PERIODS), ["period_end", "announce_date", *fields],
        )
    return out


# ================================================================
# 系统提示词 —— 有证据边界的操作建议,不把缺失维度包装成量化共振。
# ================================================================

_SYSTEM_PROMPT = """你是一位重视证据和风险预算的 A 股研究与交易分析师。基于提供的单只标的数据,给出买入、持有、加仓、减仓、卖出、止损、止盈或观望的条件化操作建议。目标是可复核的决策与风险控制,不得承诺长期稳定盈利或确定收益。

## 数据与判断边界

- 区分【事实】【推断】【待验证】,每项结论引用输入中的数值、日期或来源。空值、未提供和不适用均不是零值或利空。
- 本次只使用输入的数据,不能声称已检索外部资讯。量价强弱可作为技术承接迹象,不能冒充主力/北向/龙虎榜净流入,也不能据涨停或放量编造政策、订单、公告或利好。
- 未提供新闻样本、统计时段和情绪方法时,资讯正面占比、热度、衰减速度均为 N/A;无行业样本或历史估值序列时,不生成行业均值、估值分位或“低估”结论。未提供预期基准时不声称业绩超预期。
- 不得编造上涨/下跌概率或胜率。没有经验证的统计模型、样本和回测时写“未校准”,不用主观百分比代替。
- 标明行情截止交易日和财务报告期,不能把日线收盘价说成实时价。成交额不等于净流入;不同财报期间的累计值不能直接当单季增速比较。缺日期/单位时注明无法核验。
- 技术指标和关键价位基于前复权价格;操作表必须明确价格口径。只在提供了同日不复权参考价位时给出委托参考价,不得用前复权价格直接与不复权持仓成本算盈亏。价格不可核实时写“待核实”,给触发条件并暂缓新开仓。

## 报告结构

用 Markdown 输出约 1400-2200 字,优先呈现结论和操作表,不输出 JSON 或代码块。用户有关注重点时先直接回答,再完成下列结构。

### 1. 决策摘要与周期
给出【当前结论:条件买入 / 持有 / 减仓 / 卖出 / 观望 / 规避】、主要理由、行情日期和风险等级(低/中/高/资料不足)。未触发的条件不能写成已满足,无持仓信息时区分空仓与持仓情景。
优先使用用户指定周期;未指定时以短线 1-5 个交易日为主,补充波段 1-4 周的区别。季度判断需更长历史和产业证据,不足时不外推。说明启动/上升/震荡/走弱阶段;成长/价值/周期风格及赛道只在证据充分时定位。

### 2. 操作计划
用一张表给出「情景 / 动作 / 触发条件 / 价格或区间及依据 / 仓位参考 / 失效与退出条件」。至少覆盖空仓买入或等待、持仓持有或减仓、止损、止盈;加仓仅在原逻辑有效且新触发确认时考虑,不以亏损为加仓理由。
- 买入区间、止损价、第一/第二目标及止盈条件均需来自支撑压力、结构或波动数据,标注来源。目标是条件情景,不是收益承诺。数据不够则写 N/A 和所缺证据,不能随意填价格。
- 给出风险收益比: (第一目标价 - 入场价) / (入场价 - 止损价),使用同一价格口径、同一周期,列明三项数值。区间用最不利买入价和较近目标保守计算;仅当止损价 < 入场价 < 目标价时有效,否则不生成可开仓计划。注明未计费用/滑点;若不足 2:1,默认等待并解释,不能抬高目标凑比例。
- 未知总资产和风险承受度时不编造实际持仓比例或买入股数。仓位仅作示例上限:资料充分的低/中/高风险单股分别不超过 30%/20%/10%;资料不足不能当低风险,默认等待或只给待验证计划。若用户提供更严格风险预算,优先遵守。
- 止损先看结构/ATR,再核对风险预算。短线 5%、波段 10%仅作示例损失预算上限;结构止损过远则放弃或降低暴露,不能机械挪近止损线。股价跳空、跌停和流动性不足可能使实际损失超过预算。
- A 股股票执行须考虑 T+1、可卖数量、涨跌停、费用与滑点;不能承诺当日新买股票可立即卖出或止损必成交。ETF 按具体品种交易规则核验,不能一概套用股票规则;指数只分析,不写可直接下单买入指数。
- 有模拟盘持仓时逐账户给持有/减仓/退出情景,明确虚拟持仓;没有持仓时不假设用户已持有。

### 3. 技术面与关键价位
引用均线排列/方向、形态、MACD/KDJ/RSI/布林、成交量/成交额/量比/换手等已有数据,突出支持和反驳操作计划的证据。列出主要支撑、压力及来源,区分日线信号与尚未提供的分钟确认,不能编造分时承接。

### 4. 五维证据与共振校验
表格列为「维度 / 证据与日期 / 判断 / 得分或 N/A / 缺口」,逐项覆盖:
1. 业绩打底:营收/净利润/扣非增速、ROE、毛利率/净利率、现金流匹配与连续性。只在对应字段存在时分析。
2. 资金承接:真实资金流的 3/5/10 日持续性与量价配合;只有 OHLCV 时仅作技术观察,资金流评分 N/A。
3. 政策支撑:可核验政策原文、日期及与公司主营业务的关联;无材料则待验证。
4. 产业景气:供需、订单、竞争力和主营匹配度;概念标签或股价上涨不能证明龙头地位、订单增长或产业景气。
5. 资讯情绪:有来源、时间、去重口径的公告/新闻样本及事件落地程度;无样本则待验证,不推测“未公开利好”。
评分是证据分级,不是回测量化因子或收益概率:每维 0/1/2 分,分别表示有证据的明显不利/中性或相互矛盾/持续且相互印证的有利。必须说明依据,无法判断记 N/A,缺失不能记 0。只有五维都有充分证据才汇总满分 10 分;否则写“总分 N/A、已验证 X/5”,不得折算为十分制。ETF/指数的公司业绩维度为“不适用”,不算共振总分。
总分完整时 ≥8 为重点核验候选,6-7 为等待确认,≤5 为低优先级;分数不能直接触发买入或覆盖风险。缺失维度不妨碍给出有数据支撑的技术条件计划,但不得称“五维共振买入”。
交叉验证业绩与资金、政策与产业、估值与情绪、事件与量价;任一侧缺失就标明无法完成验证。记录相互矛盾的证据及对操作的影响,不机械用“某项不满足”推导永久剔除。

### 5. 估值、风险与复核清单
有数据时核对 PE/PB/PS/PEG、历史/行业分位、负债、现金流、商誉/减值、质押/解禁与监管事项;缺失时不得声称“无风险”。不把 PE 分位 70%/80%或资讯正面占比 60%当跨行业通用定律。
按“事实依据 → 对原逻辑的影响 → 触发后的动作”列出最重要的业绩、资金、价格、估值和事件风险。只有已验证的重大变化才触发调整,缺数据不是风险已发生。列出下一交易日需确认的信号和计划有效期,行情超出区间或新消息改变逻辑时重新评估。
最后用一句话重申当前动作及改变结论的条件。

报告末尾附: > AI 操作建议基于已提供的数据与假设,仅供决策参考,不保证收益;下单前请核对最新行情、公告及个人风险预算。
"""


# ================================================================
# 用户消息构建
# ================================================================

def _positive_price(value: float | None) -> bool:
    return value is not None and math.isfinite(value) and value > 0


def _execution_price_part(levels: dict[str, list[dict]], close: float | None,
                          raw_close: float | None) -> str:
    """将技术价位换至最新行情日的不复权尺度,不改变图表或历史价位。"""
    if not _positive_price(close) or not _positive_price(raw_close):
        return "不复权委托参考价位: 待核实(缺少有效的同日前复权/不复权收盘价);不得直接把技术价位作为委托价。"
    factor = raw_close / close
    converted = {
        key: [{**point, "value": round(point["value"] * factor, 4),
               "label": f"前复权{point['label']}折算"}
              for point in points if _positive_price(point.get("value"))]
        for key, points in levels.items()
    }
    return (
        f"同日不复权收盘价: {raw_close:.4f} 元;技术价位换算系数 raw_close/close={factor:.8f}。\n"
        f"不复权委托参考价位: {summarize_levels(converted, raw_close)}。\n"
        "仅按该行情日价格尺度折算,不是实时买卖报价;下单前核对最新价格、除权变化和最小报价单位。"
    )


def _paper_position_part(positions: list[dict], close: float | None, raw_close: float | None) -> str:
    """虚拟持仓上下文 (V3): 模拟盘持有该标的时注入提示词, 让 AI 结合仓位给建议。

    positions 为多账户列表 [{account_id, qty, avg_cost}, ...] (V2 账户隔离),
    只收 qty>0 的账户, 逐账户一行; 全空则返回空串 (不注入"空仓"之类误导性表述)。
    浮盈用 raw 口径: 持仓 avg_cost 是不复权 raw 成本, 与前复权 close 混算会在
    除权后产生虚假盈亏, 故缺少 raw_close 时不计算浮盈。
    """
    lines: list[str] = []
    for item in positions or []:
        qty = item.get("qty") or 0
        avg_cost = item.get("avg_cost") or 0
        if qty <= 0 or avg_cost <= 0:
            continue
        line = f"账户 {item.get('account_id') or 'default'}: 持仓 {qty} 股, 平均成本 {avg_cost:.3f} 元 (不复权)"
        if _positive_price(raw_close):
            line += f", 行情截止日收盘价 {raw_close:.2f} 元, 浮动盈亏 {(raw_close / avg_cost - 1) * 100:+.2f}%"
        else:
            line += ", 浮动盈亏待核实(缺少不复权收盘价)"
        lines.append(line)
    if not lines:
        return ""
    return (
        "\n以下是该标的在模拟盘 (虚拟账户) 的当前持仓:\n"
        + "\n".join(lines)
        + "\n请在结论中结合该虚拟持仓状态给出持有/减仓/加仓的可执行参考,"
        "并明确这是基于模拟盘虚拟持仓的建议。\n"
    )


def _build_user_prompt(
    kline_tail: list[dict],
    fins: dict[str, list[dict]],
    levels: dict[str, list[dict]],
    close: float | None,
    symbol: str,
    focus: str,
    asset_type: str = "stock",
    paper_positions: list[dict] | None = None,
    raw_close: float | None = None,
    name: str = "",
) -> str:
    """构建用户消息:标的 + 价位摘要 + 技术指标 JSON + 财务摘要 + 关注点 (+ 虚拟持仓)。

    asset_type 用于区分数据缺失与公司财务框架不适用的情况。
    """
    parts: list[str] = [
        f"标的标准代码: {symbol}",
        f"标的名称: {name or '未提供(不得猜测)'}",
        f"分析日期(北京时间): {cn_today()}",
        f"行情截止交易日: {kline_tail[-1].get('date') if kline_tail else '未知'}",
        f"前复权技术价位概览: {summarize_levels(levels, close)}",
        _execution_price_part(levels, close, raw_close),
        "数据口径: OHLC/均线/技术价位为前复权;amount 为成交额(元);"
        "change_pct 为小数比例(0.03=3%);turnover_rate 和财务百分比指标为百分点(3=3%)。",
        "未提供: 主力/北向/龙虎榜资金流、两融、机构持仓、政策原文与产业订单、"
        "新闻/公告/研报、资讯情绪样本、历史/行业估值分位、行业对比及业绩预期。"
        "这些维度须标记待验证,不能用量价替代或凭模型记忆补齐。",
        "",
        "以下是该标的最近日 K 数据(JSON,含 OHLCV 与已计算的技术指标。"
        f"实际 {len(kline_tail)} 根日 K,最多 {_KLINE_WINDOW} 个交易日,升序,非实时行情):",
        "```json",
        json.dumps(kline_tail, ensure_ascii=False),
        "```",
    ]

    has_fin = any(fins.values())
    if has_fin:
        parts.extend([
            "",
            "以下是已同步的财务数据(JSON,核心指标 + 利润表 + 资产负债表 + 现金流量表,"
            "每表最多 4 期,金额单位为元;缺表/缺字段不等于零,报告期与公告日分别列示):",
            "```json",
            json.dumps(fins, ensure_ascii=False),
            "```",
        ])
    elif asset_type == "index":
        parts.extend([
            "",
            "(该标的为指数: 公司财务/业绩评分不适用,不得编造报表或共振总分;"
            "仅给技术情景,不能直接下单买入指数。)",
        ])
    elif asset_type == "etf":
        parts.extend([
            "",
            "(该标的为 ETF: 公司财务/业绩评分不适用,不得编造报表或共振总分,"
            "也不要写成「财务接入中」;根据现有价量给条件计划,交易规则按该 ETF 品种核验。)",
        ])
    else:
        parts.extend([
            "",
            "(该标的暂无财务数据: 当前未提供可用报表。业绩和财务风险待验证,"
            "得分 N/A,不能推断亏损或无风险;可给技术条件计划,不能声称五维共振。)",
        ])

    from app.services.ai_provider import build_focus_instruction
    focus_instruction = build_focus_instruction(
        focus, report_name="个股分析报告", allow_trading_advice=True,
    )
    if focus_instruction:
        parts.extend(["", focus_instruction])
    paper_part = _paper_position_part(paper_positions or [], close, raw_close)
    if paper_part:
        parts.extend(["", paper_part])
    return "\n".join(parts)


# ================================================================
# 关键列筛选(控制上下文体积)
# ================================================================

_KLINE_KEEP_COLS = [
    "date", "open", "high", "low", "close", "volume", "amount", "change_pct",
    "ma5", "ma10", "ma20", "ma60",
    "macd_dif", "macd_dea", "macd_hist",
    "kdj_k", "kdj_d", "kdj_j",
    "rsi_6", "rsi_14", "rsi_24",
    "boll_upper", "boll_mid", "boll_lower",
    "atr_14", "vol_ratio_5d", "turnover_rate",
    "consecutive_limit_ups",
    # 信号类(布尔)——只挑对消息面推断有用的几个
    "signal_limit_up", "signal_broken_limit_up", "signal_macd_golden",
    "signal_macd_dead", "signal_ma_golden_5_20", "signal_volume_surge",
    "signal_boll_breakout_upper", "signal_boll_breakdown_lower",
]


# ================================================================
# 流式分析入口
# ================================================================

async def analyze_stock_stream(
    repo,
    data_dir: Path,
    symbol: str,
    focus: str = "",
) -> AsyncIterator[str]:
    """流式个股分析:yield 出每个 NDJSON 事件。

    协议(与 financial_analyzer 一致,前端解析无差异):
      {"type":"meta","symbol","summary","levels"}  数据 + 价位摘要
      {"type":"delta","content":"..."}             逐 chunk 文本
      {"type":"error","message":"..."}
      {"type":"done"}
    """
    # 1. 加载 K 线
    df = _load_kline(repo, symbol)
    if df.is_empty():
        yield json.dumps({
            "type": "error",
            "message": f"标的 {symbol} 暂无日 K 数据,请先同步",
        }, ensure_ascii=False)
        return

    # 2. 无有效收盘价时不能构建操作计划,避免模型补造当前价。
    close_value = df.tail(1)["close"][0] if "close" in df.columns else None
    close = float(close_value) if close_value is not None else None
    if not _positive_price(close):
        yield json.dumps({
            "type": "error", "message": f"标的 {symbol} 最新日 K 缺少有效收盘价,请先同步",
        }, ensure_ascii=False)
        return
    levels = compute_levels(df)

    # 3. 财务(辅助)。ETF/指数无公司报表, 跳过扫描, 由提示词走对应常态文案。
    asset_type = repo.resolve_asset_type(symbol)
    fins = {} if asset_type in {"etf", "index"} else _load_financials(data_dir, symbol)

    # 4. meta
    yield json.dumps({
        "type": "meta",
        "symbol": symbol,
        "summary": summarize_levels(levels, close),
        "levels": levels,
        "close": close,
    }, ensure_ascii=False)

    # 5+6. 构建提示词 + 流式调用 LLM(整体 try-except,任何异常都 yield error,避免前端卡死)
    try:
        from app.services.ai_provider import stream_ai_text

        # 虚拟持仓上下文 (V3): 逐账户 (V2 多账户) 收集持有该标的的持仓注入
        # (任何异常静默跳过, 不影响分析)。
        paper_positions: list[dict] = []
        try:
            from app.strategy import paper as paper_trading
            for acc_id in paper_trading.list_account_ids(data_dir):
                pos = paper_trading.load_positions(data_dir, account_id=acc_id).get(symbol)
                if pos and (pos.get("qty") or 0) > 0:
                    paper_positions.append({"account_id": acc_id, **pos})
        except Exception as e:  # noqa: BLE001
            logger.warning("读取模拟盘持仓失败 (跳过注入): %s", e)
        # 浮盈口径用 raw: 持仓成本不复权, 与前复权 close 混算会在除权后失真
        raw_value = df.tail(1)["raw_close"][0] if "raw_close" in df.columns else None
        raw_close = float(raw_value) if raw_value is not None else None

        kline_tail = _clean_rows(df, _KLINE_KEEP_COLS)
        user_prompt = _build_user_prompt(kline_tail, fins, levels, close, symbol, focus,
                                         asset_type=asset_type, paper_positions=paper_positions,
                                         raw_close=raw_close,
                                         name=repo.get_name_map([symbol]).get(symbol, ""))
        got_content = False
        async for delta in stream_ai_text(
            [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.5,
            # 不限制输出: 推理模型(deepseek reasoner 系)思考 token 计入 max_tokens
            # 预算, 固定上限会把正文挤光(实测 4500 全被推理吃掉 → 正文 0 字)。
            max_tokens=None,
            prefer_final_answer=True,
        ):
            got_content = True
            yield json.dumps({"type": "delta", "content": delta}, ensure_ascii=False)

    except Exception as e:
        logger.exception("AI stock analysis failed for %s: %s", symbol, e)
        yield json.dumps({"type": "error", "message": f"AI 分析失败: {e}"}, ensure_ascii=False)
        return

    if not got_content:
        # 流正常结束但一个正文块都没有(典型: 输出上限被思考吃光后静默截断)
        logger.warning("AI stock analysis ended with empty content for %s", symbol)
        yield json.dumps({"type": "error", "message": "AI 未返回正文(输出被截断), 请重试"}, ensure_ascii=False)
        return
    yield json.dumps({"type": "done"}, ensure_ascii=False)
