"""回春 A0：与回春模式共用形态计算，独立保存选股参数。"""

from app.strategy.market_data import filter_huichun_a0

META = {
    "id": "custom_huichun_a0",
    "name": "回春 A0",
    "description": "前轮上涨后，零轴附近首次再金叉，收盘站上上行均线；沿用回春模式数据口径。",
    "tags": ["回春", "MACD", "日线"],
    "asset_types": ["stock"],
    "timeframes": ["1d"],
    "realtime_supported": False,
    # 原回春按形态筛选，不额外叠加通用价格、市值、当前 ST 名称过滤。
    "basic_filter": {"enabled": False},
    "params": [
        {"id": "rally_threshold", "label": "前段最低涨幅（小数，0.4 = 40%）",
         "type": "float", "default": 0.4, "min": 0, "max": 10, "step": 0.05},
        {"id": "zero_threshold", "label": "最大零轴距离（小数，0.02 = 2%）",
         "type": "float", "default": 0.02, "min": 0, "max": 1, "step": 0.005},
        {"id": "ma_window", "label": "趋势均线周期（交易日）",
         "type": "int", "default": 60, "min": 2, "max": 500, "step": 1},
        {"id": "slope_lag", "label": "均线比较间隔（交易日）",
         "type": "int", "default": 5, "min": 1, "max": 250, "step": 1},
        {"id": "warmup_bars", "label": "前轮金叉前预热日线数",
         "type": "int", "default": 250, "min": 1, "max": 2000, "step": 1},
    ],
    "scoring": {},
    "order_by": "symbol",
    "descending": False,
    "limit": 0,
    "required_features": ["close"],
}

EXECUTION_BACKEND = "python_history_legacy"
# 调用契约需要历史帧；A0 的预热由共享读取器加载完整历史，不受此窗口截断。
LOOKBACK_DAYS = 2
ENTRY_SIGNALS = []
EXIT_SIGNALS = []
STOP_LOSS = None
MAX_HOLD_DAYS = None

RULES = """
1. MACD 固定为 10/20/9，前轮金叉 G 前须有足够连续有效日线预热。
2. G 至随后死叉 D 的复权收盘涨幅达到设定门槛，默认 40%。
3. D 后首次金叉 R 的 max(abs(DIF), abs(DEA))/收盘价不超过门槛，默认 2%。
4. R 收盘高于趋势均线，且该均线高于设定间隔前的均线，默认 60 日与 5 日。
5. 只认已收盘形态，复用回春的交易日历、缺口分段与复权异常隔离；不包含待金叉观察池。
6. 历史交易资格仍待核验，筛选结果不代表可成交信号；回测退出与成本需另行设置。
"""


def filter_history(df, params):
    return filter_huichun_a0(df, params)
