"""批次同步仅建立待核对持仓，不推测可卖库存、不覆盖已有账本。"""
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_position_t_store import NOW, account, plan, position

from app.api.position_t import router
from app.services.position_t_service import PositionTService
from app.services.position_t_store import LedgerError
from app.strategy import lots


class Repo:
    def get_instruments(self):
        return pl.DataFrame({"symbol": ["600000.SH", "688388.SH", "300001.SZ", "510300.SH"],
                             "name": ["样本主板", "样本科创", "样本创业", "样本基金"]})

    def resolve_asset_type(self, symbol):
        return "etf" if symbol == "510300.SH" else "stock"


@pytest.fixture
def service(tmp_path):
    return PositionTService(tmp_path, SimpleNamespace(repo=Repo()), clock=lambda: NOW)


def save_lot(service, lot_id="lot_a", symbol="600000.SH", qty=100, cost=20):
    lots.save_one(service.data_dir, {"id": lot_id, "symbol": symbol, "qty": qty, "cost_price": cost})


def sync(service, aid, version=None):
    return service.sync_lots(aid, service.store.get_account(aid)["version"] if version is None else version)


def test_aggregate_lots_preserves_unknown_inventory_and_cash(service):
    aid = account(service.store)["id"]
    before = service.store.get_account(aid)
    save_lot(service, qty=100.0, cost=20)
    save_lot(service, "lot_b", qty=300, cost=40)
    result = sync(service, aid)
    assert result["created"] == [{"symbol": "600000.SH", "name": "样本主板", "qty": 400}]
    assert result["skipped"] == []
    pos = service.store.get_position(aid, "600000.SH")
    assert pos["cost_price"] == 35
    assert pos["cost_method"] == "manual_reference"
    assert pos["source"] == "lots"
    assert pos["source_synced_at"] == NOW.isoformat()
    assert pos["source_lot_ids"] == ["lot_a", "lot_b"]
    assert pos["qty"] == pos["core_qty"] == pos["target_qty"] == 400
    assert pos["sellable_qty"] is pos["effective_sellable"] is pos["day_initial_sellable"] is None
    assert pos["day_inventory_basis"] == "unknown"
    assert not pos["enabled"] and not pos["reconciled"]
    assert pos["max_t_qty"] == pos["max_extra_qty"] == pos["reserved_qty"] == 0
    after = service.store.get_account(aid)
    for key in ("available_cash", "broker_frozen_cash", "pending_proceeds", "reserved_cash", "baseline_cash", "benchmark_valid", "config"):
        assert after[key] == before[key]


def test_repeated_sync_never_duplicates_events_or_overwrites_existing(service):
    aid = account(service.store)["id"]
    save_lot(service)
    sync(service, aid)
    before_pos, before_acc = service.store.get_position(aid, "600000.SH"), service.store.get_account(aid)
    save_lot(service, qty=900, cost=99)
    result = sync(service, aid)
    assert result["created"] == []
    assert "已有" in result["skipped"][0]["reason"]
    assert service.store.get_position(aid, "600000.SH") == before_pos
    assert service.store.get_account(aid) == before_acc


@pytest.mark.parametrize("qty,cost", [(0, 20), (-1, 20), (1.5, 20), (True, 20),
                                      (float("inf"), 20), (100, float("nan")), (100, 0), (100, True),
                                      ("100", 20), (100, "20")])
def test_invalid_lot_prevents_partial_symbol_import(service, qty, cost):
    aid = account(service.store)["id"]
    save_lot(service, "lot_good", qty=100, cost=20)
    save_lot(service, "lot_bad", qty=qty, cost=cost)
    result = sync(service, aid)
    assert result["created"] == []
    assert len(result["skipped"]) == 1
    assert service.store.list_positions(aid) == []


def test_only_catalogued_stocks_import_and_board_is_correct(service):
    aid = account(service.store)["id"]
    for i, symbol in enumerate(["688388.SH", "300001.SZ", "510300.SH", "999999.SH", "invalid"]):
        save_lot(service, f"lot_{i}", symbol=symbol)
    result = sync(service, aid)
    assert {p["symbol"] for p in result["created"]} == {"688388.SH", "300001.SZ"}
    assert {p["symbol"] for p in result["skipped"]} == {"510300.SH", "999999.SH", "INVALID"}
    assert service.store.get_position(aid, "688388.SH")["board"] == "star"
    assert service.store.get_position(aid, "300001.SZ")["board"] == "chinext"


def test_explicit_nonstock_catalog_type_is_rejected_even_if_resolver_defaults_stock(service):
    aid = account(service.store)["id"]
    service.market.repo.get_instruments = lambda: pl.DataFrame({
        "symbol": ["600000.SH"], "name": ["非股票样本"], "asset_type": ["index"], "type": ["stock"],
    })
    save_lot(service)
    result = sync(service, aid)
    assert result["created"] == []
    assert "证券类型" in result["skipped"][0]["reason"]


def test_active_plan_resources_and_existing_position_unchanged(service):
    aid = account(service.store)["id"]
    position(service.store, aid)
    active = plan(service.store, aid)
    before_pos, before_acc = service.store.get_position(aid, "600000.SH"), service.store.get_account(aid)
    save_lot(service, qty=200)
    save_lot(service, "lot_new", symbol="688388.SH", qty=400)
    result = sync(service, aid)
    assert len(result["created"]) == len(result["skipped"]) == 1
    assert service.store.get_plan(active["id"]) == active
    assert service.store.get_position(aid, "600000.SH") == before_pos
    after = service.store.get_account(aid)
    assert not after["benchmark_valid"]
    for key in ("available_cash", "effective_cash", "broker_frozen_cash", "pending_proceeds", "reserved_cash"):
        assert after[key] == before_acc[key]


def test_concurrent_sync_uses_account_version_and_single_insert(service):
    aid = account(service.store)["id"]
    save_lot(service)
    version = service.store.get_account(aid)["version"]
    other_service = PositionTService(service.data_dir, service.market, clock=lambda: NOW)

    def run(target):
        try:
            return sync(target, aid, version)
        except LedgerError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, [service, other_service]))
    assert sum(isinstance(r, dict) for r in results) == 1
    assert "version_conflict" in results
    assert len(service.store.list_positions(aid)) == 1


def test_api_sync_contract_and_empty_result(service):
    aid = account(service.store)["id"]
    app = FastAPI()
    app.state.position_t_service = service
    app.include_router(router)
    client = TestClient(app)
    url = f"/api/position-t/accounts/{aid}/sync-lots"
    version = service.store.get_account(aid)["version"]
    assert client.post(url, json={}).status_code == 422
    assert client.post(url, json={"expected_version": True}).status_code == 422
    assert client.post(url, json={"expected_version": version, "source": "broker"}).status_code == 422
    empty = client.post(url, json={"expected_version": version})
    assert empty.status_code == 200
    assert empty.json() == {"created": [], "skipped": [], "account_version": version}
    save_lot(service)
    response = client.post(url, json={"expected_version": version})
    assert response.status_code == 200, response.text
    assert response.json()["created"][0]["qty"] == 100
    assert client.post(url, json={"expected_version": version}).status_code == 409
    assert client.post("/api/position-t/accounts/missing/sync-lots", json={"expected_version": 0}).status_code == 404


def test_catalog_failure_does_not_write_partial_results(service):
    aid = account(service.store)["id"]
    save_lot(service)
    before = service.store.get_account(aid)

    def unavailable():
        raise OSError("unavailable")

    service.market.repo.get_instruments = unavailable
    with pytest.raises(LedgerError, match="证券目录"):
        sync(service, aid)
    assert service.store.list_positions(aid) == []
    assert service.store.get_account(aid) == before


def test_write_failure_rolls_back_all_positions_events_and_versions(service, monkeypatch):
    aid = account(service.store)["id"]
    save_lot(service)
    save_lot(service, "lot_other", symbol="688388.SH", qty=400)
    before = service.store.get_account(aid)
    original = service.store._save_position

    def fail_second(db, value):
        if value["symbol"] == "688388.SH":
            raise OSError("simulated write failure")
        original(db, value)

    monkeypatch.setattr(service.store, "_save_position", fail_second)
    with pytest.raises(OSError, match="simulated write"):
        sync(service, aid)
    assert service.store.list_positions(aid) == []
    assert service.store.get_account(aid) == before
    with service.store._tx() as db:
        assert db.execute("SELECT count(*) FROM events WHERE account_id=?", (aid,)).fetchone()[0] == 1


def test_prior_observation_invalidates_benchmark_for_new_unknown_position(service):
    aid = account(service.store)["id"]
    service.store.save_observation(aid, "600000.SH", {"computed_at": NOW.isoformat()})
    save_lot(service)
    sync(service, aid)
    assert not service.store.get_account(aid)["benchmark_valid"]


@pytest.mark.parametrize("field,value", [("source", "lots"), ("source_synced_at", NOW.isoformat()),
                                         ("source_lot_ids", ["lot_forged"])])
def test_normal_position_update_cannot_forge_provenance(service, field, value):
    aid = account(service.store)["id"]
    with pytest.raises(LedgerError, match="内部派生字段"):
        position(service.store, aid, **{field: value})
    assert service.store.list_positions(aid) == []


def test_manual_reconciliation_retains_import_provenance(service):
    aid = account(service.store)["id"]
    save_lot(service)
    sync(service, aid)
    before = service.store.get_position(aid, "600000.SH")
    updated = service.store.upsert_position(aid, "600000.SH", {
        "reconciled": True, "sellable_qty": 100, "expected_version": before["version"],
        "included_event_seq": service.store.get_account(aid)["event_seq"],
    }, now=NOW)
    for key in ("source", "source_synced_at", "source_lot_ids"):
        assert updated[key] == before[key]
    assert updated["reconciled"] and updated["sellable_qty"] == 100
