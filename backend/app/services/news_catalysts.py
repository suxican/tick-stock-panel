"""资讯分析·催化验证 (fuyao 专有) — 按关键词聚合当日异动解读并用本地行情核对。

非路由数据集: 只有 fuyao 提供异动解读, 经 custom_sources 调 provider.anomaly_analysis;
未配置时返回 source_unavailable, 前端引导到数据源配置。

数据契约 (实测 2026-11):
- 上游只收录「已经异动」的个股, 因此这里核对的是广度、新鲜度和次日延续,
  推不出「有消息但盘面没反应」。
- 归属交易日取 last_anomaly_time 的北京日期; 代码从 link_url 的 thsHqCode 解析。
- 历史日不可变 → 按日落 JSON (data/news_catalysts/date=YYYY-MM-DD.json); 当日现取不缓存。
- 空结果可能是上游降级, 不落缓存, 也不据此判定「当日无催化」或「关键词首次出现」。

单位: change_pct / avg_change_pct 均为小数制 (0.0366 表示 3.66%)。
"""

from __future__ import annotations

import json
import logging
import math
import re
from datetime import date as date_cls
from datetime import datetime
from pathlib import Path

import polars as pl

from app.market_time import CN_TZ, cn_now, cn_today
from app.services.dragon_tiger import _local_trading_days, resolve_trade_date
from app.services.fs_utils import atomic_write_text
from app.services.zzshare_limit_reasons import LimitReasonsSourceError, _stock_symbol
from app.services.zzshare_radar import _quotes

logger = logging.getLogger(__name__)

LOOKBACK_DAYS = 10  # 新鲜度回看的交易日数
# 全量查询按返回量受限而非天数: 实测同为 5 天跨度, 约 5900 条成功、更多则报 5003。
_MAX_SPAN_DAYS = 3  # 首次请求的自然日跨度 (含首尾); 失败后二分到单日
_BATCH_MIN_STOCKS = 3  # 「批量」: 同一关键词至少 3 只异动且至少 2 只涨停
_BATCH_MIN_LIMIT_UPS = 2
_LIMIT_UP_TAG = "LIMIT_UP"
_CODE_RE = re.compile(r"[?&]thsHqCode=([0-9]{6})(?:&|$)")


def _cache_path(data_dir: Path, d: date_cls) -> Path:
    return data_dir / "news_catalysts" / f"date={d.isoformat()}.json"


def _load_cache(path: Path, d: date_cls) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if (not isinstance(data, dict) or data.get("date") != d.isoformat()
            or not isinstance(data.get("items"), list) or not data["items"]
            or not isinstance(data.get("fetched_at"), str)):
        return None
    return data


def _provider():
    from app.data_providers import custom as custom_sources

    if not custom_sources.is_custom_provider("fuyao"):
        return None
    return custom_sources.get_provider("fuyao")


def _beijing(ms: object) -> datetime | None:
    if isinstance(ms, bool) or not isinstance(ms, (int, float)) or not math.isfinite(ms) or ms <= 0:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000, CN_TZ)
    except (OverflowError, OSError, ValueError):
        return None


def normalize_items(raw_items: object, days: set[date_cls]) -> dict[date_cls, list[dict]]:
    """上游记录 → 按交易日分组的 A 股个股解读; 每股每日保留最后一次异动的记录。"""
    by_day: dict[date_cls, dict[str, dict]] = {d: {} for d in days}
    for raw in raw_items if isinstance(raw_items, list) else []:
        if not isinstance(raw, dict) or raw.get("domain") != "A_SHARES":
            continue
        moved = _beijing(raw.get("last_anomaly_time"))
        match = _CODE_RE.search(raw.get("link_url") or "") if isinstance(raw.get("link_url"), str) else None
        content = raw.get("analysis_content")
        if moved is None or moved.date() not in by_day or match is None:
            continue
        if not isinstance(content, str) or not content.strip():
            continue
        try:
            symbol = _stock_symbol(match.group(1))
        except LimitReasonsSourceError:
            continue  # B 股等非本地股票池代码
        keywords = list(dict.fromkeys(
            k.strip() for k in raw.get("key_word_list") or [] if isinstance(k, str) and k.strip()
        ))
        published = _beijing(raw.get("first_publish_time"))
        item = {
            "symbol": symbol,
            "tag": raw.get("tag_name") if isinstance(raw.get("tag_name"), str) else None,
            "tag_code": raw.get("tag_code") if isinstance(raw.get("tag_code"), str) else None,
            "keywords": keywords,
            "content": content.strip(),
            "anomaly_at": moved.isoformat(timespec="seconds"),
            "published_at": published.isoformat(timespec="seconds") if published else None,
        }
        kept = by_day[moved.date()].get(symbol)
        if kept is None or kept["anomaly_at"] < item["anomaly_at"]:
            by_day[moved.date()][symbol] = item
    return {d: list(rows.values()) for d, rows in by_day.items()}


def _chunks(days: list[date_cls]) -> list[list[date_cls]]:
    out: list[list[date_cls]] = []
    for d in sorted(days):
        if out and (d - out[-1][0]).days < _MAX_SPAN_DAYS:
            out[-1].append(d)
        else:
            out.append([d])
    return out


def _fetch(provider, data_dir: Path, days: list[date_cls], today: date_cls) -> dict[date_cls, dict]:
    """拉取并落盘 (仅历史日)。分段失败或被截断时二分重试, 单日仍失败只缺该日; 空结果不落缓存。"""
    from app.plugins.fuyao.client import FuyaoError

    fetched: dict[date_cls, dict] = {}
    pending = _chunks(days)
    while pending:
        chunk = pending.pop(0)
        try:
            data = provider.anomaly_analysis(chunk[0].isoformat(), chunk[-1].isoformat())
            if not isinstance(data, dict) or data.get("was_truncated"):
                raise FuyaoError("响应被截断或格式异常")
        except FuyaoError as exc:
            if len(chunk) > 1:
                half = len(chunk) // 2
                pending[:0] = [chunk[:half], chunk[half:]]
            else:
                logger.warning("异动解读拉取失败 %s: %s", chunk[0], exc)
            continue
        stamp = cn_now().isoformat(timespec="seconds")
        for d, items in normalize_items(data.get("items"), set(chunk)).items():
            if not items:
                continue
            payload = {"date": d.isoformat(), "fetched_at": stamp, "items": items}
            fetched[d] = payload
            if d < today:
                try:
                    path = _cache_path(data_dir, d)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    atomic_write_text(path, json.dumps(payload, ensure_ascii=False))
                except OSError:
                    logger.warning("异动解读缓存写入失败 %s", d)
    return fetched


def _safe_quotes(repo, d: date_cls) -> tuple[dict, str | None]:
    try:
        return _quotes(repo, d)
    except (OSError, ValueError, RuntimeError, pl.exceptions.PolarsError) as exc:
        logger.warning("催化验证: %s 行情不可用 (%s)", d, type(exc).__name__)
        return {}, None


def _quote(quotes: dict, symbol: str) -> dict | None:
    quote = quotes.get(symbol.split(".")[0])
    return quote if quote and quote.get("symbol") == symbol else None


def _stats(symbols: list[str], quotes: dict) -> dict | None:
    rows = [q for s in symbols if (q := _quote(quotes, s))]
    if not rows:
        return None
    return {
        "quote_count": len(rows),
        "avg_change_pct": round(math.fsum(q["change"] for q in rows) / len(rows), 6),
        "rising_count": sum(q["change"] > 0 for q in rows),
        "limit_up_count": sum(q["limit_up"] for q in rows),
    }


def _groups(items: list[dict], quotes: dict, next_date: date_cls | None, next_quotes: dict,
            history: dict[date_cls, list[dict]], lookback_complete: bool) -> list[dict]:
    members: dict[str, list[dict]] = {}
    for item in items:
        for keyword in item["keywords"]:
            members.setdefault(keyword, []).append(item)
    seen: dict[str, list[str]] = {}
    for d in sorted(history):
        for keyword in {k for item in history[d] for k in item["keywords"]}:
            seen.setdefault(keyword, []).append(d.isoformat())

    def core(item: dict | None) -> dict | None:
        return item and {k: item[k] for k in ("symbol", "name", "change_pct", "amount")}

    out = []
    for keyword, rows in members.items():
        if len(rows) < 2:
            continue
        symbols = [r["symbol"] for r in rows]
        stats = _stats(symbols, quotes)
        tagged = sum(r["tag_code"] == _LIMIT_UP_TAG for r in rows)
        # 有本地行情时以收盘涨停为准; 否则退到上游的异动标签 (可能含盘中开板)。
        limit_ups = stats["limit_up_count"] if stats else tagged
        quoted = [r for r in rows if r["change_pct"] is not None]
        nxt = _stats(symbols, next_quotes) if next_date else None
        published = [r["published_at"] for r in rows if r["published_at"]]
        prior = seen.get(keyword, [])
        out.append({
            "keyword": keyword,
            "symbols": symbols,
            "stock_count": len(rows),
            "limit_up_count": limit_ups,
            "quote_count": stats["quote_count"] if stats else 0,
            "avg_change_pct": stats["avg_change_pct"] if stats else None,
            "batch": len(rows) >= _BATCH_MIN_STOCKS and limit_ups >= _BATCH_MIN_LIMIT_UPS,
            "leader": core(max(quoted, key=lambda r: (r["change_pct"], r["amount"] or 0), default=None)),
            "anchor": core(max((r for r in quoted if r["amount"]), key=lambda r: r["amount"], default=None)),
            "seen_dates": prior,
            # 回看窗口不完整且未见过时无法断言首次出现。
            "fresh": False if prior else (True if lookback_complete else None),
            "first_published_at": min(published) if published else None,
            "next_day": nxt and {"date": next_date.isoformat(), **nxt},
        })
    return sorted(out, key=lambda g: (-g["limit_up_count"], -g["stock_count"],
                                      -(g["avg_change_pct"] or 0), g["keyword"]))


def get_catalysts(repo, target: date_cls | None = None) -> dict:
    """某交易日的催化分组。

    state: ok | source_unavailable (未配置 fuyao 且无本地缓存) | no_data (拉取失败或当日无记录)
    quote_state: ok | unavailable (缺本地日线时涨跌幅为 null, 涨停数退到上游标签)
    """
    data_dir = repo.store.data_dir
    today = cn_today()
    target = target or today
    known = _local_trading_days(data_dir)
    # 当日分区可能尚未落盘; 其余日期回退到最近的本地交易日。
    trade = target if target == today and target not in known else resolve_trade_date(data_dir, target)
    prior = [d for d in known if d < trade][-LOOKBACK_DAYS:]

    loaded: dict[date_cls, dict] = {}
    for d in [*prior, trade]:
        if d < today and (cached := _load_cache(_cache_path(data_dir, d), d)):
            loaded[d] = cached
    missing = [d for d in [*prior, trade] if d not in loaded]
    base = {"requested_date": target.isoformat(), "trade_date": trade.isoformat()}
    if missing:
        provider = _provider()
        if provider is None:
            if trade not in loaded:
                return {**base, "state": "source_unavailable"}
        else:
            loaded.update(_fetch(provider, data_dir, missing, today))
    if trade not in loaded:
        if trade == today and prior:
            # 开盘前当日尚无记录: 显示最近一个交易日, requested_date 保留原请求。
            return {**get_catalysts(repo, prior[-1]), "requested_date": target.isoformat()}
        return {**base, "state": "no_data",
                "message": "未取得该交易日的异动解读，可能是上游暂不可用或当日尚无记录。"}

    record = loaded[trade]
    quotes, quote_asof = _safe_quotes(repo, trade)
    next_date = next((d for d in known if trade < d <= today), None)
    next_quotes = _safe_quotes(repo, next_date)[0] if next_date else {}
    names = repo.get_name_map([item["symbol"] for item in record["items"]])
    items = []
    for item in record["items"]:
        quote = _quote(quotes, item["symbol"]) or {}
        following = _quote(next_quotes, item["symbol"]) or {}
        items.append({
            **item,
            "name": names.get(item["symbol"]) or item["symbol"],
            "change_pct": quote.get("change"),
            "limit_up": quote.get("limit_up"),
            "amount": quote.get("amount"),
            "next_change_pct": following.get("change"),
        })
    items.sort(key=lambda r: (r["change_pct"] is None, -(r["change_pct"] or 0), r["symbol"]))
    history = {d: loaded[d]["items"] for d in prior if d in loaded}
    return {
        **base,
        "state": "ok",
        "fetched_at": record["fetched_at"],
        "live": trade == today,
        "quote_state": "ok" if quotes else "unavailable",
        "quote_asof": quote_asof,
        "next_date": next_date.isoformat() if next_date and next_quotes else None,
        "lookback": {"days": len(prior), "cached": len(history)},
        "counts": {
            "stocks": len(items),
            "limit_up": sum(item["tag_code"] == _LIMIT_UP_TAG for item in items),
        },
        "groups": _groups(items, quotes, next_date if next_quotes else None, next_quotes,
                          history, len(history) == len(prior)),
        "items": items,
    }
