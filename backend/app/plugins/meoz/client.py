"""猫爪 / 猫头鹰数据 HTTP 客户端。

职责: 认证、统一信封解包 (fields + items 二维数组 → dict 行)、节点切换、
429 退避与全局限速、单次 6000 行上限的截断识别。不知道 provider / services 层。

文档 (2026-11 核对 OpenAPI):
  - 请求: POST JSON {apikey, apiname, fields, params}, 两站共用同一 Key。
  - 日内服务 sz/sh.meoz.cn:6688, 历史服务 hist/hist-vip.meoz.cn:6688。
  - 响应: {code, message, data: {fields, items}}; 业务 code=200 成功,
    1002 未找到数据, 401/403 Key 或权限/额度问题, HTTP 429 带 Retry-After。
  - 单次最多返回 6000 行, 超过时服务端截取前 6000 行且不报错 —— 返回行数
    达到上限一律按截断处理, 由调用方拆小请求, 不把残缺结果当成完整数据。
"""

from __future__ import annotations

import logging
import threading
import time

import httpx

logger = logging.getLogger(__name__)

LIVE_URLS = ("https://sz.meoz.cn:6688/api", "https://sh.meoz.cn:6688/api")
HISTORY_URLS = ("https://hist.meoz.cn:6688/api", "https://hist-vip.meoz.cn:6688/api")

ROW_CAP = 6000
# 标准版 3 次/秒; 全局串行节流留出余量, 多线程(实时轮询/分钟刷新/同步任务)共用。
_MIN_INTERVAL_S = 0.35
_RATE_RETRIES = 2
_MAX_RETRY_AFTER_S = 30.0
_MESSAGE_MAX = 200

_OK = 200
_NOT_FOUND = 1002
_AUTH_CODES = {401, 403}


class MeozError(Exception):
    """接口错误(网络失败 / HTTP 非 200 / 业务 code 非 200)。"""

    def __init__(self, message: str = "", *, code: int | str | None = None) -> None:
        super().__init__(message)
        self.code = code


class MeozAuthError(MeozError):
    """Key 缺失、无效、停用, 或套餐权限/额度不足; 重试无意义。"""


class MeozTruncatedError(MeozError):
    """返回行数达到单次上限, 结果可能被服务端截断。"""


def _short(text: object) -> str:
    return str(text or "")[:_MESSAGE_MAX]


class MeozClient:
    """猫爪 REST 客户端 (线程安全: httpx.Client 可并发复用, 节流用锁串行)。"""

    def __init__(
        self,
        api_key: str,
        timeout: float = 20.0,
        live_urls: tuple[str, ...] = LIVE_URLS,
        history_urls: tuple[str, ...] = HISTORY_URLS,
        min_interval_s: float = _MIN_INTERVAL_S,
    ) -> None:
        if not api_key:
            raise MeozAuthError("未配置 MEOZ_API_KEY")
        self._api_key = api_key
        self._urls = {"live": live_urls, "history": history_urls}
        self._min_interval_s = min_interval_s
        self._throttle_lock = threading.Lock()
        self._last_request = 0.0
        self._http = httpx.Client(timeout=timeout)

    def close(self) -> None:
        self._http.close()

    def _throttle(self) -> None:
        with self._throttle_lock:
            wait = self._last_request + self._min_interval_s - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.monotonic()

    def _post(self, service: str, body: dict) -> httpx.Response:
        """按节点顺序发送; 仅连接/超时类错误切换到同一服务的备用节点。"""
        last_error: Exception | None = None
        for url in self._urls[service]:
            self._throttle()
            try:
                return self._http.post(url, json=body)
            except httpx.TransportError as e:
                last_error = e
                logger.info("猫爪节点 %s 不可用, 尝试备用节点: %s", url, type(e).__name__)
        raise MeozError(f"网络请求失败: {type(last_error).__name__}")

    def query(
        self,
        apiname: str,
        params: dict | None = None,
        fields: str | None = None,
        *,
        service: str = "live",
    ) -> list[dict]:
        """调用一个接口, 返回 dict 行列表。

        正常无数据 (code=1002) 返回 []; 达到行数上限抛 MeozTruncatedError。
        """
        body: dict = {"apikey": self._api_key, "apiname": apiname, "params": params or {}}
        if fields:
            body["fields"] = fields
        for attempt in range(_RATE_RETRIES + 1):
            resp = self._post(service, body)
            if resp.status_code != 429:
                break
            if attempt == _RATE_RETRIES:
                raise MeozError(f"{apiname} 请求频率超限 (HTTP 429)", code=429)
            try:
                delay = float(resp.headers.get("Retry-After") or 1)
            except ValueError:
                delay = 1.0
            time.sleep(min(max(delay, 0.5), _MAX_RETRY_AFTER_S))
        return self._parse(apiname, resp)

    @staticmethod
    def _parse(apiname: str, resp: httpx.Response) -> list[dict]:
        try:
            payload = resp.json()
        except ValueError:
            payload = None
        code = payload.get("code") if isinstance(payload, dict) else None
        message = _short(payload.get("message")) if isinstance(payload, dict) else ""
        if resp.status_code in _AUTH_CODES or code in _AUTH_CODES:
            raise MeozAuthError(f"{apiname} 拒绝访问 ({code or resp.status_code}): {message}",
                                code=code or resp.status_code)
        if resp.status_code != 200 or not isinstance(payload, dict):
            raise MeozError(f"{apiname} HTTP {resp.status_code}: {message}", code=resp.status_code)
        if code == _NOT_FOUND:
            return []
        if code != _OK:
            raise MeozError(f"{apiname} 接口错误 code={code}: {message}", code=code)
        data = payload.get("data")
        if data is None:
            return []
        fields = data.get("fields") if isinstance(data, dict) else None
        items = data.get("items") if isinstance(data, dict) else None
        if not isinstance(fields, list) or not isinstance(items, list):
            raise MeozError(f"{apiname} 响应结构无法识别 (缺少 fields/items)")
        rows = [dict(zip(fields, item, strict=False)) for item in items if isinstance(item, list)]
        if len(rows) >= ROW_CAP:
            raise MeozTruncatedError(f"{apiname} 返回 {len(rows)} 行, 达到单次上限, 结果可能被截断")
        return rows
