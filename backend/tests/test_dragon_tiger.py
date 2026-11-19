"""龙虎榜服务测试 (不依赖真实网络)。

覆盖: 交易日回退 (非交易日/目标日解析)、历史日 JSON 缓存命中与落盘、
无可用数据源降级、当日未发布 fallback_prev、彻底失败 no_data、
兜底链 (主源失败换源并标注、主源「无数据」不换源)、缓存来源、AI 复盘摘要段、
thscode 归一化。日期用 2026-08-26/27/28 (过去交易日)。
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.data_providers import routing_health as rh
from app.services import dragon_tiger as dt


def _mk_days(data_dir: Path, *days: str) -> None:
    for d in days:
        (data_dir / "kline_daily" / f"date={d}").mkdir(parents=True, exist_ok=True)


@pytest.fixture()
def data_dir(tmp_path: Path) -> Path:
    _mk_days(tmp_path, "2026-08-26", "2026-08-27", "2026-08-28")
    return tmp_path


def _payload(iso: str, **extra) -> dict:
    items = [
        {"thscode": "600519.SH", "ticker": "600519", "name": "贵州茅台",
         "change": 0.05, "net_value": 1.5e8, "net_rate": 0.01,
         "buy_value": 2e8, "sell_value": 0.5e8, "hot_rank": 3, "range_days": 1},
        {"thscode": "000858.SZ", "ticker": "000858", "name": "五粮液",
         "change": -0.03, "net_value": -0.6e8, "net_rate": -0.004,
         "buy_value": 0.4e8, "sell_value": 1e8, "range_days": 3},
    ]
    org = [{**items[0], "org_net_value": 0.8e8}]
    return {
        "trade_date": iso,
        "all": {"trade_date": iso, "stock_count": 2, "count": 2, "stock_items": items},
        "org": {"trade_date": iso, "stock_count": 1, "count": 1, "stock_items": org},
        "hot_money": {"trade_date": iso, "count": 1, "hot_money_items": [
            {"name": "宁波桑田路", "buying": 1.9e8,
             "rows": [{"thscode": "600519.SH", "name": "贵州茅台", "hot_money_item_net_value": 1.2e8}]},
        ]},
        **extra,
    }


class _FakeProvider:
    """get_dragon_tiger(day): fail_dates 抛错, none_dates 返回 None (该日无数据)。"""

    def __init__(self, fail_dates=(), none_dates=(), extra=None):
        self.calls: list[str] = []
        self.fail_dates, self.none_dates, self.extra = set(fail_dates), set(none_dates), extra or {}

    def get_dragon_tiger(self, day: date):
        iso = day.isoformat()
        self.calls.append(iso)
        if iso in self.fail_dates:
            raise RuntimeError(f"{iso} 请求失败")
        if iso in self.none_dates:
            return None
        return _payload(iso, **self.extra)


@pytest.fixture()
def chain(monkeypatch):
    """兜底链打桩: 主源 fuyao, 兜底 meoz; providers 可在用例中替换。"""
    rh.reset()
    state = SimpleNamespace(primary="fuyao", fallbacks=["meoz"],
                            providers={"fuyao": _FakeProvider(),
                                       "meoz": _FakeProvider(extra={"hot_money_basis": "meoz_seat_match"})})
    monkeypatch.setattr(rh, "_primary", lambda cap_id: state.primary)
    monkeypatch.setattr(rh, "_fallbacks", lambda cap_id: list(state.fallbacks))
    monkeypatch.setattr(rh, "_candidate_names", lambda cap_id: set(state.providers))
    monkeypatch.setattr(rh, "resolve_provider", lambda name: state.providers[name])
    monkeypatch.setattr(rh, "_display", lambda name: {"fuyao": "fuyao", "meoz": "猫爪数据"}.get(name, name))
    yield state
    rh.reset()


# ---- 交易日解析 ----

def test_resolve_rolls_back_non_trading_day(data_dir):
    assert dt.resolve_trade_date(data_dir, date(2026, 8, 30)) == date(2026, 8, 28)
    assert dt.resolve_trade_date(data_dir, date(2026, 8, 27)) == date(2026, 8, 27)


def test_resolve_older_than_all_partitions_returns_target(data_dir):
    assert dt.resolve_trade_date(data_dir, date(2020, 1, 1)) == date(2020, 1, 1)


# ---- 状态与缓存 ----

def test_empty_chain_returns_source_unavailable(data_dir, chain):
    chain.providers = {}
    assert dt.get_dragon_tiger(data_dir, None)["state"] == "source_unavailable"


def test_fetch_stores_cache_then_hits_cache(data_dir, chain):
    fuyao = chain.providers["fuyao"]
    out = dt.get_dragon_tiger(data_dir, None)  # 默认 → 最近分区 08-28
    assert out["state"] == "ok" and out["trade_date"] == "2026-08-28"
    assert fuyao.calls == ["2026-08-28"]   # 三榜由插件一次取齐
    assert (data_dir / "dragon_tiger" / "date=2026-08-28.json").exists()
    fuyao.calls.clear()
    out2 = dt.get_dragon_tiger(data_dir, date(2026, 8, 30))  # 周日 → 08-28 → 命中缓存
    assert out2["state"] == "ok" and out2["trade_date"] == "2026-08-28"
    assert fuyao.calls == []


def test_explicit_history_date_uses_cache(data_dir, chain):
    fuyao = chain.providers["fuyao"]
    dt.get_dragon_tiger(data_dir, date(2026, 8, 27))
    assert fuyao.calls == ["2026-08-27"]
    fuyao.calls.clear()
    dt.get_dragon_tiger(data_dir, date(2026, 8, 27))
    assert fuyao.calls == []


def test_unpublished_day_falls_back_to_prev(data_dir, chain):
    chain.providers["fuyao"] = _FakeProvider(none_dates={"2026-08-28"})
    out = dt.get_dragon_tiger(data_dir, date(2026, 8, 28))
    assert out["state"] == "fallback_prev"
    assert out["trade_date"] == "2026-08-27" and out["requested_date"] == "2026-08-28"
    cached = json.loads((data_dir / "dragon_tiger" / "date=2026-08-27.json").read_text(encoding="utf-8"))
    assert cached["state"] == "ok"   # 回退日缓存以 ok 落盘, 不污染直查


def test_primary_none_does_not_switch_and_falls_back_prev(data_dir, chain):
    chain.providers["fuyao"] = _FakeProvider(none_dates={"2026-08-28"})
    meoz = chain.providers["meoz"]
    out = dt.get_dragon_tiger(data_dir, date(2026, 8, 28))
    assert out["state"] == "fallback_prev" and out["source"] == "fuyao"
    assert meoz.calls == []          # 主源「无数据」不是失败, 不去兜底源取


def test_all_fail_returns_no_data(data_dir, chain):
    for name in ("fuyao", "meoz"):
        chain.providers[name] = _FakeProvider(fail_dates={"2026-08-28", "2026-08-27"})
    out = dt.get_dragon_tiger(data_dir, date(2026, 8, 28))
    assert out["state"] == "no_data"
    assert out.get("message")


def test_corrupt_cache_refetches(data_dir, chain):
    cache = data_dir / "dragon_tiger" / "date=2026-08-28.json"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text("{broken json", encoding="utf-8")
    out = dt.get_dragon_tiger(data_dir, date(2026, 8, 28))
    assert out["state"] == "ok"
    assert chain.providers["fuyao"].calls


# ---- 兜底链与来源 ----

def test_primary_ok_marks_source_not_degraded(data_dir, chain):
    out = dt.get_dragon_tiger(data_dir, date(2026, 8, 28))
    assert (out["source"], out["source_display"], out["degraded"]) == ("fuyao", "fuyao", False)
    assert out["fallback_reason"] is None and out["hot_money_basis"] is None


def test_primary_error_falls_back_to_meoz_and_marks_degraded(data_dir, chain):
    chain.providers["fuyao"] = _FakeProvider(fail_dates={"2026-08-28"})
    out = dt.get_dragon_tiger(data_dir, date(2026, 8, 28))
    assert out["state"] == "ok"
    assert (out["source"], out["source_display"], out["degraded"]) == ("meoz", "猫爪数据", True)
    assert out["fallback_reason"]
    assert out["hot_money_basis"] == "meoz_seat_match"


def test_cache_stores_source_and_basis(data_dir, chain):
    chain.providers["fuyao"] = _FakeProvider(fail_dates={"2026-08-28"})
    dt.get_dragon_tiger(data_dir, date(2026, 8, 28))
    cached = json.loads((data_dir / "dragon_tiger" / "date=2026-08-28.json").read_text(encoding="utf-8"))
    assert cached["source"] == "meoz" and cached["hot_money_basis"] == "meoz_seat_match"
    assert "degraded" not in cached and "fallback_reason" not in cached   # 运行时字段不落盘


def test_legacy_cache_reads_as_fuyao(data_dir, chain):
    path = data_dir / "dragon_tiger" / "date=2026-08-27.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"state": "ok", **_payload("2026-08-27")}, ensure_ascii=False), encoding="utf-8")
    chain.primary, chain.fallbacks = "meoz", ["fuyao"]
    out = dt.get_dragon_tiger(data_dir, date(2026, 8, 27))
    assert out["source"] == "fuyao"
    assert out["degraded"] is True      # 当前主源已是猫爪, 这份历史数据来自 fuyao
    assert chain.providers["meoz"].calls == []


def test_cached_meoz_day_marked_degraded_when_primary_fuyao(data_dir, chain):
    path = data_dir / "dragon_tiger" / "date=2026-08-27.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"state": "ok", "source": "meoz", "hot_money_basis": "meoz_seat_match",
                                **_payload("2026-08-27")}, ensure_ascii=False), encoding="utf-8")
    out = dt.get_dragon_tiger(data_dir, date(2026, 8, 27))
    assert (out["source"], out["degraded"], out["fallback_reason"]) == ("meoz", True, None)


# ---- AI 复盘摘要 ----

def test_build_recap_context_contains_summary(data_dir, chain):
    ctx = dt.build_recap_context(data_dir)
    assert "净买入居前" in ctx and "贵州茅台" in ctx
    assert "机构净买居前" in ctx
    assert "宁波桑田路" in ctx


def test_recap_context_names_source_and_hot_money_basis(data_dir, chain):
    chain.providers["fuyao"] = _FakeProvider(fail_dates={"2026-08-28"})
    ctx = dt.build_recap_context(data_dir)
    assert ctx.splitlines()[0] == "(数据日期: 2026-08-28；来源: 猫爪数据)"
    assert "按猫爪游资名单匹配" in ctx


def test_build_recap_context_empty_without_source(data_dir, chain):
    chain.providers = {}
    assert dt.build_recap_context(data_dir) == ""


# ---- thscode 归一化 (2026-10-29 用户反馈: 退市整理期股/B股 thscode=null 进前端) ----

def test_derive_thscode_market_suffix():
    assert dt.derive_thscode("002731") == "002731.SZ"
    assert dt.derive_thscode("300750") == "300750.SZ"
    assert dt.derive_thscode("600519") == "600519.SH"
    assert dt.derive_thscode("688111") == "688111.SH"
    assert dt.derive_thscode("830799") == "830799.BJ"
    assert dt.derive_thscode("900920") == "900920.SH"   # 沪B
    assert dt.derive_thscode("200468") == "200468.SZ"   # 深B
    assert dt.derive_thscode("600519.SH") == "600519.SH"
    assert dt.derive_thscode(None) is None
    assert dt.derive_thscode("ABCDEF") is None


def test_normalize_stock_items_fills_null_thscode_from_ticker():
    items = [
        {"thscode": None, "ticker": "002731", "name": "萃华退", "net_value": -525558.3},
        {"thscode": None, "ticker": "200468", "name": "宁通信B"},
        {"thscode": "600519.SH", "ticker": "600519", "name": "贵州茅台"},
    ]
    out = dt.normalize_stock_items(items)
    assert [i["thscode"] for i in out] == ["002731.SZ", "200468.SZ", "600519.SH"]
    assert out[0]["name"] == "萃华退" and out[0]["net_value"] == -525558.3


def test_normalize_stock_items_drops_unidentifiable():
    out = dt.normalize_stock_items([
        {"thscode": None, "ticker": None, "name": "无代码"},
        {"thscode": "  ", "ticker": "nope", "name": "坏ticker"},
        "junk",
    ])
    assert out == []


def test_load_cache_normalizes_legacy_null_thscode(tmp_path):
    """旧版本写入的历史缓存含 null thscode → 读回即归一, 不把脏数据透给前端。"""
    path = tmp_path / "date=2026-10-29.json"
    path.write_text(json.dumps({
        "state": "ok",
        "trade_date": "2026-10-29",
        "all": {"stock_items": [{"thscode": None, "ticker": "002731", "name": "萃华退"}]},
        "org": {"stock_items": [{"thscode": "600519.SH"}]},
        "hot_money": {"hot_money_items": [
            {"name": "T王", "rows": [{"thscode": None, "ticker": "200468"}]},
        ]},
    }, ensure_ascii=False), encoding="utf-8")
    payload = dt._load_cache(path)
    assert payload["all"]["stock_items"][0]["thscode"] == "002731.SZ"
    assert payload["org"]["stock_items"][0]["thscode"] == "600519.SH"
    assert payload["hot_money"]["hot_money_items"][0]["rows"][0]["thscode"] == "200468.SZ"


def test_get_dragon_tiger_normalizes_provider_null_thscode(data_dir, chain):
    class _NullProvider:
        def get_dragon_tiger(self, day):
            iso = day.isoformat()
            return {
                "trade_date": iso,
                "all": {"trade_date": iso, "stock_count": 1, "count": 1,
                        "stock_items": [{"thscode": None, "ticker": "002731", "name": "萃华退",
                                         "net_value": -525558.3}]},
                "org": {"trade_date": iso, "stock_items": []},
                "hot_money": {"trade_date": iso, "hot_money_items": [
                    {"name": "T王", "buying": 1.0, "rows": [{"thscode": None, "ticker": "900920"}]}]},
            }

    chain.providers["fuyao"] = _NullProvider()
    payload = dt.get_dragon_tiger(data_dir, date(2026, 8, 28))
    assert payload["state"] == "ok"
    assert payload["all"]["stock_items"][0]["thscode"] == "002731.SZ"
    assert payload["hot_money"]["hot_money_items"][0]["rows"][0]["thscode"] == "900920.SH"
