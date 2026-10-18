"""持仓做 T 行情边界：复用行情/分钟/盘口服务，不从页面或 AI 取数。"""
from __future__ import annotations

import math
from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from statistics import median

from app.market_time import CN_TZ
from app.services.huichun_calendar import BUNDLED_CALENDAR_PATH, load_market_calendar
from app.strategy.position_t import as_cn, continuous_session


def market_time_ms(value):
    """标准行情 timestamp 契约是 Unix 毫秒；不把接收时间冒充市场时间。"""
    if not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    try:
        return datetime.fromtimestamp(value / 1000, CN_TZ)
    except (OverflowError, OSError, ValueError):
        return None


def normalize_bars(rows, now, semantics):
    if semantics not in {"start", "end"}:
        return []
    result = []
    for row in rows:
        stamp = row.get("datetime")
        if stamp is None:
            continue
        end = as_cn(stamp) + (timedelta(minutes=1) if semantics == "start" else timedelta())
        t = end.time()
        if end.date() != now.date() or end > now or not (time(9, 31) <= t <= time(11, 30) or time(13, 1) <= t <= time(14, 57)):
            continue
        result.append({"end": end.isoformat(), **{k: row.get(k) for k in ("open", "high", "low", "close", "amount")},
                       "volume_shares": row["volume"] * 100 if row.get("volume") is not None else None})
    return sorted(result, key=lambda b: b["end"])


class PositionTMarket:
    def __init__(self, repo, quote_service, depth_service, capset):
        from app.services.sector_monitor import SectorMonitorService

        self.repo, self.quotes, self.depth, self.capset = repo, quote_service, depth_service, capset
        self.sectors = SectorMonitorService(repo)
        self._minute_key = None
        self._minutes = {}
        self._minute_received = None
        self._stats = {}
        self._instruments = {}
        self._instrument_day = None
        self._metadata_at = None
        self._sessions = {}

    def sessions(self, first: date, last: date):
        return load_market_calendar(BUNDLED_CALENDAR_PATH, start=first, end=last)

    def trading_day(self, day):
        if day not in self._sessions:
            try:
                self._sessions = {day: day in self.sessions(day, day)}
            except ValueError:
                return None
        return self._sessions[day]

    def capabilities(self):
        from app.services.kline_sync import intraday_monitor_support

        support = intraday_monitor_support(self.capset)
        return {"manual": True, "simulation": True, "broker": False, "auto_order": False,
                "minute": support, "replay_source": "recorded_observations",
                "reasons": ["仅人工或模拟登记；不连接券商", "缺少市场时间、盘口、证券状态或完整分钟时暂停判断",
                            "分钟时间语义须按数据源确认；历史回放只使用本功能已留存的当时证据"]}

    def _history(self, symbol, day):
        key = (symbol, day)
        if key not in self._stats:
            if any(k[1] != day for k in self._stats):
                self._stats.clear()
            df = self.repo.get_daily(symbol, day - timedelta(days=90), day - timedelta(days=1), columns=["date", "volume"])
            rows = df.to_dicts()
            volumes = [float(r["volume"]) * 100 / 240 for r in rows if r.get("volume") and math.isfinite(float(r["volume"]))]
            self._stats[key] = {"history_days": len(volumes), "median_minute_volume_shares": median(volumes) if volumes else None}
        return self._stats[key]

    def _metadata(self, now):
        if self._instrument_day != now.date() or self._metadata_at is None or (now - self._metadata_at).total_seconds() >= 300:
            df = self.repo.get_instruments()
            self._instruments = {r["symbol"]: r for r in df.to_dicts()}
            self._instrument_day = now.date()
            self._metadata_at = now
            self._stats.clear()

    def _quote(self, position, raw, book, now, config):
        from app.price_limits import (
            is_no_limit_day,
            is_risk_warning_name,
            parse_listing_date,
            price_limit_pct,
        )

        errors = []
        symbol = position["symbol"]
        stamp, received = market_time_ms(raw.get("timestamp")), market_time_ms(raw.get("received_ts"))
        book_stamp = market_time_ms(book.get("timestamp"))
        if not book_stamp or not 0 <= (now - book_stamp).total_seconds() <= config["quote_max_age_seconds"]:
            errors.append("盘口时间缺失或过期")
        if stamp and book_stamp and abs((stamp - book_stamp).total_seconds()) > config["quote_max_age_seconds"]:
            errors.append("盘口与成交报价时间不匹配")
        instrument = self._instruments.get(symbol, {})
        listing = parse_listing_date(instrument.get("listing_date"))
        if not instrument or listing is None:
            errors.append("证券元数据或上市日期未确认")
        inferred_board = "bse" if symbol.endswith(".BJ") else "star" if symbol.startswith(("688", "689")) else "chinext" if symbol.startswith(("300", "301")) else "main"
        if position["board"] != inferred_board or self.repo.resolve_asset_type(symbol) != "stock":
            errors.append("持仓板块或证券类型与证券目录不一致")
        if is_no_limit_day(symbol, listing, now.date()):
            errors.append("上市初期无涨跌幅窗口首版暂停新建计划")
        previous = raw.get("prev_close")
        upper = lower = None
        if isinstance(previous, (int, float)) and math.isfinite(previous) and previous > 0:
            ratio = Decimal(str(price_limit_pct(symbol, now.date(), is_risk_warning=is_risk_warning_name(instrument.get("name")))))
            upper, lower = [float((Decimal(str(previous)) * (1 + sign * ratio)).quantize(Decimal(".01"), rounding=ROUND_HALF_UP)) for sign in (1, -1)]
        actual_up, actual_down = raw.get("limit_up_price"), raw.get("limit_down_price")
        if all(isinstance(v, (int, float)) and math.isfinite(v) and v > 0 for v in (actual_up, actual_down)) and actual_up > actual_down:
            upper, lower = actual_up, actual_down
        elif raw.get("price_limit_rule") != "standard":
            errors.append("当日涨跌停范围未获数据源确认；理论价仅作观察")
        if raw.get("no_price_limit") is True:
            errors.append("当日无涨跌幅限制，首版暂停新建计划")
        # 标准插件可提供明确 trading/suspended；未知状态不能从有价格或有量推断。
        status = raw.get("trade_status") or book.get("trade_status")
        if status not in {"trading", "suspended", "delisted"}:
            status = "unknown"
            errors.append("数据源未提供可核验的证券交易状态")
        def top(key):
            values = book.get(key) or []
            return values[0] if values else None
        return {"source": raw.get("source"), "price": raw.get("last_price"), "previous_close": previous,
                "market_ts": stamp.isoformat() if stamp else None, "received_ts": received.isoformat() if received else None,
                "computed_at": now.isoformat(), "book_market_ts": book_stamp.isoformat() if book_stamp else None,
                "bid": top("bid_prices"), "ask": top("ask_prices"), "trade_status": status,
                "is_trading_day": self.trading_day(now.date()), "limit_up_price": upper, "limit_down_price": lower,
                "unit": {"price": "CNY", "volume": "shares", "amount": "CNY"},
                "minute_received_at": self._minute_received, "data_errors": errors, **self._history(symbol, now.date())}

    def collect(self, positions, accounts, now):
        """每轮一批；分钟每分钟一次，标的按未完成计划优先由调用方排序。"""
        import polars as pl

        from app.services import preferences
        from app.services.kline_sync import fetch_intraday_monitor_batch, intraday_monitor_support

        status = self.quotes.status()
        enabled = status.get("enabled") and not status.get("paused") and status.get("realtime_allowed")
        trading = self.trading_day(now.date()) is True and continuous_session(now)
        support = intraday_monitor_support(self.capset)
        if preferences.get_minute_data_provider() != "tickflow":
            from app.data_providers.capabilities import capability_gate

            usable, label = capability_gate("minute")
            if not usable:
                support = {"available": False, "max_symbols": 0, "reason": f"{label}不可用，不跨源回退"}
        self._metadata(now)
        symbols = list(dict.fromkeys(p["symbol"] for p in positions))
        selected = symbols[:int(support["max_symbols"])] if support["available"] else []
        records = self.quotes.get_market_records()
        # 成分股只有真实市场时间新鲜才参与聚合，不用接收时间替代。
        fresh = [r for r in records.values() if (s := market_time_ms(r.get("timestamp"))) and 0 <= (now - s).total_seconds() <= 10]
        targets = [{"key": key} for key in {*(f"index:{p['benchmark_symbol']}" for p in positions), *(p["sector_id"] for p in positions if p.get("sector_id"))}]
        columns = ["symbol", "change_pct", "last_price", "prev_close"]
        stock_rows = [{k: r.get(k) for k in columns} for r in fresh]
        index_rows = [{**r, "change_pct": r["change_pct"] * 100 if r.get("change_pct") is not None else None} for r in stock_rows]
        snapshots = self.sectors.build_snapshots(pl.DataFrame(stock_rows), pl.DataFrame(index_rows), targets, {5}, now=now.timestamp()) if trading and enabled else {}
        books = self.depth.get_order_books(selected) if trading and enabled and selected else {}
        minute_slot = (now - timedelta(seconds=3)).replace(second=0, microsecond=0).isoformat()
        key = (minute_slot, tuple(selected), preferences.get_minute_data_provider())
        if trading and enabled and selected and key != self._minute_key:
            # 失败也记本轮键，不以每 5 秒重试放大源频率；旧缓存不能冒充新数据。
            self._minute_key, self._minutes = key, {}
            frame = fetch_intraday_monitor_batch(selected, self.capset, now=now.replace(tzinfo=None))
            for row in frame.to_dicts():
                self._minutes.setdefault(row["symbol"], []).append(row)
            self._minute_received = datetime.now(CN_TZ).isoformat()
        result = {}
        worst_ts = min((market_time_ms(r["timestamp"]) for r in fresh), default=None)
        for pos in positions:
            cfg = accounts[pos["account_id"]]["config"]
            quote = self._quote(pos, records.get(pos["symbol"], {}), books.get(pos["symbol"], {}), now, cfg)
            if not enabled:
                quote["data_errors"].append("全局实时行情未启用或已暂停")
            if pos["symbol"] not in selected:
                quote["data_errors"].append("分钟能力不可用或本轮监控容量不足")
            if cfg.get("minute_timestamp_semantics") not in {"start", "end"}:
                quote["data_errors"].append("分钟时间标记语义待按数据源确认")
            market = snapshots.get(f"index:{pos['benchmark_symbol']}", {})
            sector = snapshots.get(pos.get("sector_id"), {})
            idx_stamp = market_time_ms(records.get(pos["benchmark_symbol"], {}).get("timestamp"))
            env = {"market_return_5m": market.get("window_changes", {}).get(5),
                   "sector_return_5m": sector.get("window_changes", {}).get(5), "coverage": sector.get("coverage_ratio", 0),
                   "market_ts": idx_stamp.isoformat() if idx_stamp and market.get("valid") else None,
                   "sector_ts": worst_ts.isoformat() if worst_ts and sector.get("valid") else None,
                   "window_metric": "daily_return_difference",
                   "benchmark_symbol": pos["benchmark_symbol"], "sector_id": pos.get("sector_id")}
            bars = normalize_bars(self._minutes.get(pos["symbol"], []), now, cfg.get("minute_timestamp_semantics")) if enabled and trading else []
            result[(pos["account_id"], pos["symbol"])] = {"bars": bars, "quote": quote, "environment": env}
        return result
