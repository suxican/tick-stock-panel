"""Trade validation is explicit, bounded, read-only on evidence and separately saved."""
import json
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.first_board import router
from app.market_time import CN_TZ
from app.services import first_board as fb


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(fb, "_now", lambda: datetime(2026, 10, 22, 16, tzinfo=CN_TZ))
    app = FastAPI()
    app.include_router(router)
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    app.state.first_board_service = fb.FirstBoardService(repo, history_loader=lambda *_: pl.DataFrame())
    return TestClient(app)


def body(**changes):
    return {"mode": "replay", "start": "2026-10-19", "end": "2026-10-21", **changes}


@pytest.mark.parametrize("changes", [
    {"mode": "unknown"}, {"start": "2026-10-22"}, {"revision": -1},
    {"revision": True}, {"extra": True}, {"options": {"slippage_bps": -1}},
    {"mode": "paper", "options": {"amount": 10000}},
    {"account_id": "default"}, {"mode": "paper", "account_id": "../escape"},
])
def test_trade_validation_rejects_invalid_contract(client, changes):
    response = client.post("/api/first-board/trade-validation", json=body(**changes))
    assert response.status_code == 422, response.text


@pytest.mark.parametrize("changes", [
    {"end": "2026-10-22"}, {"mode": "paper", "end": "2026-10-23"},
    {"start": "2026-01-01"}, {"revision": 999},
])
def test_trade_validation_rejects_incomplete_or_unbounded_research(client, changes):
    response = client.post("/api/first-board/trade-validation", json=body(**changes))
    assert response.status_code == 400, response.text
    assert not (client.app.state.first_board_service.root / "trade-validation").exists()


def test_replay_freezes_request_and_saves_separately_from_daily_research(client, monkeypatch):
    from app.backtest import first_board_execution as execution

    calls = []
    real_run = execution.run_execution_validation

    def run(repo, store, **kwargs):
        calls.append(kwargs)
        result = real_run(repo, store, **kwargs)
        result["fingerprint"] = "input-digest"
        return result

    monkeypatch.setattr(execution, "run_execution_validation", run)
    response = client.post("/api/first-board/trade-validation", json=body())
    assert response.status_code == 200, response.text
    result = response.json()
    assert calls[0]["through"] == date(2026, 10, 21)
    assert calls[0]["revision"] == 0
    assert calls[0]["options"].amount == 10000
    assert result["request"]["options"]["participation_rate"] == .01
    assert result["kind"] == "replay" and result["fingerprint"] == "input-digest"
    runs = client.get("/api/first-board/trade-validation-runs").json()["runs"]
    assert len(runs) == 1 and runs[0]["result"] == result
    assert client.get("/api/first-board/research-runs").json() == {"runs": []}
    assert not (client.app.state.first_board_service.root / "events").exists()


def test_paper_mode_receives_strict_events_and_selected_account_without_touching_replay(client, monkeypatch):
    from app.services import first_board_trade_audit as audit

    svc = client.app.state.first_board_service
    events = svc.root / "events"
    events.mkdir(parents=True)
    document = {"schema_version": 1, "date": "2026-10-20", "last": {}, "events": [
        {"id": "abc", "date": "2026-10-20", "revision": 0, "event_type": "buy_candidate",
         "symbol": "600000.SH", "evidence": {}}]}
    path = events / "2026-10-20.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    original = path.read_bytes()
    calls = []
    real_build = audit.build_paper_trade_audit

    def build(data_dir, account_id, **kwargs):
        calls.append((account_id, kwargs))
        result = real_build(data_dir, account_id, **{**kwargs, "events": []})
        result["fingerprint"] = "ledger-digest"
        return result

    monkeypatch.setattr(audit, "build_paper_trade_audit", build)
    response = client.post("/api/first-board/trade-validation", json=body(mode="paper", account_id="default"))
    assert response.status_code == 200, response.text
    assert calls[0][0] == "default"
    assert calls[0][1]["events"] == document["events"]
    assert response.json()["request"]["options"] is None
    assert path.read_bytes() == original


def test_paper_default_account_comes_from_selected_historical_revision(client):
    service = client.app.state.first_board_service
    for account_id in ("historical", "current", "explicit"):
        fb.paper.create_account(service.repo.store.data_dir, 100000, account_id=account_id)
    historical = service.save_config({**service.get_config(), "paper_account_id": "historical"})
    current = service.save_config({**historical, "paper_account_id": "current"})
    assert current["revision"] > historical["revision"]
    response = client.post("/api/first-board/trade-validation", json=body(
        mode="paper", revision=historical["revision"],
    ))
    assert response.status_code == 200, response.text
    assert response.json()["request"]["account_id"] == "historical"
    assert response.json()["account_id"] == "historical"
    overridden = client.post("/api/first-board/trade-validation", json=body(
        mode="paper", revision=historical["revision"], account_id="explicit",
    ))
    assert overridden.status_code == 200, overridden.text
    assert overridden.json()["request"]["account_id"] == "explicit"


def test_corrupt_evidence_is_not_silently_dropped(client):
    svc = client.app.state.first_board_service
    directory = svc.root / "events"
    directory.mkdir(parents=True)
    path = directory / "2026-10-20.json"
    path.write_text("broken", encoding="utf-8")
    response = client.post("/api/first-board/trade-validation", json=body(mode="paper"))
    assert response.status_code == 400 and "损坏" in response.json()["detail"]
    assert path.read_text(encoding="utf-8") == "broken"
    assert not (svc.root / "trade-validation").exists()


def test_trade_validation_shares_research_lock_and_releases_after_failure(client, monkeypatch):
    from app.backtest import first_board_execution as execution

    svc = client.app.state.first_board_service
    svc._research_lock.acquire()
    try:
        response = client.post("/api/first-board/trade-validation", json=body())
        assert response.status_code == 400 and "正在运行" in response.json()["detail"]
    finally:
        svc._research_lock.release()
    monkeypatch.setattr(execution, "run_execution_validation", lambda *a, **k: (_ for _ in ()).throw(ValueError("缺少证据")))
    assert client.post("/api/first-board/trade-validation", json=body()).status_code == 400
    assert svc._research_lock.acquire(blocking=False)
    svc._research_lock.release()


def test_corrupt_saved_report_is_preserved_and_reported(client):
    root = client.app.state.first_board_service.root / "trade-validation"
    root.mkdir(parents=True)
    path = root / ("a" * 32 + ".json")
    path.write_text("{}", encoding="utf-8")
    response = client.get("/api/first-board/trade-validation-runs")
    assert response.status_code == 400 and "损坏" in response.json()["detail"]
    assert path.read_text(encoding="utf-8") == "{}"


@pytest.mark.parametrize("damage", [
    "missing_summary", "missing_coverage", "invalid_timestamp", "invalid_date",
    "invalid_trade", "invalid_model", "nonfinite_summary",
])
def test_saved_trade_report_rejects_invalid_display_contract(client, damage):
    response = client.post("/api/first-board/trade-validation", json=body())
    assert response.status_code == 200, response.text
    result = response.json()
    path = client.app.state.first_board_service.root / "trade-validation" / f"{result['id']}.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    if damage == "missing_summary":
        del document["result"]["summary"]
    elif damage == "missing_coverage":
        del document["result"]["coverage"]
    elif damage == "invalid_timestamp":
        document["created_at"] = document["result"]["created_at"] = "bad-date"
    elif damage == "invalid_date":
        document["result"]["request"]["start"] = "2026-13-01"
    elif damage == "invalid_trade":
        document["result"]["trades"] = [{"signal_id": "incomplete"}]
    elif damage == "invalid_model":
        document["result"]["model"] = None
    else:
        document["result"]["summary"]["net_pnl_sum"] = float("nan")
    text = json.dumps(document)
    path.write_text(text, encoding="utf-8")
    reopened = client.get("/api/first-board/trade-validation-runs")
    assert reopened.status_code == 400
    assert "损坏" in reopened.json()["detail"]
    assert path.read_text(encoding="utf-8") == text


def test_invalid_engine_output_is_not_saved_as_a_report(client, monkeypatch):
    from app.backtest import first_board_execution as execution

    monkeypatch.setattr(execution, "run_execution_validation", lambda *args, **kwargs: {
        "schema_version": 1, "mode": "recorded_execution", "status": "no_signals",
    })
    response = client.post("/api/first-board/trade-validation", json=body())
    assert response.status_code == 400
    assert not (client.app.state.first_board_service.root / "trade-validation").exists()


def test_new_events_freeze_complete_execution_configuration():
    config = fb.FirstBoardConfig(revision=3, exit_time="10:15", exit_loss_pct=.05)
    event = fb.FirstBoardService._event(
        {"symbol": "600000.SH", "name": "样本", "pattern": "platform", "pattern_label": "平台突破",
         "state": "approaching", "evidence": {}},
        "buy_candidate", config, datetime(2026, 10, 21, 10, tzinfo=CN_TZ),
    )
    assert event["config"] == config.model_dump(mode="json")


def test_replay_http_reads_immutable_samples_and_real_parquet_then_reopens_report(client, tmp_path):
    from app.services.kline_sync import _write_minute_partition
    from app.tickflow.repository import DataStore, KlineRepository

    svc = client.app.state.first_board_service
    svc.repo = KlineRepository(DataStore(tmp_path))
    symbol = "600000.SH"
    days = [date(2026, 10, 19), date(2026, 10, 20)]
    for day in days:
        opening = datetime.combine(day, datetime.min.time()).replace(hour=9, minute=30)
        times = [opening + timedelta(minutes=i) for i in range(121)]
        times += [opening.replace(hour=13, minute=1) + timedelta(minutes=i) for i in range(120)]
        price = 10. if day == days[0] else 10.2
        frame = pl.DataFrame({"symbol": [symbol] * 241, "datetime": times,
                              **{key: [price] * 241 for key in ("open", "high", "low", "close")},
                              "volume": [10000.] * 241, "amount": [price * 1_000_000] * 241,
                              "minute_price_basis": ["raw"] * 241,
                              "minute_time_grid": ["auction_end"] * 241})
        _write_minute_partition(frame, tmp_path / "kline_minute")
    pl.DataFrame([{"symbol": symbol, "date": day,
                   **{key: 10.2 if day == days[1] else 10.
                      for key in ("open", "high", "low", "close", "raw_close", "raw_high", "raw_low")},
                   "volume": 1_000_000., "amount": 1_000_000_000.}
                  for day in [date(2026, 10, 16), *days]]).write_parquet(
                      tmp_path / "kline_daily_enriched" / "part.parquet")
    pl.DataFrame({"symbol": [symbol], "name": ["样本"],
                  "listing_date": [date(1999, 11, 10)]}).write_parquet(
                      tmp_path / "instruments" / "instruments.parquet")
    for day in days:
        observed = datetime.combine(day, datetime.min.time()).replace(hour=10, second=17, tzinfo=CN_TZ)
        svc._observation_store.record(
            [{"symbol": symbol, "name": "样本", "pattern": "platform", "can_buy": day == days[0],
              "quote_time": observed.isoformat(), "state": "approaching",
              "evidence": {"limit_up_price": 11., "raw_prev_close": 10.}}],
            now=observed, rule_revision=0, membership_version=None,
            metadata={"config": svc.get_config()},
        )
    sources = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.suffix in {".parquet", ".gz"}}
    response = client.post("/api/first-board/trade-validation", json=body(end="2026-10-19"))
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["summary"]["closed"] == 1
    assert result["coverage"]["stock_days"] == result["coverage"]["sample_count"] == 1
    trade = result["trades"][0]
    assert trade["entry_at"] == "2026-10-19T10:03:00+08:00"
    assert trade["exit_at"] == "2026-10-20T10:33:00+08:00"
    assert trade["net_return"] < trade["gross_return"]
    assert client.get("/api/first-board/trade-validation-runs").json()["runs"][0]["result"] == result
    assert {p: p.read_bytes() for p in sources} == sources


def test_paper_http_attributes_real_ledger_and_preserves_all_source_bytes(client, tmp_path):
    svc = client.app.state.first_board_service
    event = svc._event(
        {"symbol": "600000.SH", "name": "样本", "pattern": "platform", "pattern_label": "平台突破",
         "state": "approaching", "evidence": {}},
        "buy_candidate", fb.FirstBoardConfig(), datetime(2026, 10, 19, 10, tzinfo=CN_TZ),
    )
    svc._write_day(date(2026, 10, 19), {"schema_version": 1, "date": "2026-10-19",
                                        "last": {}, "events": [event]})
    root = tmp_path / "paper" / "accounts" / "default"
    (root / "orders").mkdir(parents=True)
    (root / "account.json").write_text(json.dumps({"id": "default", "initial_cash": 100000,
                                                    "cash": 100090, "status": "active"}), encoding="utf-8")
    fills = []
    for side, day, price in [("buy", 19, 10.), ("sell", 20, 11.)]:
        stamp = f"2026-10-{day}T10:02:00+08:00"
        order = {"id": side, "symbol": "600000.SH", "side": side, "qty": 100,
                 "source": f"first_board:{event['id']}" if side == "buy" else "manual",
                 "status": "filled", "created_at": stamp, "filled_at": stamp,
                 "ref_price": price, "asset_type": "stock"}
        (root / "orders" / f"order_{side}.json").write_text(json.dumps(order), encoding="utf-8")
        fills.append({"kind": "fill", "order_id": side, "symbol": "600000.SH", "date": stamp[:10],
                      "ts": stamp, "side": side, "qty": 100, "price": price, "fee": 5., "asset_type": "stock"})
    (root / "fills.jsonl").write_text("\n".join(json.dumps(fill) for fill in fills), encoding="utf-8")
    sources = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    response = client.post("/api/first-board/trade-validation", json=body(mode="paper", account_id="default"))
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["summary"]["closed_signals"] == 1
    assert result["summary"]["realized_net_pnl"] == 90
    assert result["trades"][0]["signal_id"] == event["id"]
    assert result["trades"][0]["exits"][0]["manual_exit"] is True
    assert client.get("/api/first-board/trade-validation-runs").json()["runs"][0]["result"] == result
    assert {p: p.read_bytes() for p in sources} == sources
