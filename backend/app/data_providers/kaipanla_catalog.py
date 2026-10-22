"""开盘啦匿名接口目录与边界解析;不包含账号、登录接口或行情源替换。

供应商百分数保持百分数,金额只使用文档注明的口径。目录只能证明文档
能力,实际可用性由请求结果判断。仅包含市场情绪菜单需要的匿名只读数据集。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

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
    params: dict = field(default_factory=dict)
    response_path: str = "info"
    row_kind: str = "arrays"
    columns: tuple[str, ...] = ()
    pageable: bool = False


def _spec(
    name: str, label: str, category: str, action: str, controller: str, columns: str, **kwargs
) -> KplDataset:
    return KplDataset(
        id=f"ext_kpl_{name}",
        label=label,
        category=category,
        action=action,
        controller=controller,
        columns=tuple(columns.split()),
        **kwargs,
    )


_CORE_REQUEST_IDS = ",".join(
    f"{symbol.split('.')[1]}{symbol.split('.')[0]}" for symbol in CORE_INDEX_SYMBOLS
)


# Only datasets consumed by the market-emotion menu are exposed.
_DATASETS = (
    _spec(
        "emotion",
        "市场情绪",
        "市场概览",
        "ChangeStatistics",
        "HisHomeDingPan",
        "record_id strong ztjs lbgd df_num",
        date_param="Date",
        row_kind="objects",
        params={"Index": "0", "st": "1000"},
    ),
    _spec(
        "capacity",
        "市场量能",
        "市场概览",
        "MarketCapacity",
        "HisHomeDingPan",
        "record_id last_wan s_zrcs_wan s_zrtj_wan s3_zrtj_wan yclnstr color time",
        date_param="Date",
        params={"Type": "0"},
        row_kind="object",
    ),
    _spec(
        "indices",
        "核心指数快照",
        "市场概览",
        "GetZsReal",
        "StockL2History",
        "StockID prod_name last_px increase_rate_pct increase_amount turnover",
        method="POST",
        date_param="Day",
        current_host="apphwhq",
        current_controller="UserSelectStock",
        response_path="StockList",
        row_kind="objects",
        params={"StockIDList": _CORE_REQUEST_IDS},
    ),
    _spec(
        "limit_counts",
        "涨跌停总数",
        "涨停分析",
        "MarketStockZDNum",
        "HisHomeDingPan",
        "record_id SJZT SJDT",
        method="POST",
        date_param="Date",
        row_kind="object",
    ),
    _spec(
        "limit_ladder_counts",
        "连板数量统计",
        "涨停分析",
        "DailyLimitIndex",
        "HisHomeDingPan",
        "record_id first_board_count second_board_count third_board_count fourth_board_count fifth_plus_count",
        date_param="Day",
        current_host="apphwhq",
        current_controller="HomeDingPan",
        row_kind="array",
    ),
    _spec(
        "limit_performance",
        "涨停表现",
        "涨停分析",
        "ZhangTingExpression",
        "HisHomeDingPan",
        "record_id limit_up_count two_board_count three_board_count max_board_count "
        "two_board_promotion_pct three_board_promotion_pct max_board_promotion_pct broken_rate_pct "
        "yesterday_limit_up_pct yesterday_continuous_pct yesterday_broken_pct summary",
        date_param="Day",
        row_kind="array",
    ),
    _spec(
        "limit_up",
        "涨停板列表",
        "涨停分析",
        "DailyLimitPerformance",
        "HisHomeDingPan",
        "StockID Name reserved2 reserved3 limit_up_ts reason seal_amount max_seal_amount "
        "main_net main_buy main_sell turnover concepts float_market_cap turnover_pct reserved15 "
        "resealed amplitude_pct board_tag ZSCode reserved20 price change_pct",
        date_param="Day",
        current_host="apphwhq",
        current_controller="HomeDingPan",
        params={"PidType": "1", "Type": "4", "Order": "0", "st": "100", "Index": "0"},
        pageable=True,
    ),
    _spec(
        "broken_limits",
        "破板个股",
        "涨停分析",
        "DailyLimitPerformance2",
        "HisHomeDingPan",
        "StockID Name reserved2 reserved3 price change_pct concepts main_net main_buy main_sell "
        "turnover float_market_cap turnover_pct reserved13 amplitude_pct reserved15 reserved16 max_seal_amount",
        date_param="Day",
        current_host="apphwhq",
        current_controller="HomeDingPan",
        params={"PidType": "1", "Type": "5", "Order": "1", "st": "100", "Index": "0"},
        pageable=True,
    ),
    _spec(
        "withdrawal",
        "大幅回撤",
        "涨停分析",
        "SharpWithdrawal",
        "HisHomeDingPan",
        "StockID Name change_pct withdrawal_pct price",
        date_param="Day",
    ),
    _spec(
        "weight",
        "权重表现",
        "板块数据",
        "WeightPerformance",
        "HisHomeDingPan",
        "record_id plate_id plate_name change_pct leader_id leader_name leader_pct direction",
        date_param="Day",
        row_kind="groups",
    ),
    _spec(
        "live",
        "大盘直播",
        "直播数据",
        "ZhiBoContent",
        "HisConceptionPoint",
        "ID UID Time Comment Type ShareData_json UserName Image Stock_json DisStock_json",
        method="POST",
        date_param="Date",
        current_host="apphwhq",
        current_controller="ConceptionPoint",
        response_path="List",
        row_kind="objects",
    ),
)


def datasets() -> tuple[KplDataset, ...]:
    return _DATASETS


def get_dataset(dataset_id: str) -> KplDataset | None:
    return next((spec for spec in _DATASETS if spec.id == dataset_id), None)


_LABELS = {
    "record_id": "记录编号",
    "strong": "情绪强度",
    "ztjs": "涨停家数",
    "lbgd": "连板高度",
    "df_num": "大幅回撤家数",
    "last_wan": "最新量能(万)",
    "s_zrcs_wan": "昨日量能(万)",
    "s_zrtj_wan": "昨日统计量能(万)",
    "s3_zrtj_wan": "三日量能(万)",
    "yclnstr": "预测成交额",
    "SJZT": "涨停家数",
    "SJDT": "跌停家数",
    "StockID": "股票代码",
    "Name": "名称",
    "ID": "业务编号",
    "plate_id": "板块代码",
    "plate_name": "板块名称",
    "ZSCode": "板块代码",
    "prod_name": "指数名称",
    "last_px": "指数点位",
    "increase_rate_pct": "指数涨跌幅(%)",
    "increase_amount": "涨跌点数",
    "limit_up_ts": "涨停时间",
    "first_board_count": "首板数量",
    "second_board_count": "二板数量",
    "third_board_count": "三板数量",
    "fourth_board_count": "四板数量",
    "fifth_plus_count": "五板及以上数量",
    "limit_up_count": "家数原值(口径待核)",
    "two_board_count": "二连板家数",
    "three_board_count": "三连板家数",
    "max_board_count": "板数原值(口径待核)",
    "two_board_promotion_pct": "二板晋级率(%)",
    "three_board_promotion_pct": "三板晋级率(%)",
    "max_board_promotion_pct": "最高板晋级率(%)",
    "broken_rate_pct": "破板率(%)",
    "yesterday_limit_up_pct": "昨日涨停表现(%)",
    "yesterday_continuous_pct": "昨日连板表现(%)",
    "yesterday_broken_pct": "昨日破板表现(%)",
    "summary": "盘面总结",
    "price": "价格",
    "change_pct": "涨跌幅(%)",
    "turnover_pct": "换手率(%)",
    "amplitude_pct": "振幅(%)",
    "withdrawal_pct": "回撤幅度(%)",
    "reason": "涨停原因",
    "concepts": "所属概念",
    "main_net": "主力净额(元)",
    "main_buy": "主力买入(元)",
    "main_sell": "主力卖出(元)",
    "turnover": "成交额(元)",
    "float_market_cap": "流通市值(元)",
    "seal_amount": "封单额(元)",
    "max_seal_amount": "最大封单额(元)",
    "board_tag": "连板标签",
    "resealed": "是否回封",
    "direction": "交易方向",
    "Time": "发布时间",
    "time": "来源时间",
    "Comment": "盘面解读",
    "UserName": "作者",
    "trade_date": "业务日期",
    "date": "查询归属日期",
    "source": "数据来源",
    "fetched_at": "获取时间",
    "extra_json": "额外字段",
    "color": "展示颜色编号",
    "leader_id": "龙头股票代码",
    "leader_name": "龙头名称",
    "leader_pct": "龙头涨跌幅(%)",
    "UID": "作者编号",
    "Type": "消息类型",
    "Image": "作者头像",
}


def field_label(name: str) -> str:
    return _LABELS.get(name, name)


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


def _extract(payload: dict, path: str) -> Any:
    current: Any = payload
    for key in path.split(".") if path else []:
        if not isinstance(current, dict) or key not in current:
            raise ValueError(f"开盘啦响应结构缺少 {path}")
        current = current[key]
    return current


def _business_id(spec: KplDataset, row: dict) -> str:
    # 不以列表顺序/抓取时间为键,否则翻页和刷新会产生不同业务记录。
    keys = (
        "StockID",
        "ID",
        "ZSCode",
        "GroupID",
        "BID",
        "investor_id",
        "direction",
        "D3",
        "Three",
        "plate_id",
        "TimeMin",
        "TagID",
        "trade_date",
        "kind",
    )
    identity = {key: row[key] for key in keys if row.get(key) is not None}
    if not identity:
        if spec.action in {
            "ChangeStatistics",
            "MarketCapacity",
            "MarketStockZDNum",
            "DailyLimitIndex",
            "ZhangTingExpression",
            "MorningBidding",
            "MorningBiddingNum",
        }:
            return spec.id
        raise ValueError("开盘啦数据行缺少稳定业务键")
    return hashlib.sha256(_json(identity).encode("utf-8")).hexdigest()[:24]


def _finish(spec: KplDataset, row: dict) -> dict:
    if "record_id" in spec.columns:
        row["record_id"] = _business_id(spec, row)
    elif row.get(spec.columns[0]) in (None, ""):
        raise ValueError("开盘啦数据行缺少稳定业务键")
    # 稳定业务键保持在首列;新增供应商字段保留为 extra_json。
    return {column: row.get(column) for column in spec.columns} | {
        key: value for key, value in row.items() if key not in spec.columns
    }


_ALIASES = {
    "last_wan": "last",
    "s_zrcs_wan": "s_zrcs",
    "s_zrtj_wan": "s_zrtj",
    "s3_zrtj_wan": "s3_zrtj",
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
        missing = columns[len(value) :]
        if any(not column.startswith("reserved") for column in missing):
            raise ValueError("开盘啦数组结构缺少声明的字段")
        # 部分响应省略末尾预留槽位;业务列仍必须完整且位置不变。
        value = [*value, *([None] * len(missing))]
    row = dict(zip(columns, value, strict=False))
    if len(value) > len(columns):
        row["extra_json"] = _json(value[len(columns) :])
    for key, raw in list(row.items()):
        if isinstance(raw, (dict, list)):
            row[key] = _json(raw)
        elif key.endswith("_pct") and isinstance(raw, str) and raw.endswith("%"):
            row[key] = raw[:-1]
    return _finish(spec, row)


def _group_rows(spec: KplDataset, raw: Any) -> list[dict]:
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
    raise ValueError("开盘啦分组接口尚无解析契约")


def normalize_emotion_history(payload: Any, through: date) -> list[dict]:
    """Keep verified source sessions for the sentiment page, never relabel holidays."""
    spec = get_dataset("ext_kpl_emotion")
    if not isinstance(payload, dict) or str(payload.get("errcode")) != "0":
        raise ValueError("市场情绪接口返回失败状态")
    raw = _extract(payload, spec.response_path)
    if (
        not isinstance(raw, list)
        or len(raw) > 1000
        or any(not isinstance(row, dict) for row in raw)
    ):
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


def normalize_payload(
    spec: KplDataset, payload: Any, target: date
) -> tuple[list[dict], date | None, int]:
    """响应 → 有意义平铺行、可证明的日期、原始分页条数。

    时间戳是供应商响应/发布时间,不推断为价格更新时间。date=None 表示
    包没有可核验归属日期;客户端可另标 request_parameter,不伪造响应日期。
    """
    if not isinstance(payload, dict):
        raise ValueError("开盘啦响应结构应为对象")
    if "errcode" in payload and str(payload["errcode"]) != "0":
        raise ValueError("开盘啦接口返回失败状态")
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
    if (
        spec.action in ("DailyLimitPerformance", "DailyLimitPerformance2")
        and isinstance(raw, list)
        and len(raw) == 2
        and isinstance(raw[0], list)
        and (not raw[0] or isinstance(raw[0][0], list))
        and (
            _as_date(raw[1])
            or (isinstance(raw[1], int) and not isinstance(raw[1], bool) and raw[1] >= 0)
        )
    ):
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
        rows = _group_rows(spec, raw)
        raw_count = (
            sum(len(raw.get(side, [])) for side in ("SZ", "XD"))
            if isinstance(raw, dict)
            else len(raw)
        )
        return rows, actual, raw_count
    if not isinstance(raw, list):
        raise ValueError("开盘啦响应数据结构应为列表")
    if spec.row_kind == "arrays":
        rows = [_array_row(spec, entry) for entry in raw]
    elif spec.row_kind == "objects":
        rows = [_object_row(spec, entry, target) for entry in raw]
        row_dates = [day for entry in raw for day in _dates(entry)]
        actual = actual or _check_dates(row_dates, target)
    else:
        raise ValueError("开盘啦行类型未声明")
    return rows, actual, len(raw)
