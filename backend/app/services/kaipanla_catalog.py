"""开盘啦匿名接口目录与边界解析;不包含账号、登录接口或行情源替换。

供应商百分数保持百分数,金额只使用文档注明的口径。目录只能证明文档
能力,实际可用性由请求结果判断。法定节假日属于参考数据,不是交易日历。
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any
from urllib.parse import urlencode

from app.services.ext_data import ExtConfig, ExtField, PullConfig
from app.services.index_const import CORE_INDEX_SYMBOLS


@dataclass(frozen=True)
class KplDataset:
    id: str
    label: str
    category: str
    action: str
    controller: str
    method: str = "GET"
    host: str = "apphis"
    current_host: str | None = None
    current_controller: str | None = None
    date_param: str | None = None
    date_format: str = "iso"
    params: dict = field(default_factory=dict)
    required_params: tuple[str, ...] = ()
    response_path: str = "info"
    row_kind: str = "arrays"
    columns: tuple[str, ...] = ()
    pageable: bool = False
    schedule_minutes: int = 5
    historical: bool = True


def _spec(name: str, label: str, category: str, action: str, controller: str,
          columns: str, **kwargs) -> KplDataset:
    return KplDataset(
        id=f"ext_kpl_{name}", label=label, category=category, action=action,
        controller=controller, columns=tuple(columns.split()), **kwargs,
    )


_LIMIT_REASONS = (
    "StockID Name reserved2 reserved3 reserved4 reserved5 limit_up_ts reserved7 "
    "auction_turnover board_state board_number concepts main_buy main_sell change_pct "
    "turnover reason detail reserved18"
)
_RANK_COLUMNS = (
    "plate_id plate_name strength change_pct speed_pct turnover main_net main_buy main_sell "
    "volume_ratio market_cap range_pct large_order_net total_market_cap institutional_add "
    "pe_previous pe_current"
)
_CORE_REQUEST_IDS = ",".join(
    f"{symbol.split('.')[1]}{symbol.split('.')[0]}" for symbol in CORE_INDEX_SYMBOLS
)

# 43 类功能中排除三个明确登录功能,以及认证列含义不明确的竞价列表。
# 不推断文档未声明的当前域名/controller。需要标的/题材/区间参数的功能仅按需请求。
_DATASETS = (
    _spec("emotion", "市场情绪", "市场概览", "ChangeStatistics", "HisHomeDingPan",
          "record_id strong ztjs lbgd df_num", date_param="Date", row_kind="objects",
          params={"Index": "0", "st": "1000"}),
    _spec("capacity", "市场量能", "市场概览", "MarketCapacity", "HisHomeDingPan",
          "record_id last_wan s_zrcs_wan s_zrtj_wan s3_zrtj_wan yclnstr color time",
          date_param="Date", params={"Type": "0"}, row_kind="object"),
    _spec("indices", "核心指数快照", "市场概览", "GetZsReal", "StockL2History",
          "StockID prod_name last_px increase_rate_pct increase_amount turnover",
          method="POST", date_param="Day", current_host="apphwhq",
          current_controller="UserSelectStock", response_path="StockList", row_kind="objects",
          params={"StockIDList": _CORE_REQUEST_IDS}, schedule_minutes=1),
    _spec("limit_counts", "涨跌停总数", "涨停分析", "MarketStockZDNum", "HisHomeDingPan",
          "record_id SJZT SJDT", method="POST", date_param="Date", row_kind="object"),
    _spec("limit_ladder_counts", "连板数量统计", "涨停分析", "DailyLimitIndex", "HisHomeDingPan",
          "record_id first_board_count second_board_count third_board_count fourth_board_count fifth_plus_count",
          date_param="Day", current_host="apphwhq", current_controller="HomeDingPan",
          row_kind="array", schedule_minutes=1),
    _spec("limit_performance", "涨停表现", "涨停分析", "ZhangTingExpression", "HisHomeDingPan",
          "record_id limit_up_count two_board_count three_board_count max_board_count "
          "two_board_promotion_pct three_board_promotion_pct max_board_promotion_pct broken_rate_pct "
          "yesterday_limit_up_pct yesterday_continuous_pct yesterday_broken_pct summary",
          date_param="Day", row_kind="array"),
    _spec("limit_ladder", "涨停梯队", "涨停分析", "GetZhangTingTianTi", "FuPanLa",
          "StockID Name continuous_boards limit_up_ts ZSCode ZSName reserved6 reserved7 "
          "sector_limit_up_count turnover sector_turnover ZhuShuList_json", method="POST", date_param="Date",
          current_host="apphwhq", response_path="StockList", schedule_minutes=1),
    _spec("limit_up", "涨停板列表", "涨停分析", "DailyLimitPerformance", "HisHomeDingPan",
          "StockID Name reserved2 reserved3 limit_up_ts reason seal_amount max_seal_amount "
          "main_net main_buy main_sell turnover concepts float_market_cap turnover_pct reserved15 "
          "resealed amplitude_pct board_tag ZSCode reserved20 price change_pct",
          date_param="Day", current_host="apphwhq", current_controller="HomeDingPan",
          params={"PidType": "1", "Type": "4", "Order": "0", "st": "100", "Index": "0"},
          pageable=True, schedule_minutes=1),
    _spec("broken_limits", "破板个股", "涨停分析", "DailyLimitPerformance2", "HisHomeDingPan",
          "StockID Name reserved2 reserved3 price change_pct concepts main_net main_buy main_sell "
          "turnover float_market_cap turnover_pct reserved13 amplitude_pct reserved15 reserved16 max_seal_amount",
          date_param="Day", current_host="apphwhq", current_controller="HomeDingPan",
          params={"PidType": "1", "Type": "5", "Order": "1", "st": "100", "Index": "0"},
          pageable=True, schedule_minutes=1),
    _spec("limit_reasons", "涨停原因", "涨停分析", "GetPlateInfo_w38", "HisLimitResumption",
          f"record_id {_LIMIT_REASONS} ZSCode ZSName nums_json", method="POST", date_param="Date",
          response_path="list", row_kind="groups", params={"Index": "0", "st": "20"}, pageable=True),
    _spec("highlights", "盘面亮点", "涨停分析", "GetPMSL_PMLD", "FuPanLa",
          "record_id TimeMin TagID ZSCode ZSName TagShuXing TagName Detail StockList_json",
          date_param="Date", current_host="apphwhq", response_path="List", row_kind="objects",
          params={"Index": "0", "st": "20"}, pageable=True),
    _spec("withdrawal", "大幅回撤", "涨停分析", "SharpWithdrawal", "HisHomeDingPan",
          "StockID Name change_pct withdrawal_pct price", date_param="Day"),
    _spec("plate_history", "板块涨停历史", "板块数据", "GetDatePlate", "HisLimitResumption",
          f"record_id {_LIMIT_REASONS} ZSCode ZSName trade_date TCExplain", method="POST",
          required_params=("ZSCode",), response_path="list", row_kind="groups",
          params={"Index": "0", "st": "3"}, pageable=True),
    _spec("sector_strength", "板块强度", "板块数据", "RealRankingInfo", "ZhiShuRanking",
          _RANK_COLUMNS, method="POST", date_param="Date", current_host="apphwhq", response_path="list",
          params={"Type": "1", "ZSType": "7", "Order": "1", "Index": "0", "st": "100"},
          pageable=True, schedule_minutes=1),
    _spec("industry_rank", "行业涨幅", "板块数据", "RealRankingInfo", "ZhiShuRanking",
          _RANK_COLUMNS, method="POST", date_param="Date", current_host="apphwhq", response_path="list",
          params={"Type": "2", "ZSType": "4", "Order": "1", "Index": "0", "st": "100"},
          pageable=True, schedule_minutes=1),
    _spec("region_rank", "地区涨幅", "板块数据", "RealRankingInfo", "ZhiShuRanking",
          _RANK_COLUMNS, method="POST", date_param="Date", current_host="apphwhq", response_path="list",
          params={"Type": "2", "ZSType": "6", "Order": "1", "Index": "0", "st": "100"}, pageable=True),
    _spec("weight", "权重表现", "板块数据", "WeightPerformance", "HisHomeDingPan",
          "record_id plate_id plate_name change_pct leader_id leader_name leader_pct direction",
          date_param="Day", row_kind="groups"),
    _spec("lhb_flows", "龙虎榜动向", "龙虎榜", "GetYTFP_LHBDX", "FuPanLa",
          "record_id BID BName direction StockID Name money_raw Three", date_param="Date",
          current_host="apphwhq", response_path="List", row_kind="groups", schedule_minutes=30),
    _spec("lhb_list", "龙虎榜列表", "龙虎榜", "GetStockList", "LongHuBang",
          "record_id ID Name IncreaseAmount_pct D3 BuyIn JoinNum Turnover CircPrice Amplitude_pct "
          "TurnoverRatio_pct Capitalization", method="POST", host="applhb", date_param="Time",
          response_path="list", row_kind="objects", params={"Index": "0", "st": "100"},
          pageable=True, schedule_minutes=30),
    _spec("lhb_details", "龙虎榜详细信息", "龙虎榜", "GetNewOneStockInfo", "Stock",
          "ID Name CurPrice QuoteChange_pct TurnoverRatio_pct Circulation_yi BuyIn Turnover "
          "List_json OnTimeList_json lbnum", method="POST", host="applhb", date_param="Time",
          required_params=("StockID",), params={"Type": "0"}, response_path="", row_kind="object",
          schedule_minutes=30),
    _spec("hot_money_flows", "游资动向", "龙虎榜", "YouZiDongXiangByList", "Index",
          "record_id investor_id investor_name StockID Name Money D3 IncreaseAmount_pct GInfo_json Slist_json",
          method="POST", host="applhb", date_param="Time", response_path="DongXiang",
          row_kind="groups", schedule_minutes=30),
    _spec("hot_money_seats", "游资席位信息", "龙虎榜", "GroupInfo", "BusinessGroup",
          "record_id GID ShortName Info ID Name", method="POST", host="applhb", required_params=("GID",),
          response_path="BusinessList", row_kind="groups", historical=False, schedule_minutes=1440),
    _spec("auction_summary", "竞价总体信息", "竞价数据", "MorningBidding", "HisHomeDingPan",
          "record_id tJJJE lJJJE ycln lln tSZ tXD lSZ lXD", date_param="Date", row_kind="object"),
    _spec("auction_counts", "竞价数量统计", "竞价数据", "MorningBiddingNum", "HisHomeDingPan",
          "record_id limit_buy_count matched_large_count popular_count large_net_count auction_sell_count",
          date_param="Date", row_kind="array"),
    _spec("tail_bidding", "尾盘抢筹", "竞价数据", "GetWPQC", "StockBidYiDong",
          "StockID Name main_type margin_flag concepts change_pct turnover float_market_cap main_buy main_sell "
          "main_net grab_amount matched_amount reserved13 reserved14 grab_pct grab_ratio",
          date_param="Day", date_format="compact", current_host="apphwhq", response_path="List",
          params={"Type": "1", "Order": "1", "Index": "0", "st": "100"}, pageable=True),
    _spec("stock_plates", "股票所属板块", "股票数据", "GetStockIDPlate", "StockL2Data",
          "plate_id plate_name change_pct", method="POST", host="apphwshhq", required_params=("StockID",),
          response_path="List", params={"Type": "1"}, historical=False, schedule_minutes=1440),
    _spec("stock_plates_v2", "股票所属板块及龙头", "股票数据", "GetFeaturedSection", "StockL2Data",
          "plate_id plate_name change_pct leader_id leader_name leader_pct reserved6", host="apphwshhq",
          required_params=("StockID",), historical=False, schedule_minutes=1440),
    _spec("new_high", "百日新高", "股票数据", "GroupStock_W28", "StockNewHigh",
          "record_id StockID Name price change_pct concepts turnover main_net main_buy main_sell float_market_cap "
          "market_cap is_new ZSCode ZSName turnover_pct GroupID GroupName", method="POST", date_param="Date",
          current_host="apphwhq", response_path="GroupList", row_kind="groups",
          params={"Type": "0_0_0_0_0", "IsAll": "0", "Index": "0", "st": "20"}, pageable=True),
    _spec("range_plates", "板块区间统计", "股票数据", "GetInterviewsByDateZS", "StockLineData",
          "plate_id plate_name range_pct main_buy main_sell main_net turnover float_market_cap inflow_days "
          "reserved9 reserved10 range_strength", method="POST", current_host="apphwhq", response_path="List",
          required_params=("DStart", "DEnd"), params={"Type": "9", "Order": "1", "Index": "0", "st": "100"}, pageable=True),
    _spec("range_stocks", "股票区间统计", "股票数据", "GetInterviewsByDateStock", "StockLineData",
          "StockID Name price range_pct main_buy main_sell main_net turnover_pct turnover float_market_cap "
          "concepts margin_flag main_type inflow_days reserved14 reserved15", method="POST", current_host="apphwhq",
          response_path="List", required_params=("DStart", "DEnd"),
          params={"Type": "2", "FilterBJS": "0", "Order": "1", "Index": "0", "st": "1000"}, pageable=True),
    _spec("review_list", "复盘榜", "股票数据", "GetRQZ_Data", "Index", "StockID", method="POST",
          host="apphwshhq", response_path="List", row_kind="scalars", historical=False, schedule_minutes=30),
    _spec("stock_hotspots", "股票风口明细", "风口概念", "GetFengKList", "StockFengKData",
          "StockID Name reserved2 change_pct turnover main_buy main_sell main_net hotspots reserved9 "
          "main_type concepts event_ts", method="POST", date_param="Day", date_format="compact",
          current_host="apphwhq", response_path="List",
          params={"Time": "1500", "Order": "17", "Index": "0", "st": "100"}, pageable=True),
    _spec("concept_hotspots", "概念风口", "风口概念", "GetFengKYDPlate", "StockFengKData",
          "concept_name strength", method="POST", date_param="Day", date_format="compact", response_path="List"),
    _spec("theme_details", "题材详情", "题材数据", "InfoGet", "Theme",
          "ID Name BriefIntro ClassLayer Desc PlateSwitch StkSwitch Introduction CreateTime UpdateTime "
          "Table_json Stocks_json StockList_json IsNew ZT_json Power Subscribe IsGood ComNum GoodNum",
          method="POST", host="applhb", required_params=("ID",), response_path="", row_kind="object",
          historical=False, schedule_minutes=1440),
    _spec("theme_search", "题材库搜索", "题材数据", "InfoSearch", "Theme",
          "record_id ID Name Desc CreateTime kind LName_json LID_json LIDNameMap_json",
          method="POST", host="applhb", required_params=("key",), response_path="List",
          row_kind="groups", historical=False, schedule_minutes=1440),
    _spec("live", "大盘直播", "直播数据", "ZhiBoContent", "HisConceptionPoint",
          "ID UID Time Comment Type ShareData_json UserName Image Stock_json DisStock_json",
          method="POST", date_param="Date", current_host="apphwhq", current_controller="ConceptionPoint",
          response_path="List", row_kind="objects", schedule_minutes=1),
    _spec("new_high_trend", "历史新高趋势", "新高趋势", "GetDayNewHigh_W28", "StockNewHigh",
          "trend_date new_high_total new_high_count reserved", method="POST", response_path="x",
          row_kind="scalars", params={"GroupID": "ALL"}, schedule_minutes=1440),
    _spec("holidays", "法定节假日参考", "节假日", "GetHoliday", "YiDongKanPan",
          "holiday_date", method="POST", response_path="List", row_kind="scalars", schedule_minutes=1440),
    _spec("news", "最新消息", "最新消息", "AppNews", "UserInfo",
          "ID Time Content URL Type StockID StockName StockStr Param0", method="POST", host="applhb",
          response_path="List", row_kind="objects", params={"Index": "0", "st": "20"},
          historical=False, schedule_minutes=5),
)


def datasets() -> tuple[KplDataset, ...]:
    return _DATASETS


def get_dataset(dataset_id: str) -> KplDataset | None:
    return next((spec for spec in _DATASETS if spec.id == dataset_id), None)


_LABELS = {
    "record_id": "记录编号", "strong": "情绪强度", "ztjs": "涨停家数", "lbgd": "连板高度",
    "df_num": "大幅回撤家数", "last_wan": "最新量能(万)", "s_zrcs_wan": "昨日量能(万)",
    "s_zrtj_wan": "昨日统计量能(万)", "s3_zrtj_wan": "三日量能(万)", "yclnstr": "预测成交额",
    "SJZT": "涨停家数", "SJDT": "跌停家数", "StockID": "股票代码", "Name": "名称",
    "ID": "业务编号", "plate_id": "板块代码", "plate_name": "板块名称", "ZSCode": "板块代码",
    "ZSName": "板块名称", "GroupID": "分组代码", "GroupName": "分组名称", "prod_name": "指数名称",
    "last_px": "指数点位", "increase_rate_pct": "指数涨跌幅(%)", "increase_amount": "涨跌点数",
    "continuous_boards": "连板数", "limit_up_ts": "涨停时间", "sector_limit_up_count": "板块涨停数",
    "sector_turnover": "板块成交额(元)", "first_board_count": "首板数量", "second_board_count": "二板数量",
    "third_board_count": "三板数量", "fourth_board_count": "四板数量", "fifth_plus_count": "五板及以上数量",
    "limit_up_count": "家数原值(口径待核)", "two_board_count": "二连板家数", "three_board_count": "三连板家数",
    "max_board_count": "板数原值(口径待核)", "two_board_promotion_pct": "二板晋级率(%)",
    "three_board_promotion_pct": "三板晋级率(%)", "max_board_promotion_pct": "最高板晋级率(%)",
    "broken_rate_pct": "破板率(%)", "yesterday_limit_up_pct": "昨日涨停表现(%)",
    "yesterday_continuous_pct": "昨日连板表现(%)", "yesterday_broken_pct": "昨日破板表现(%)",
    "summary": "盘面总结", "price": "价格", "change_pct": "涨跌幅(%)", "speed_pct": "涨速(%)",
    "turnover_pct": "换手率(%)", "amplitude_pct": "振幅(%)", "withdrawal_pct": "回撤幅度(%)",
    "reason": "涨停原因", "detail": "详细说明", "concepts": "所属概念", "strength": "强度",
    "main_net": "主力净额(元)", "main_buy": "主力买入(元)", "main_sell": "主力卖出(元)",
    "turnover": "成交额(元)", "float_market_cap": "流通市值(元)", "market_cap": "总市值(元)",
    "total_market_cap": "总市值(元)", "seal_amount": "封单额(元)", "max_seal_amount": "最大封单额(元)",
    "volume_ratio": "量比", "range_pct": "区间涨幅(%)", "large_order_net": "三千万大单净额(元)",
    "institutional_add": "机构增仓", "pe_previous": "上年度市盈率", "pe_current": "本年度市盈率",
    "board_state": "连板状态", "board_number": "板数", "board_tag": "连板标签", "resealed": "是否回封",
    "money_raw": "游资金额(原始值,单位待核验)", "BName": "游资名称", "BID": "游资编号",
    "direction": "交易方向", "Three": "是否三日榜", "investor_id": "游资编号", "investor_name": "游资名称",
    "tJJJE": "今日竞价金额", "lJJJE": "昨日竞价金额", "ycln": "今日预测成交", "lln": "昨日预测成交",
    "tSZ": "今日上涨家数", "tXD": "今日下跌家数", "lSZ": "昨日上涨家数", "lXD": "昨日下跌家数",
    "Time": "发布时间", "time": "来源时间", "TimeMin": "事件时间", "event_ts": "事件时间",
    "Comment": "盘面解读", "Content": "消息内容", "UserName": "作者", "URL": "消息链接",
    "Detail": "亮点说明", "TagName": "亮点类型", "TagID": "标签编号", "TagShuXing": "标签属性",
    "concept_name": "概念名称", "hotspots": "风口概念", "trade_date": "业务日期", "trend_date": "趋势日期",
    "holiday_date": "法定节假日", "new_high_total": "新高总数", "new_high_count": "当日新高数",
    "date": "查询归属日期", "source": "数据来源", "fetched_at": "获取时间", "extra_json": "额外字段",
    "auction_turnover": "涨停时成交额(元)", "color": "展示颜色编号", "D3": "是否三日榜",
    "IncreaseAmount_pct": "涨跌幅(%)", "BuyIn": "净买入额(元)", "JoinNum": "关联营业部数量",
    "Turnover": "成交额(元)", "CircPrice": "流通市值(元)", "Amplitude_pct": "振幅(%)",
    "TurnoverRatio_pct": "换手率(%)", "Capitalization": "总市值(元)", "CurPrice": "价格",
    "QuoteChange_pct": "涨跌幅(%)", "Circulation_yi": "流通市值(亿)", "lbnum": "连续上榜次数",
    "Money": "游资金额(原始值)", "GID": "游资组编号", "ShortName": "游资简称", "Info": "游资简介",
    "limit_buy_count": "涨停委买数量", "matched_large_count": "大额撮合数量", "popular_count": "热门股数量",
    "large_net_count": "大额净买入数量", "auction_sell_count": "竞价砸盘数量", "main_type": "主力类型",
    "margin_flag": "是否融资融券", "grab_amount": "抢筹金额(元)", "matched_amount": "撮合成交额(元)",
    "grab_pct": "抢筹幅度(%)", "grab_ratio": "抢筹占比(原始值)", "leader_id": "龙头股票代码",
    "leader_name": "龙头名称", "leader_pct": "龙头涨跌幅(%)", "is_new": "是否新增新高",
    "inflow_days": "净流入天数", "range_strength": "区间强度", "TCExplain": "板块说明",
    "BriefIntro": "题材简介", "ClassLayer": "分类层级", "Desc": "说明", "Introduction": "题材详情",
    "CreateTime": "创建时间", "UpdateTime": "更新时间", "Power": "力度", "Subscribe": "订阅数量",
    "IsNew": "是否新增", "IsGood": "是否精选", "ComNum": "评论数量", "GoodNum": "点赞数量",
    "PlateSwitch": "板块展示开关", "StkSwitch": "股票展示开关", "kind": "题材类型",
    "UID": "作者编号", "Type": "消息类型", "Image": "作者头像", "StockName": "关联股票名称",
    "StockStr": "关联股票代码", "Param0": "消息参数", "nums_json": "涨停统计详情",
    "ZhuShuList_json": "涨停板块详情",
}


def presets() -> list[ExtConfig]:
    configs = []
    for spec in _DATASETS:
        if spec.required_params:
            continue
        params = {"a": spec.action, "c": spec.controller, **spec.params}
        fields = [ExtField(column, "string", _LABELS.get(column, column)) for column in spec.columns]
        fields.extend(ExtField(column, "string", _LABELS[column]) for column in ("date", "source", "fetched_at", "extra_json"))
        configs.append(ExtConfig(
            id=spec.id, label=f"开盘啦·{spec.label}", mode="timeseries", fields=fields,
            market_level=True, description="匿名补充数据;更新时间以实际响应为准,不替换行情源。",
            pull=PullConfig(
                url=f"https://{spec.host}.longhuvip.com/w1/api/index.php?{urlencode(params)}",
                method=spec.method, schedule_minutes=spec.schedule_minutes, enabled=False,
                date_param=spec.date_param, date_format=spec.date_format,
            ),
        ))
    return configs


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _as_date(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    if re.fullmatch(r"\d{8}", value):
        return date(int(value[:4]), int(value[4:6]), int(value[6:]))
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:[T ].*)?", value):
        return date.fromisoformat(value[:10])
    return None


def _dates(obj: dict) -> list[date]:
    out = []
    for key in ("Date", "date", "Day", "day", "Time"):
        value = obj.get(key)
        for item in value if isinstance(value, list) else [value]:
            parsed = _as_date(item)
            if parsed:
                out.append(parsed)
            elif key != "Time" and item not in (None, ""):
                raise ValueError("开盘啦响应日期字段格式错误")
    return out


def _check_dates(values: list[date], target: date) -> date | None:
    if any(value != target for value in values):
        raise ValueError("开盘啦响应日期与请求日期不一致,已拒绝该数据")
    return target if values else None


def _check_seat_dates(value: Any, target: date) -> None:
    """核验交易席位日期,不把 OnTimeList 等历史日期清单误当成交日期。"""
    if isinstance(value, list):
        for item in value:
            _check_seat_dates(item, target)
    elif isinstance(value, dict):
        for key, item in value.items():
            if key in ("BuyList", "SellList"):
                if not isinstance(item, list) or any(not isinstance(seat, dict) for seat in item):
                    raise ValueError("龙虎榜席位结构错误")
                for seat in item:
                    _check_dates(_dates(seat), target)
            elif isinstance(item, (dict, list)):
                _check_seat_dates(item, target)


def _extract(payload: dict, path: str) -> Any:
    current: Any = payload
    for key in path.split(".") if path else []:
        if not isinstance(current, dict) or key not in current:
            raise ValueError(f"开盘啦响应结构缺少 {path}")
        current = current[key]
    return current


def _business_id(spec: KplDataset, row: dict) -> str:
    # 不以列表顺序/抓取时间为键,否则翻页和刷新会产生不同业务记录。
    keys = ("StockID", "ID", "ZSCode", "GroupID", "BID", "investor_id", "direction", "D3", "Three",
            "plate_id", "TimeMin", "TagID", "trade_date", "kind")
    identity = {key: row[key] for key in keys if row.get(key) is not None}
    if not identity:
        if spec.action in {"ChangeStatistics", "MarketCapacity", "MarketStockZDNum", "DailyLimitIndex",
                           "ZhangTingExpression", "MorningBidding", "MorningBiddingNum"}:
            return spec.id
        raise ValueError("开盘啦数据行缺少稳定业务键")
    return hashlib.sha256(_json(identity).encode("utf-8")).hexdigest()[:24]


def _finish(spec: KplDataset, row: dict) -> dict:
    if "record_id" in spec.columns:
        row["record_id"] = _business_id(spec, row)
    elif row.get(spec.columns[0]) in (None, ""):
        raise ValueError("开盘啦数据行缺少稳定业务键")
    # 将稳定业务键放首列,复用扩展表无 symbol 行的合并键约定。
    return {column: row.get(column) for column in spec.columns} | {
        key: value for key, value in row.items() if key not in spec.columns
    }


_ALIASES = {
    "last_wan": "last", "s_zrcs_wan": "s_zrcs", "s_zrtj_wan": "s_zrtj", "s3_zrtj_wan": "s3_zrtj",
}


def _object_row(spec: KplDataset, value: Any, target: date, *, check_date: bool = True) -> dict:
    if not isinstance(value, dict):
        raise ValueError("开盘啦数据行结构应为对象")
    if check_date:
        _check_dates(_dates(value), target)
    row = {}
    used = {"errcode", "Date", "date", "Day", "day"}
    for column in spec.columns:
        if column == "record_id":
            continue
        source = _ALIASES.get(column, column)
        if column.endswith("_json"):
            source = column[:-5]
        elif column.endswith("_pct") and source not in value:
            source = column[:-4]
        elif column.endswith("_yi") and source not in value:
            source = column[:-3]
        if source in value:
            used.add(source)
            raw = value[source]
            if isinstance(raw, (dict, list)):
                raw = _json(raw)
            if column.endswith("_pct") and isinstance(raw, str) and raw.endswith("%"):
                raw = raw[:-1]
            row[column] = raw
    if not row and value:
        raise ValueError("开盘啦对象缺少声明的业务字段")
    extra = {key: value for key, value in value.items() if key not in used}
    if extra:
        row["extra_json"] = _json(extra)
    return _finish(spec, row)


def _array_row(spec: KplDataset, value: Any, columns: tuple[str, ...] | None = None) -> dict:
    columns = columns or tuple(
        column for column in spec.columns if column != "record_id" and not column.endswith("_json")
    )
    if not isinstance(value, list):
        raise ValueError("开盘啦数组结构缺少声明的字段")
    if len(value) < len(columns):
        missing = columns[len(value):]
        if any(not column.startswith("reserved") for column in missing):
            raise ValueError("开盘啦数组结构缺少声明的字段")
        # 部分响应省略末尾预留槽位;业务列仍必须完整且位置不变。
        value = [*value, *([None] * len(missing))]
    row = dict(zip(columns, value, strict=False))
    if len(value) > len(columns):
        row["extra_json"] = _json(value[len(columns):])
    for key, raw in list(row.items()):
        if isinstance(raw, (dict, list)):
            row[key] = _json(raw)
        elif key.endswith("_pct") and isinstance(raw, str) and raw.endswith("%"):
            row[key] = raw[:-1]
    return _finish(spec, row)


def _group_rows(spec: KplDataset, raw: Any, payload: dict, target: date) -> list[dict]:
    if spec.action == "WeightPerformance":
        if not isinstance(raw, dict):
            raise ValueError("权重表现结构应为对象")
        rows = []
        cols = ("plate_id", "plate_name", "change_pct", "leader_id", "leader_name", "leader_pct")
        for direction in ("SZ", "XD"):
            group = raw.get(direction)
            if not isinstance(group, list):
                raise ValueError("权重表现缺少上涨/下跌列表")
            for entry in group:
                row = _array_row(spec, entry, cols)
                row["direction"] = direction
                rows.append(_finish(spec, row))
        return rows
    if not isinstance(raw, list) or any(not isinstance(group, dict) for group in raw):
        raise ValueError("开盘啦分组结构应为对象列表")
    rows = []
    for group in raw:
        if spec.action in ("GetPlateInfo_w38", "GetDatePlate", "GroupStock_W28"):
            key = "List" if spec.action == "GroupStock_W28" else "StockList"
            stock_rows = group.get(key)
            if not isinstance(stock_rows, list):
                raise ValueError("开盘啦分组缺少股票列表")
            if spec.action == "GroupStock_W28":
                columns = spec.columns[1:-2]
                context = {key: group.get(key) for key in ("GroupID", "GroupName")}
            else:
                columns = tuple(_LIMIT_REASONS.split())
                context = {"ZSCode": group.get("ZSCode", payload.get("ZSCode")), "ZSName": group.get("ZSName")}
                if spec.action == "GetDatePlate":
                    group_dates = _dates(group)
                    if len(set(group_dates)) > 1:
                        raise ValueError("开盘啦板块历史日期字段冲突")
                    context.update(trade_date=group_dates[0].isoformat() if group_dates else None,
                                   TCExplain=group.get("TCExplain"))
                else:
                    _check_dates(_dates(group), target)
                    if "nums" in payload:
                        context["nums_json"] = _json(payload["nums"])
            for entry in stock_rows:
                rows.append(_finish(spec, _array_row(spec, entry, columns) | context))
        elif spec.action == "GetYTFP_LHBDX":
            _check_dates(_dates(group), target)
            for side in ("Buy", "Sell"):
                trades = group.get(side, [])
                if not isinstance(trades, list):
                    raise ValueError("龙虎榜动向交易列表结构错误")
                for trade in trades:
                    if not isinstance(trade, dict):
                        raise ValueError("龙虎榜动向交易结构错误")
                    rows.append(_finish(spec, {"BID": group.get("BID"), "BName": group.get("BName"),
                        "direction": side, "StockID": trade.get("Sto"), "Name": trade.get("StoN"),
                        "money_raw": trade.get("Money"), "Three": trade.get("Three")}))
        elif spec.action == "YouZiDongXiangByList":
            _check_dates(_dates(group), target)
            trades = group.get("List")
            if not isinstance(trades, list):
                raise ValueError("游资动向缺少股票列表")
            for trade in trades:
                if not isinstance(trade, dict):
                    raise ValueError("游资动向股票结构错误")
                if trade.get("ID") in (None, ""):
                    raise ValueError("游资动向股票缺少业务键")
                trade_with_context = trade | {"investor_id": group.get("ID"),
                    "investor_name": group.get("ShortName"), "StockID": str(trade.get("ID", "")).zfill(6)}
                row = _object_row(spec, trade_with_context, target)
                rows.append(_finish(spec, row))
        elif spec.action == "GroupInfo":
            row = _object_row(spec, group, target)
            row.update({key: payload.get(key) for key in ("GID", "ShortName", "Info")})
            rows.append(_finish(spec, row))
        elif spec.action == "InfoSearch":
            row = _object_row(spec, group, target)
            row["kind"] = "theme"
            rows.append(_finish(spec, row))
        else:
            raise ValueError("开盘啦分组接口尚无解析契约")
    if spec.action == "InfoSearch":
        secondary = payload.get("SList", [])
        if not isinstance(secondary, list):
            raise ValueError("子题材列表结构错误")
        for group in secondary:
            row = _object_row(spec, group, target)
            row["kind"] = "subtheme"
            rows.append(_finish(spec, row))
    return rows


def normalize_emotion_history(payload: Any, through: date) -> list[dict]:
    """Keep verified source sessions for the sentiment page, never relabel holidays."""
    spec = get_dataset("ext_kpl_emotion")
    if not isinstance(payload, dict) or str(payload.get("errcode")) != "0":
        raise ValueError("市场情绪接口返回失败状态")
    raw = _extract(payload, spec.response_path)
    if not isinstance(raw, list) or len(raw) > 1000 or any(not isinstance(row, dict) for row in raw):
        raise ValueError("市场情绪历史结构或数量超出声明范围")
    sessions = {}
    for value in raw:
        row_dates = _dates(value)
        if len(set(row_dates)) != 1:
            raise ValueError("市场情绪历史缺少日期或日期字段冲突")
        session = row_dates[0]
        if session > through:
            continue
        row = _object_row(spec, value, session) | {"trade_date": session.isoformat()}
        if session in sessions and sessions[session] != row:
            raise ValueError("市场情绪历史同一日期返回冲突记录")
        sessions[session] = row
    return [sessions[session] for session in sorted(sessions)]


def normalize_payload(spec: KplDataset, payload: Any, target: date) -> tuple[list[dict], date | None, int]:
    """响应 → 有意义平铺行、可证明的日期、原始分页条数。

    时间戳是供应商响应/发布时间,不推断为价格更新时间。date=None 表示
    包没有可核验归属日期;客户端可另标 request_parameter,不伪造响应日期。
    """
    if not isinstance(payload, dict):
        raise ValueError("开盘啦响应结构应为对象")
    if "errcode" in payload and str(payload["errcode"]) != "0":
        raise ValueError("开盘啦接口返回失败状态")
    if spec.action in ("GetNewOneStockInfo", "YouZiDongXiangByList"):
        _check_seat_dates(payload, target)
    actual = _check_dates(_dates(payload), target)
    raw = _extract(payload, spec.response_path)
    if spec.action == "ChangeStatistics":
        if not isinstance(raw, list) or any(not isinstance(row, dict) for row in raw):
            raise ValueError("市场情绪结构应为对象列表")
        rows = []
        for row in raw:
            row_dates = _dates(row)
            if len(set(row_dates)) > 1:
                raise ValueError("市场情绪行日期字段冲突")
            if row_dates and row_dates[0] == target:
                rows.append(_object_row(spec, row, target))
        return rows, target if rows else None, len(raw)
    # 历史 info=[二维股票列表, 日期];破板接口也会以总数作为数字尾项。
    if (spec.action in ("DailyLimitPerformance", "DailyLimitPerformance2")
            and isinstance(raw, list) and len(raw) == 2
            and isinstance(raw[0], list)
            and (not raw[0] or isinstance(raw[0][0], list))
            and (_as_date(raw[1]) or (
                isinstance(raw[1], int) and not isinstance(raw[1], bool) and raw[1] >= 0
            ))):
        if suffix_date := _as_date(raw[1]):
            actual = _check_dates([suffix_date], target)
        raw = raw[0]
    if spec.row_kind == "object":
        if raw == {}:
            return [], actual, 0
        row = _object_row(spec, raw, target)
        if isinstance(raw, dict):
            row_date = _check_dates(_dates(raw), target)
            actual = actual or row_date
        return [row], actual, 1
    if spec.row_kind == "array":
        if not isinstance(raw, list):
            raise ValueError("开盘啦响应数据结构应为列表")
        return ([_array_row(spec, raw)] if raw else []), actual, 1 if raw else 0
    if spec.row_kind == "groups":
        rows = _group_rows(spec, raw, payload, target)
        if isinstance(raw, list) and spec.action != "GetDatePlate":
            actual = actual or _check_dates([day for group in raw for day in _dates(group)], target)
        raw_count = sum(len(raw.get(side, [])) for side in ("SZ", "XD")) if isinstance(raw, dict) else len(raw)
        return rows, actual, raw_count
    if not isinstance(raw, list):
        raise ValueError("开盘啦响应数据结构应为列表")
    if spec.row_kind == "arrays":
        rows = [_array_row(spec, entry) for entry in raw]
        if spec.action == "GetZhangTingTianTi" and "ZhuShuList" in payload:
            rows = [row | {"ZhuShuList_json": _json(payload["ZhuShuList"])} for row in rows]
    elif spec.row_kind == "objects":
        rows = [_object_row(spec, entry, target) for entry in raw]
        row_dates = [day for entry in raw for day in _dates(entry)]
        actual = actual or _check_dates(row_dates, target)
    elif spec.row_kind == "scalars":
        rows = []
        for entry in raw:
            if not isinstance(entry, (str, int, float)):
                raise ValueError("开盘啦标量列表结构错误")
            if spec.action == "GetDayNewHigh_W28":
                parts = str(entry).split("_")
                if len(parts) != 4 or not _as_date(parts[0]):
                    raise ValueError("历史新高趋势字段结构错误")
                parts[0] = _as_date(parts[0]).isoformat()
                rows.append(_array_row(spec, parts))
            elif spec.action == "GetHoliday":
                parsed = _as_date(str(entry))
                if not parsed:
                    raise ValueError("节假日日期结构错误")
                rows.append({"holiday_date": parsed.isoformat()})
            else:
                rows.append(_finish(spec, {spec.columns[0]: str(entry)}))
    else:
        raise ValueError("开盘啦行类型未声明")
    return rows, actual, len(raw)
