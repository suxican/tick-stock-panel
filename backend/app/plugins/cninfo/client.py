"""巨潮资讯网公告查询客户端。调用方只接触标准化后的记录, 不接触厂商结构。

接口无官方文档, 以下为实测 (2026-11):
- POST /new/hisAnnouncement/query, stock="代码,机构代码" + seDate 区间按个股查询;
  只传六位代码返回空, 机构代码取自 /new/data/szse_stock.json (含沪深京 A 股);
- 每页固定 30 条 (pageSize 更大无效), hasMore 标识是否还有下一页;
- 网关间歇返回 504 (约 10 秒超时), 北京时间早盘前尤其频繁, 需重试;
- 按分类或标题关键词的筛选比无筛选查询更容易超时, 故分类在本地按标题做;
- announcementTime 为毫秒时间戳; 创业板等带具体时刻, 沪市/北交所/深主板多为当日 00:00。
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from datetime import date, datetime

import httpx

from app.market_time import CN_TZ

BASE_URL = "https://www.cninfo.com.cn"
PDF_BASE = "https://static.cninfo.com.cn/"
PAGE_SIZE = 30
MAX_PAGES = 20  # 单股一个月很少超过 3 页, 防止上游异常时无限翻页
_INTERVAL_S = 1.0  # 请求间隔, 降低被封 IP 的风险
_RETRY_BACKOFF_S = (2.0, 5.0, 10.0, 20.0)
_CODE = re.compile(r"[0-9]{6}")
_COLUMNS = {"SH": "sse", "SZ": "szse", "BJ": "bj"}
_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": f"{BASE_URL}/new/commonUrl/pageOfSearch?url=disclosure/list/search",
}


class CninfoError(RuntimeError):
    """网络失败、非 JSON 或结构异常。不保存部分结果。"""


def _record(raw: object) -> dict | None:
    if not isinstance(raw, dict):
        raise CninfoError("公告记录格式异常")
    code, title, ms = raw.get("secCode"), raw.get("announcementTitle"), raw.get("announcementTime")
    ann_id, url = raw.get("announcementId"), raw.get("adjunctUrl")
    if not (isinstance(code, str) and _CODE.fullmatch(code)):
        return None  # 债券等非六位股票代码
    if not (isinstance(title, str) and title.strip() and isinstance(ann_id, str) and ann_id
            and isinstance(ms, int) and not isinstance(ms, bool) and ms > 0):
        raise CninfoError("公告记录缺少标题、标识或时间")
    published = datetime.fromtimestamp(ms / 1000, CN_TZ)
    name = raw.get("secName")
    return {
        "id": ann_id,
        "code": code,
        "name": name.strip() if isinstance(name, str) and name.strip() else code,
        # 标题里的 <em> 等高亮标签来自检索接口, 去掉后保存纯文本
        "title": re.sub(r"<[^>]+>", "", title).strip(),
        "published_at": published.isoformat(timespec="seconds"),
        "has_time": (published.hour, published.minute, published.second) != (0, 0, 0),
        "url": PDF_BASE + url if isinstance(url, str) and url else None,
    }


class CninfoClient:
    def __init__(self, timeout: float = 20.0, sleep: Callable[[float], None] = time.sleep) -> None:
        self._http = httpx.Client(base_url=BASE_URL, headers=_HEADERS, timeout=timeout)
        self._sleep = sleep
        self._requested = False

    def close(self) -> None:
        self._http.close()

    def _request(self, method: str, path: str, **kwargs) -> object:
        if self._requested:
            self._sleep(_INTERVAL_S)
        self._requested = True
        for backoff in (*_RETRY_BACKOFF_S, None):
            try:
                resp = self._http.request(method, path, **kwargs)
                error = None if resp.status_code == 200 else CninfoError(f"巨潮返回 HTTP {resp.status_code}")
                retryable = resp.status_code >= 500 or resp.status_code == 429
            except httpx.HTTPError as exc:
                error, retryable = CninfoError("巨潮请求失败"), True
                error.__cause__ = exc
            if error is None:
                break
            if not retryable or backoff is None:
                raise error
            self._sleep(backoff)
        try:
            return resp.json()
        except ValueError as exc:
            raise CninfoError("巨潮响应不是 JSON") from exc

    def org_ids(self) -> dict[str, str]:
        """六位代码 → 巨潮机构代码 (A 股)。"""
        payload = self._request("GET", "/new/data/szse_stock.json")
        rows = payload.get("stockList") if isinstance(payload, dict) else None
        if not isinstance(rows, list) or not rows:
            raise CninfoError("巨潮代码表为空或格式异常")
        return {r["code"]: r["orgId"] for r in rows
                if isinstance(r, dict) and isinstance(r.get("code"), str) and isinstance(r.get("orgId"), str)}

    def stock_announcements(self, symbol: str, org_id: str, start: date, end: date) -> list[dict]:
        """单只股票在 [start, end] 的全部公告。任一页失败整体抛错; 翻页不足视为不完整。"""
        code, _, suffix = symbol.partition(".")
        column = _COLUMNS.get(suffix)
        if column is None or not _CODE.fullmatch(code):
            raise ValueError(f"不支持的代码: {symbol}")
        records: dict[str, dict] = {}
        total = 0
        for page in range(1, MAX_PAGES + 1):
            payload = self._request("POST", "/new/hisAnnouncement/query", data={
                "pageNum": page, "pageSize": PAGE_SIZE, "column": column, "tabName": "fulltext",
                "plate": "", "stock": f"{code},{org_id}", "searchkey": "", "secid": "", "category": "",
                "trade": "", "seDate": f"{start.isoformat()}~{end.isoformat()}", "sortName": "",
                "sortType": "", "isHLtitle": "false",
            })
            if not isinstance(payload, dict):
                raise CninfoError("巨潮响应格式异常")
            rows = payload.get("announcements") or []
            count = payload.get("totalAnnouncement")
            if not isinstance(rows, list) or not isinstance(count, int) or count < 0:
                raise CninfoError("巨潮响应缺少公告列表或总数")
            total = max(total, count)
            for raw in rows:
                if (item := _record(raw)) is not None and item["code"] == code:
                    records[item["id"]] = item
            if not payload.get("hasMore") or not rows:
                break
        else:
            raise CninfoError("巨潮翻页超过上限")
        if page < -(-total // PAGE_SIZE):
            raise CninfoError("巨潮翻页提前结束, 公告不完整")
        return list(records.values())
