"""实时指数核心清单 — 产品级固定契约 (单一权威)。

指数展示层 (侧栏指数条 / 市场总览) 固定四只核心指数, 不开放配置:
- 数据源边界: TickFlow 与 fuyao 指数快照双源均完整覆盖, 无降级分歧;
- 后端消费方 (quote_service / overview / sector_monitor) 与前端 Layout
  统一引用此处, 不得各自维护副本。

监控规则的指数标的不受此限 — quote_service 会把启用规则的指数并入显式拉取。
"""

CORE_INDEX_NAMES: dict[str, str] = {
    "000001.SH": "上证指数",
    "399001.SZ": "深证成指",
    "399006.SZ": "创业板指",
    "000680.SH": "科创综指",
}

CORE_INDEX_SYMBOLS: tuple[str, ...] = tuple(CORE_INDEX_NAMES.keys())

# 指数页可选标的。此目录只用于展示，不订阅行情，也不扩大侧栏/监控的核心清单。
_PAGE_ENTRIES = (
    *((symbol, name, "CN") for symbol, name in CORE_INDEX_NAMES.items()),
    ("000688.SH", "科创50", "CN"),
    ("000016.SH", "上证50", "CN"),
    ("000903.SH", "中证A100", "CN"),
    ("000905.SH", "中证500", "CN"),
    ("000852.SH", "中证1000", "CN"),
    ("^DJI", "道琼斯工业平均指数", "US"),
    ("^GSPC", "标普500", "US"),
    ("^IXIC", "纳斯达克综合指数", "US"),
    ("^N225", "日经225", "JP"),
    ("^KS11", "韩国KOSPI", "KR"),
)

INDEX_PAGE_BY_SYMBOL = {
    symbol: {"symbol": symbol, "name": name, "market": market, "asset_type": "index"}
    for symbol, name, market in _PAGE_ENTRIES
}
THS_ALL_A_SYMBOL = "883421.TI"
INDEX_PAGE_BY_SYMBOL[THS_ALL_A_SYMBOL] = {
    "symbol": THS_ALL_A_SYMBOL, "name": "同花顺全A（沪深）",
    "market": "CN", "asset_type": "index", "capability": "ths_index",
}
OVERSEAS_INDEX_SYMBOLS = frozenset(symbol for symbol, _, market in _PAGE_ENTRIES if market != "CN")
