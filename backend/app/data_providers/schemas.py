"""Internal provider schema column lists."""
from __future__ import annotations

import polars as pl

DAILY_COLUMNS = [
    "symbol", "asset_type", "source", "date", "open", "high", "low", "close",
    "volume", "amount", "pre_close", "change_pct",
]

ADJ_FACTOR_COLUMNS = ["symbol", "asset_type", "source", "trade_date", "ex_factor"]

INSTRUMENT_COLUMNS = [
    "symbol", "name", "exchange", "asset_type", "source", "listing_date", "status",
]

MINUTE_COLUMNS = [
    "symbol", "asset_type", "source", "datetime", "open", "high", "low", "close",
    "volume", "amount", "freq",
]

# ---- 交易所参考数据 (盘后同步落盘; 一个交易日一张表, 表存在即该日完整) ----
# 代码带交易所后缀; 价格为原价 (元)。

PRICE_LIMIT_SCHEMA = {
    "date": pl.Date, "symbol": pl.String,
    # 交易所生效涨跌停价 (原价); 当日无涨跌幅限制 (新股上市初期等) 时为 null 且 no_limit=True
    "limit_up": pl.Float64, "limit_down": pl.Float64, "no_limit": pl.Boolean,
    "source": pl.String,
}

# 只列当日处于风险警示 (ST/*ST 等) 的股票; 不在表内 = 当日非风险警示
ST_STATUS_SCHEMA = {
    "date": pl.Date, "symbol": pl.String, "name": pl.String, "st_type": pl.String, "source": pl.String,
}

# 只列当日停牌 (含部分时段停牌) 的股票; timing 为空 = 全天停牌, 有值 = 停牌时段
SUSPENSION_SCHEMA = {
    "date": pl.Date, "symbol": pl.String, "timing": pl.String, "source": pl.String,
}

# 集合竞价 (09:25 定版): 竞价成交价 (原价, 元)、竞价涨幅 (小数制)、竞价成交额 (元)、成交量与
# 竞价结束时未匹配量 (股, 上游 um_vol, 口径未证实, 实测远小于涨停封单, 不用于推算)。只列有竞价成交的股票
AUCTION_SCHEMA = {
    "date": pl.Date, "symbol": pl.String, "price": pl.Float64, "change_pct": pl.Float64,
    "amount": pl.Float64, "volume": pl.Float64, "unmatched_volume": pl.Float64, "source": pl.String,
}

LIMIT_EVENT_SCHEMA = {
    "date": pl.Date, "symbol": pl.String, "name": pl.String,
    # u=涨停 d=跌停 ub=涨停炸板 db=跌停炸板; 为收盘集合竞价前的状态, 尾盘回封以收盘价复核
    "type": pl.String, "is_break": pl.Boolean,
    "limit_times": pl.Int64, "pre_limit_times": pl.Int64,
    "first_time": pl.String, "last_time": pl.String,  # 北京时间 HH:MM:SS
    "open_times": pl.Int64,
    "fd_amount": pl.Float64,  # 封单额, 元
    "limit_detail": pl.String,  # 封板/开板时间线 JSON
    "close": pl.Float64, "change_pct": pl.Float64,  # 小数制
    "amount": pl.Float64,  # 元
    "source": pl.String,
}
