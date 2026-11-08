"""首板超跌日线观察池研究：已收盘信号，供下一交易日开盘回测。"""

from app.strategy.market_data import filter_first_board_daily_history

META = {
    "id": "custom_first_board_oversold_daily",
    "name": "首板超跌 · 日线观察池研究",
    "description": "收盘确认超跌观察资格，供次日开盘回测；不含盘中涨幅、临板及买入确认。参数独立于首板工作台。",
    "tags": ["首板", "超跌", "日线研究"],
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
        {"id": "oversold_window", "label": "超跌高点窗口（交易日）",
         "type": "int", "default": 60, "min": 20, "max": 120, "step": 1},
        {"id": "oversold_min_drawdown", "label": "距窗口高点跌幅下限（小数，0.25 = 25%）",
         "type": "float", "default": 0.25, "min": 0.01, "max": 0.99, "step": 0.01},
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
3. 1 - T日收盘价 / 超跌窗口最高价达到跌幅下限，默认60日、25%。
4. 这里只确认超跌观察资格，不要求T+1盘中涨幅达到5%、突破近5日高点或临近涨停。
5. 不包含盘中板块、市场环境、昨日首板反馈、优先分及账户额度门控。
6. 无默认止损、持有期限或退出信号；须另设一致的退出、成本及成交假设后比较。
7. 保存或优化参数仅影响本研究策略，不会改写首板工作台规则。
"""


def filter_history(df, params):
    return filter_first_board_daily_history(df, params, "oversold")
