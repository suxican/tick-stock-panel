"""Anonymous Kaipanla supplementary data; never substitutes the quote provider.

The catalog is the request allowlist. Complete pages are validated before a
snapshot is published, and callers can distinguish cached/stale observations.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import threading
import time
import uuid
import weakref
from collections import OrderedDict
from datetime import date
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import httpx

from app.market_time import cn_now, cn_today
from app.services.kaipanla_catalog import get_dataset, normalize_emotion_history, normalize_payload

_MAX_PAGES = 30
_MAX_ROWS = 30_000
_CACHE_LIMIT = 128
_CACHE: OrderedDict[tuple, tuple[float, dict]] = OrderedDict()
_CACHE_LOCK = threading.Lock()
_LOOPS: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
_DEVICE_ID = uuid.uuid4().hex
_HEADERS = {
    "User-Agent": "Dalvik/2.1.0 (Linux; U; Android 9)",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
}
_COMMON = {"DeviceID": _DEVICE_ID, "PhoneOSNew": "2", "VerSion": "5.23.0.1", "apiv": "w44"}
_RESERVED = {"a", "c", "deviceid", "phoneosnew", "version", "apiv", "index"}
_CREDENTIALS = {"token", "userid", "authorization", "cookie"}


def _loop_state() -> tuple[asyncio.Semaphore, dict]:
    loop = asyncio.get_running_loop()
    # All accesses to this state happen on the owning loop. A new loop (e.g.
    # an isolated test) must never inherit asyncio locks from a closed loop.
    with _CACHE_LOCK:
        if loop not in _LOOPS:
            _LOOPS[loop] = (asyncio.Semaphore(2), {})
        return _LOOPS[loop]


def _parameters(spec, parameters: dict | None) -> dict:
    supplied = parameters or {}
    allowed = set(spec.params) | set(spec.required_params)
    out = dict(spec.params)
    for key, value in supplied.items():
        if key.casefold() in _CREDENTIALS:
            raise ValueError("开盘啦补充数据仅支持匿名请求,不接受登录凭据")
        if key not in allowed or key.casefold() in _RESERVED:
            raise ValueError(f"该数据集不支持参数: {key}")
        if not isinstance(value, (str, int, float)) or isinstance(value, bool):
            raise ValueError(f"参数 {key} 必须是字符串或数值")
        if len(str(value)) > 500:
            raise ValueError(f"参数 {key} 过长")
        out[key] = value
    missing = [key for key in spec.required_params if not str(out.get(key, "")).strip()]
    if missing:
        raise ValueError("缺少查询参数: " + ", ".join(missing))
    for key in ("DStart", "DEnd"):
        if key in out:
            try:
                parsed = date.fromisoformat(str(out[key]))
            except ValueError as exc:
                raise ValueError(f"{key} 必须为 YYYY-MM-DD") from exc
            if parsed > cn_today():
                raise ValueError("区间查询不能使用未来日期")
    if "DStart" in out and "DEnd" in out and str(out["DStart"]) > str(out["DEnd"]):
        raise ValueError("区间开始日期不能晚于结束日期")
    if "st" in out:
        size = int(out["st"])
        if not 1 <= size <= 1000:
            raise ValueError("每页数量必须在 1 到 1000 之间")
        out["st"] = size
    return out


def _wire(spec, target: date, parameters: dict) -> tuple[str, dict]:
    current = target == cn_today() and spec.current_host is not None
    host = spec.current_host if current else spec.host
    controller = spec.current_controller or spec.controller if current else spec.controller
    params = {**_COMMON, "a": spec.action, "c": controller, **parameters}
    if spec.date_param:
        params[spec.date_param] = (
            target.strftime("%Y%m%d") if spec.date_format == "compact" else target.isoformat()
        )
    if current and spec.action == "GetZsReal":
        from app.services.index_const import CORE_INDEX_SYMBOLS

        params["a"] = "RefreshStockList"
        params["StockIDList"] = ",".join(
            exchange + code for code, exchange in (symbol.split(".") for symbol in CORE_INDEX_SYMBOLS)
        )
        params.pop(spec.date_param, None)
    return f"https://{host}.longhuvip.com/w1/api/index.php", params


async def _request(client: httpx.AsyncClient, method: str, url: str, params: dict) -> dict:
    kwargs = {"params": params} if method == "GET" else {"data": params}
    response = await client.request(method, url, **kwargs)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("开盘啦响应结构不是对象")
    if str(payload.get("errcode")) != "0":
        raise ValueError(f"开盘啦接口返回错误码 {payload.get('errcode', 'missing')}")
    return payload


def _total(payload: dict, spec) -> int | None:
    # Broken-limit history wraps rows with their total rather than a date.
    info = payload.get("info")
    if (spec.action == "DailyLimitPerformance2" and isinstance(info, list) and len(info) == 2
            and isinstance(info[0], list) and isinstance(info[1], int) and not isinstance(info[1], bool)):
        return max(0, info[1])
    keys = ("GroupCount",) if spec.action == "GroupStock_W28" else ("Total", "Count")
    for key in keys:
        value = payload.get(key)
        if isinstance(value, (int, str)):
            try:
                return max(0, int(value))
            except ValueError:
                continue
    return None


async def _fetch(spec, target: date, parameters: dict, *, include_history: bool = False) -> dict:
    if not spec.historical and target != cn_today():
        raise ValueError("该接口只提供当前快照,不能用于指定历史日期")
    range_end = date.fromisoformat(str(parameters["DEnd"])) if "DEnd" in parameters else None
    response_date = range_end or target
    url, base = _wire(spec, response_date, parameters)
    all_rows: list[dict] = []
    history: list[dict] = []
    actual_date: date | None = None
    group_values = (
        range(1, 6) if spec.action in {"DailyLimitPerformance", "DailyLimitPerformance2"}
        and "PidType" not in parameters else (parameters.get("PidType"),)
    )
    # The defaults contain PidType; the caller signals aggregate by omitting
    # its override (fetch_dataset removes that default before entering here).
    async with httpx.AsyncClient(timeout=12, headers=_HEADERS) as client:
        for group in group_values:
            params = dict(base)
            if group is not None:
                params["PidType"] = group
            offset = 0
            seen_pages: set[str] = set()
            for _ in range(_MAX_PAGES if spec.pageable else 1):
                if spec.pageable:
                    params["Index"] = offset
                payload = await _request(client, spec.method, url, params)
                # These detail endpoints omit the root identity in live replies;
                # bind the row to the explicit query, never to its list position.
                identity_param = {"GetNewOneStockInfo": "StockID", "InfoGet": "ID"}.get(spec.action)
                if identity_param:
                    identity = str(params[identity_param])
                    if payload.get("ID") not in (None, "") and str(payload["ID"]) != identity:
                        raise ValueError("详情响应标识与查询参数不一致")
                    payload = {**payload, "ID": identity}
                rows, observed_date, raw_count = normalize_payload(spec, payload, response_date)
                if include_history:
                    history = normalize_emotion_history(payload, target)
                if spec.action == "ChangeStatistics" and raw_count and not rows and not include_history:
                    raise ValueError("情绪接口未返回请求日期,保留原快照")
                if observed_date is not None:
                    if observed_date != response_date:
                        raise ValueError(
                            f"数据日期 {observed_date} 与请求日期 {response_date} 不一致,未写入快照"
                        )
                    actual_date = observed_date
                if group is not None:
                    rows = [{**row, "pid_type": group} for row in rows]
                if raw_count:
                    digest = hashlib.sha256(
                        json.dumps(rows, ensure_ascii=False, sort_keys=True).encode()
                    ).hexdigest()
                    if digest in seen_pages:
                        raise ValueError("分页未推进,拒绝发布不完整榜单")
                    seen_pages.add(digest)
                all_rows.extend(rows)
                if len(all_rows) > _MAX_ROWS:
                    raise ValueError("数据超过单次拉取上限,未发布不完整榜单")
                total = _total(payload, spec)
                if spec.pageable and raw_count == 0 and total is not None and offset < total:
                    raise ValueError("接口提前返回空页,未发布不完整榜单")
                offset += raw_count
                if not spec.pageable or raw_count == 0 or (total is not None and offset >= total):
                    break
                await asyncio.sleep(0.15)
            else:
                raise ValueError("分页超过安全上限,未发布不完整榜单")
    # These endpoints return multi-day history without a date parameter. Keep
    # future sessions out of an explicit historical query.
    if spec.action in {"GetDayNewHigh_W28", "GetDatePlate"}:
        date_column = "trend_date" if spec.action == "GetDayNewHigh_W28" else "trade_date"
        all_rows = [row for row in all_rows if row.get(date_column) and (
            date.fromisoformat(str(row[date_column])) <= target
        )]
    fetched_at = cn_now().isoformat()
    # Source time is intentionally not synthesized from our fetch time.
    for row in all_rows:
        row.update({"date": target.isoformat(), "source": "开盘啦", "fetched_at": fetched_at})
    result = {
        "id": spec.id, "source": "开盘啦", "requested_date": target.isoformat(),
        "data_date": actual_date.isoformat() if actual_date else (
            range_end.isoformat() if range_end else (
                target.isoformat() if spec.date_param and spec.date_param in base else None
            )
        ),
        "date_origin": "response" if actual_date else (
            "range_parameters" if range_end else (
                "request_parameter" if spec.date_param and spec.date_param in base else "observation"
            )
        ),
        "fetched_at": fetched_at, "state": "ok" if all_rows else "empty", "rows": all_rows,
    }
    if include_history:
        result["history"] = [row | {
            "date": row["trade_date"], "source": "开盘啦", "fetched_at": fetched_at,
        } for row in history]
    return result


async def fetch_dataset(
    dataset_id: str, target: date | None = None, parameters: dict | None = None,
    *, force: bool = False, include_history: bool = False,
) -> dict:
    """Fetch a catalog dataset with bounded caching and one in-flight request per key."""
    spec = get_dataset(dataset_id)
    if spec is None:
        raise ValueError("未知或需要登录的开盘啦数据集")
    if include_history and spec.action != "ChangeStatistics":
        raise ValueError("仅市场情绪接口支持历史序列")
    day = target or cn_today()
    if day > cn_today():
        raise ValueError("不能查询未来日期")
    params = _parameters(spec, parameters)
    if "DEnd" in params and date.fromisoformat(str(params["DEnd"])) > day:
        raise ValueError("区间结束日期不能晚于查询归属日期")
    if spec.action in {"DailyLimitPerformance", "DailyLimitPerformance2"} and not (
        parameters and "PidType" in parameters
    ):
        params.pop("PidType", None)
    key = (dataset_id, day.isoformat(), tuple(sorted((k, str(v)) for k, v in params.items())))
    if include_history:
        key = (*key, "history")
    ttl = 60 if day == cn_today() else 3600
    now = time.monotonic()
    with _CACHE_LOCK:
        previous = _CACHE.get(key)
        if previous is not None and not force and now - previous[0] < ttl:
            _CACHE.move_to_end(key)
            return copy.deepcopy(previous[1])
    semaphore, pending = _loop_state()
    if key in pending:
        return copy.deepcopy(await asyncio.shield(pending[key]))
    future = asyncio.get_running_loop().create_future()
    pending[key] = future
    try:
        async with semaphore:
            result = await _fetch(spec, day, params, include_history=True) if include_history else (
                await _fetch(spec, day, params)
            )
        with _CACHE_LOCK:
            _CACHE[key] = (time.monotonic(), copy.deepcopy(result))
            _CACHE.move_to_end(key)
            while len(_CACHE) > _CACHE_LIMIT:
                _CACHE.popitem(last=False)
        future.set_result(result)
        return copy.deepcopy(result)
    except BaseException as exc:
        # Waiters receive the same failure without cancelling the owning fetch.
        if not future.done():
            future.set_exception(exc)
            future.exception()
        raise
    finally:
        pending.pop(key, None)


async def fetch_config_rows(config, target: date, *, force: bool = False) -> list[dict]:
    """Trusted preset adapter for the existing extension pull/storage pipeline."""
    spec = get_dataset(config.id)
    if spec is None or config.pull is None:
        raise ValueError("未知开盘啦预设")
    pull = config.pull
    parsed = urlsplit(pull.url)
    if parsed.hostname not in {
        f"{host}.longhuvip.com" for host in (spec.host, spec.current_host) if host
    }:
        raise ValueError("开盘啦预设必须使用文档声明的接口域名")
    if pull.auth and pull.auth.get("type", "none") != "none":
        raise ValueError("开盘啦预设不支持登录鉴权")
    if any(key.casefold() in _CREDENTIALS for key in pull.headers):
        raise ValueError("开盘啦预设不接受登录凭据")
    allowed = set(spec.params) | set(spec.required_params)
    params = {}
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        if key.casefold() in _CREDENTIALS:
            raise ValueError("开盘啦预设不接受登录凭据")
        if key in allowed and key != "PidType" and key.casefold() not in _RESERVED:
            params[key] = value
    result = await fetch_dataset(config.id, target, params, force=force)
    return result["rows"]


def context_date(data_dir: Path, target: date | None = None) -> date:
    """Default to the same latest local session used by the existing recap."""
    if target is not None:
        if target > cn_today():
            raise ValueError("不能查询未来日期")
        return target
    from app.services.dragon_tiger import resolve_trade_date

    return resolve_trade_date(data_dir, cn_today()) or cn_today()


async def refresh_supplement(data_dir: Path, target: date | None = None) -> dict:
    """Refresh the recap's bounded batch through the existing extension pipeline."""
    from app.services.ext_data import ExtConfigStore
    from app.services.ext_presets import get_preset
    from app.services.ext_pull import fetch_and_ingest
    from app.services.kaipanla_context import CONTEXT_DATASET_IDS, read_kaipanla_context

    day = context_date(data_dir, target)
    store = ExtConfigStore(data_dir)

    async def refresh_one(dataset_id: str) -> dict:
        try:
            config = store.get(dataset_id)
            if config is None:
                config = get_preset(dataset_id)
                if config is None:
                    raise ValueError("补充数据预设不可用")
                store.upsert(config, keep_strategy_cache=True)
            rows, _ = await fetch_and_ingest(
                config, data_dir, day, keep_strategy_cache=True, force=True,
            )
            return {"id": dataset_id, "state": "ok" if rows else "empty", "rows": rows}
        except Exception as exc:
            # Failure of one vendor endpoint must not discard the other seven
            # valid snapshots or overwrite the previous complete observation.
            return {"id": dataset_id, "state": "error", "rows": 0, "message": str(exc)[:200]}

    results = await asyncio.gather(*(refresh_one(dataset_id) for dataset_id in CONTEXT_DATASET_IDS))
    return {
        "source": "开盘啦", "date": day.isoformat(), "results": results,
        "context": read_kaipanla_context(data_dir, day),
    }
