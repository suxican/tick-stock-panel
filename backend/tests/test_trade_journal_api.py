"""结清批次 + 交易记录路由: 先写记录再删批次与派生规则; 列表 / 统计 / 删除。"""
from __future__ import annotations

from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api import lots as lots_api
from app.api import trade_journal as journal_api
from app.strategy import monitor_rules
from app.strategy import trade_journal as tj


def _lot(**overrides):
    lot = {"id": "lot_test1", "symbol": "600519.SH", "qty": 100, "cost_price": 1500.0,
           "buy_date": "2026-11-05", "target_pct": 10, "stop_pct": 5,
           "remind_date": "2026-12-01", "lead_days": 2}
    lot.update(overrides)
    return lot


def _request(tmp_path: Path):
    repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        resolve_asset_type=lambda _symbol: "stock",
        get_name_map=lambda _symbols=None: {"600519.SH": "贵州茅台"},
    )
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=repo)))


@pytest.fixture
def request_(tmp_path, monkeypatch):
    reloads = []
    monkeypatch.setattr("app.services.preferences.get_webhook_default_channels", lambda: [])
    monkeypatch.setattr(lots_api, "_reload_engine", lambda r: reloads.append(1))
    monkeypatch.setattr("app.market_time.cn_today", lambda: date(2026, 11, 13))
    req = _request(tmp_path)
    req.reloads = reloads
    return req


def _close(**overrides):
    body = {"sell_price": 1537.5, "sell_date": "2026-11-13", "reason": "planned_profit", "note": " 到位 "}
    body.update(overrides)
    return lots_api.CloseLotModel(**body)


def _lot_path(tmp_path: Path, lot_id: str) -> Path:
    return tmp_path / "user_data" / "lots" / f"{lot_id}.json"


def test_close_writes_entry_then_removes_lot_and_rules(tmp_path, request_):
    lots_api.sync_lot(request_, _lot())
    before = len(request_.reloads)
    out = lots_api.close_lot("lot_test1", _close(), request_)
    entry = out["entry"]
    assert out["ok"] is True
    assert (entry["id"], entry["name"], entry["note"]) == ("tj_lot_test1", "贵州茅台", "到位")
    assert entry["pnl_pct"] == pytest.approx(2.5)
    assert entry["hold_days"] == 6 and entry["had_invalidation"] is False
    assert not _lot_path(tmp_path, "lot_test1").exists()
    assert monitor_rules.load_one(tmp_path, "lot_test1_p") is None
    assert monitor_rules.load_one(tmp_path, "lot_test1_d") is None
    assert len(request_.reloads) == before + 1
    stored = tj.load_all(tmp_path)
    assert len(stored) == 1 and "pnl_pct" not in stored[0]  # 盈亏不落盘, 读取时计算


def test_close_missing_lot_is_404_and_writes_nothing(tmp_path, request_):
    with pytest.raises(HTTPException) as exc:
        lots_api.close_lot("lot_none", _close(), request_)
    assert exc.value.status_code == 404
    assert tj.load_all(tmp_path) == []


def test_close_bad_lot_id_is_400(request_):
    with pytest.raises(HTTPException) as exc:
        lots_api.close_lot("../x", _close(), request_)
    assert exc.value.status_code == 400


@pytest.mark.parametrize("overrides, msg", [
    ({"reason": "other"}, "归因"), ({"sell_price": 0}, "卖出价"), ({"sell_date": "2026-11-14"}, "不能晚于今天"),
])
def test_close_invalid_input_is_400_and_keeps_lot(tmp_path, request_, overrides, msg):
    lots_api.sync_lot(request_, _lot())
    with pytest.raises(HTTPException) as exc:
        lots_api.close_lot("lot_test1", _close(**overrides), request_)
    assert exc.value.status_code == 400 and msg in exc.value.detail
    assert _lot_path(tmp_path, "lot_test1").exists()
    assert monitor_rules.load_one(tmp_path, "lot_test1_p") is not None
    assert tj.load_all(tmp_path) == []


def test_close_without_buy_date_succeeds_with_null_context(tmp_path, request_):
    lots_api.sync_lot(request_, _lot(buy_date=None))
    entry = lots_api.close_lot("lot_test1", _close(), request_)["entry"]
    assert entry["buy_date"] is None and entry["hold_days"] is None and entry["buy_phase"] is None


def test_close_on_non_trading_sell_date_keeps_hold_days_null(tmp_path, request_):
    lots_api.sync_lot(request_, _lot())
    entry = lots_api.close_lot("lot_test1", _close(sell_date="2026-11-08"), request_)["entry"]  # 周日补录
    assert entry["sell_date"] == "2026-11-08" and entry["hold_days"] is None


def test_close_again_after_success_is_404_and_single_entry(tmp_path, request_):
    lots_api.sync_lot(request_, _lot())
    lots_api.close_lot("lot_test1", _close(), request_)
    with pytest.raises(HTTPException) as exc:
        lots_api.close_lot("lot_test1", _close(sell_price=1.0), request_)
    assert exc.value.status_code == 404
    stored = tj.load_all(tmp_path)
    assert len(stored) == 1 and stored[0]["sell_price"] == 1537.5


def test_list_sorted_desc_with_stats_and_delete(tmp_path, request_):
    lots_api.sync_lot(request_, _lot())
    lots_api.sync_lot(request_, _lot(id="lot_test2", symbol="000001.SZ", cost_price=10.0))
    lots_api.close_lot("lot_test1", _close(sell_date="2026-11-12"), request_)
    lots_api.close_lot("lot_test2", _close(sell_price=9.5, reason="late_stop"), request_)
    out = journal_api.list_entries(request_)
    assert [e["id"] for e in out["entries"]] == ["tj_lot_test2", "tj_lot_test1"]
    assert out["entries"][0]["pnl_pct"] == pytest.approx(-5.0)
    assert (out["stats"]["total"], out["stats"]["wins"], out["stats"]["losses"]) == (2, 1, 1)

    assert journal_api.delete_entry("tj_lot_test2", request_) == {"ok": True}
    after = journal_api.list_entries(request_)
    assert after["stats"]["total"] == 1
    assert journal_api.delete_entry("tj_lot_test2", request_) == {"ok": False}


def test_list_empty(request_):
    assert journal_api.list_entries(request_) == {"entries": [], "stats": None}


@pytest.mark.parametrize("bad", ["../x", "lot_test1", "tj_", "tj_A"])
def test_delete_rejects_bad_id(request_, bad):
    with pytest.raises(HTTPException) as exc:
        journal_api.delete_entry(bad, request_)
    assert exc.value.status_code == 400
