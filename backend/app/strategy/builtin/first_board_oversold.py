"""首板oversold日线观察池, 实际盘中提示由首板工作台评估。"""
from app.strategy.first_board import filter_pattern_history, pattern_strategy_meta

META = pattern_strategy_meta("oversold")
EXECUTION_BACKEND = "python_history_legacy"
# 参数允许最多250个完成交易日, 并额外读取当前行和涨停前收盘分母。
LOOKBACK_DAYS = 252
BASIC_FILTER = {"enabled": False}
ENTRY_SIGNALS = []
EXIT_SIGNALS = []
MAX_HOLD_DAYS = 1


def filter_history(df, params):
    return filter_pattern_history(df, params, "oversold")
