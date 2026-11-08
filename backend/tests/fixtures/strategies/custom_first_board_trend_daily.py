"""首板趋势日线观察池研究：已收盘信号，供下一交易日开盘回测。"""

from app.strategy.market_data import filter_first_board_daily_history

META = {
    "id": "custom_first_board_trend_daily",
    "name": "首板趋势 · 日线观察池研究",
    "description": "收盘确认趋势观察资格，供次日开盘回测；不含盘中涨幅、临板及买入确认。参数独立于首板工作台。",
    "tags": ["首板", "趋势", "日线研究"],
    "asset_types": ["stock"],
    "timeframes": ["1d"],
    "realtime_supported": False,
    # 市场范围、风险警示与历史资格由共享首板规则统一处理。
    "basic_filter": {"enabled": False},
    "params": [
        {"id": "universe", "label": "市场范围（hs_a=沪深非ST；main_board=沪深主板非ST）",
         "type": "select", "default": "hs_a_non_st",
         "options": ["hs_a_non_st", "main_board_non_st"]},
        {"id": "lookback_days", "label": "近N个交易日无收盘涨停（含信号日）",
         "type": "int", "default": 35, "min": 1, "max": 60, "step": 1},
        {"id": "min_history_days", "label": "最少有效历史（交易日）",
         "type": "int", "default": 60, "min": 20, "max": 250, "step": 1},
        {"id": "trend_window", "label": "趋势窗口（交易日）",
         "type": "int", "default": 40, "min": 10, "max": 60, "step": 1},
        {"id": "trend_min_return", "label": "窗口涨幅下限（小数，0.03 = 3%）",
         "type": "float", "default": 0.03, "min": 0, "max": 1, "step": 0.01},
        {"id": "trend_max_return", "label": "窗口涨幅上限（小数，0.40 = 40%）",
         "type": "float", "default": 0.40, "min": 0.01, "max": 2, "step": 0.01},
    ],
    "scoring": {},
    "order_by": "symbol",
    "descending": False,
    "limit": 0,
    "required_features": ["open", "high", "low", "close", "raw_close", "limit_up_price"],
}

EXECUTION_BACKEND = "python_history_legacy"
# 覆盖可调参数的最大历史要求，并额外提供涨停判定的前收盘参考行。
LOOKBACK_DAYS = 251
ENTRY_SIGNALS = []
EXIT_SIGNALS = []
STOP_LOSS = None
MAX_HOLD_DAYS = None

RULES = """
1. 仅研究已完成日线：T日收盘确认的观察资格用于T+1日；回测请设置次日开盘入场。
2. 复用首板市场范围、非ST、有效连续历史及近N日无收盘涨停规则；窗口包含T日。
3. 窗口末日收盘价 / 窗口首日收盘价 - 1 位于涨幅上下限之间，含两端，默认40日、3%至40%。
4. T日收盘价须严格高于同一趋势窗口的收盘均价；趋势计算使用一致的复权价格。
5. 这里只确认趋势观察资格，不要求T+1盘中涨幅达到5%、突破近5日高点或临近涨停。
6. 不包含盘中板块、市场环境、昨日首板反馈、优先分及账户额度门控。
7. 无默认止损、持有期限或退出信号；须另设一致的退出、成本及成交假设后比较。
8. 保存或优化参数仅影响本研究策略，不会改写首板工作台规则。
"""


def filter_history(df, params):
    return filter_first_board_daily_history(df, params, "trend")
