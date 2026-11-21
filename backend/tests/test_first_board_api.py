from datetime import datetime, timedelta
from types import SimpleNamespace

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.first_board import router
from app.market_time import CN_TZ
from app.services import first_board as fb
from app.services.first_board import FirstBoardService
from app.services.first_board_context import evaluate_sector_peers


@pytest.fixture
def client(tmp_path):
    app = FastAPI()
    app.include_router(router)
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    app.state.first_board_service = FirstBoardService(repo, history_loader=lambda *_: pl.DataFrame())
    return TestClient(app)


def test_config_roundtrip_and_conflict(client):
    config = client.get("/api/first-board/config").json()
    assert config["enabled"] is False
    response = client.put("/api/first-board/config", json={**config, "notify": False})
    assert response.status_code == 200
    assert response.json()["revision"] == 1
    assert client.put("/api/first-board/config", json=config).status_code == 409
    assert len(client.get("/api/first-board/versions").json()["versions"]) == 2


def test_snapshot_filters_minimum_change_and_applies_saved_revision(client, monkeypatch):
    now = datetime(2026, 10, 16, 10, 0, tzinfo=CN_TZ)
    monkeypatch.setattr(fb, "_now", lambda: now)
    service = client.app.state.first_board_service
    prices = {"600000.SH": 9.991, "600001.SH": 10.499, "600002.SH": 10.5, "600003.SH": 10.8}
    candidates = pl.DataFrame([{
        "symbol": symbol, "name": symbol, "pattern": "platform", "pattern_label": "平台突破",
        "reference_date": now.date() - timedelta(days=1), "reference_price": 10.0,
        "reference_adjusted_close": 10.0, "breakout_price": 10.2,
        "history_bars": 65, "first_board_count": 0,
    } for symbol in prices])

    def prepare(_day, _config):
        service._candidates = candidates
        service._environment = {"allowed": True, "date": "2026-10-15", "state": "range"}
        service._sectors = {}

    monkeypatch.setattr(service, "_prepare", prepare)
    monkeypatch.setattr(service._attention_data, "minutes", lambda *_args, **_kwargs: (
        pl.DataFrame(), False, "测试无分钟数据",
    ))
    quotes = pl.DataFrame([{
        "symbol": symbol, "name": symbol, "date": now.date(),
        "quote_ts": int(now.timestamp() * 1000), "raw_close": price, "close": price,
        "raw_high": price, "prev_close": 10.0, "change_pct": price / 10.0 - 1,
        "limit_up_price": 10.995, "turnover_rate": 5.0, "amount": 80_000_000.0,
    } for symbol, price in prices.items()])

    config = client.get("/api/first-board/config").json()
    response = client.put("/api/first-board/config", json={
        **config, "enabled": True, "notify": False, "require_sector_confirmation": False,
        "rules": {**config["rules"], "min_change_pct": .05},
    })
    assert response.status_code == 200
    saved = response.json()
    assert saved["rules"]["min_change_pct"] == .05
    service.process_snapshot(quotes, now.date(), now=now)
    response = client.get("/api/first-board/snapshot")
    assert response.status_code == 200
    snapshot = response.json()
    assert snapshot["revision"] == saved["revision"]
    assert {row["symbol"] for row in snapshot["rows"]} == {"600002.SH", "600003.SH"}
    assert snapshot["coverage"] == {
        "candidate_count": 2, "symbol_count": 2, "quote_count": 4, "fresh_count": 4, "role_hidden_count": 0,
    }

    response = client.put("/api/first-board/config", json={
        **saved, "rules": {**saved["rules"], "min_change_pct": .06},
    })
    assert response.status_code == 200
    revision = response.json()["revision"]
    assert revision == saved["revision"] + 1
    reset = client.get("/api/first-board/snapshot").json()
    assert reset["revision"] == revision
    assert reset["rows"] == []
    service.process_snapshot(quotes, now.date(), now=now)
    response = client.get("/api/first-board/snapshot")
    assert response.status_code == 200
    refreshed = response.json()
    assert refreshed["revision"] == revision
    assert [row["symbol"] for row in refreshed["rows"]] == ["600003.SH"]
    assert refreshed["coverage"] == {
        "candidate_count": 1, "symbol_count": 1, "quote_count": 4, "fresh_count": 4, "role_hidden_count": 0,
    }


def test_empty_data_responses(client):
    for endpoint in ("snapshot", "events?day=2026-10-16", "research-runs"):
        response = client.get(f"/api/first-board/{endpoint}")
        assert response.status_code == 200, response.text
    assert client.get("/api/first-board/events?day=2026-10-16").json()["total"] == 0
    assert client.post("/api/first-board/refresh").json()["rows"] == []


def test_first_alert_endpoint_ranks_whole_day_then_filters_and_pages(client, monkeypatch):
    # Older events retain their original change ranking. Scores from a later
    # alert for the same stock or from another revision must not fill the gaps.
    clock = datetime(2026, 10, 16, 10, tzinfo=CN_TZ)
    monkeypatch.setattr(fb, "_now", lambda: clock + timedelta(hours=1))
    service = client.app.state.first_board_service
    day = clock.date()

    def alert(i, when, rev=4):
        return service._event({"symbol": f"60000{i}.SH", "name": f"候选{i}", "pattern": "platform",
                               "pattern_label": "平台突破", "state": "approaching", "change_pct": .01 * i,
                               "evidence": {}}, "buy_candidate", fb.FirstBoardConfig(revision=rev), when)

    events = [alert(i, clock) for i in range(1, 5)]
    events += [alert(1, clock + timedelta(minutes=1)), alert(5, clock + timedelta(minutes=2), rev=5)]
    # A newer version with only a watch event remains selectable, while the
    # default opens the latest version with an actual buy alert.
    events += [{**alert(6, clock + timedelta(minutes=3), rev=6), "event_type": "watch"}]
    state = {"schema_version": 1, "date": str(day), "last": {}, "events": events}
    service._write_day(day, state)
    base = f"/api/first-board/first-alert-rankings?day={day}"
    latest = client.get(base)
    assert latest.status_code == 200, latest.text
    assert latest.json()["available_revisions"] == [6, 5, 4]
    assert latest.json()["revision"] == 5 and latest.json()["total"] == 1
    assert client.get(base + "&revision=6").json()["total"] == 0
    assert client.get(base + "&revision=2").status_code == 400

    filtered = client.get(base + "&revision=4&top_n=3&limit=1&offset=1")
    assert filtered.status_code == 200, filtered.text
    report = filtered.json()
    assert (report["total"], report["matched"], report["top_n"], len(report["rows"])) == (4, 3, 3, 1)
    assert report["coverage"]["change_known"] == 4
    assert report["rows"][0]["ranks"]["change"]["day"] == 2
    front = client.get(base + "&revision=4&top_n=3").json()["rows"]
    assert [row["ranks"]["change"]["day"] for row in front] == [1, 2, 3]
    # Replacing the archive must invalidate a cached page, even across process
    # boundaries. Reading the corrected report never mutates the input file.
    state["events"].append(alert(7, clock + timedelta(minutes=4), rev=4))
    service._write_day(day, state)
    path = service.root / "events" / f"{day}.json"
    saved = path.read_bytes()
    after = client.get(base + "&revision=4").json()
    assert after["total"] == after["matched"] == 5 and after["rows"][-1]["ranks"]["change"]["day"] == 5
    assert path.read_bytes() == saved


def test_first_alert_endpoint_reports_empty_future_and_invalid_queries(client, monkeypatch):
    monkeypatch.setattr(fb, "_now", lambda: datetime(2026, 10, 16, 10, tzinfo=CN_TZ))
    base = "/api/first-board/first-alert-rankings?day=2026-10-16"
    empty = client.get(base)
    assert empty.status_code == 200
    assert empty.json()["available_revisions"] == [] and empty.json()["rows"] == []
    assert empty.json()["revision"] == 0 and empty.json()["total"] == empty.json()["matched"] == 0
    assert client.get(base.replace("10-16", "10-17")).status_code == 400
    for query in ("limit=0", "limit=501", "offset=-1", "revision=-1", "top_n=4"):
        assert client.get(base + "&" + query).status_code == 422


def test_events_filter_before_limit_and_preserve_order(client, monkeypatch):
    records = [{"id": str(i), "pattern": "platform", "event_type": "watch"} for i in range(5001)]
    records += [
        {"id": "buy", "pattern": "oversold", "state": "approaching", "event_type": "buy_candidate"},
        {"id": "near", "pattern": "oversold", "state": "approaching", "event_type": "approaching"},
        {"id": "exit", "pattern": "exit", "event_type": "exit_candidate"},
    ]
    monkeypatch.setattr(client.app.state.first_board_service, "events", lambda _day: records)
    base = "/api/first-board/events?day=2026-10-16"
    assert client.get(base).json()["total"] == 5004
    assert len(client.get(base).json()["events"]) == 5000
    result = client.get(base + "&pattern=oversold").json()
    assert [row["id"] for row in result["events"]] == ["buy", "near"]
    assert result["total"] == 2
    for query, ids in [
        ("event_type=approaching", ["near"]),
        ("pattern=oversold&event_type=buy_candidate", ["buy"]),
        ("pattern=exit&event_type=exit_candidate", ["exit"]),
        ("pattern=trend&event_type=approaching", []),
    ]:
        result = client.get(base + "&" + query).json()
        assert [row["id"] for row in result["events"]] == ids
        assert result["total"] == len(ids)
    assert records[0]["id"] == "0"


@pytest.mark.parametrize("query", ["pattern=unknown", "event_type=unknown"])
def test_invalid_event_filters_rejected(client, query):
    assert client.get("/api/first-board/events?" + query).status_code == 422


@pytest.mark.parametrize("payload", [
    {"event_id": "bad/path", "side": "buy", "qty": 100},
    {"event_id": "abc", "side": "buy", "qty": 101},
    {"event_id": "abc", "side": "buy", "qty": True},
    {"event_id": "abc", "side": "sell", "qty": -100},
])
def test_invalid_order_payload_rejected(client, payload):
    assert client.post("/api/first-board/paper-order", json=payload).status_code == 422


def test_unknown_event_and_ambiguous_order_rejected(client):
    body = {"event_id": "abc", "side": "buy", "qty": 100}
    assert client.post("/api/first-board/paper-order", json=body).status_code == 400
    assert client.post("/api/first-board/paper-order", json={**body, "amount": 1000.0}).status_code == 400


def test_invalid_dates_and_config_rejected(client):
    assert client.get("/api/first-board/events?day=bad").status_code == 422
    assert client.post("/api/first-board/research", json={"start": "2026-10-16", "end": "2026-10-15"}).status_code == 422
    config = client.get("/api/first-board/config").json()
    assert client.put("/api/first-board/config", json={**config, "exit_time": "12:00"}).status_code == 422


def test_missing_service_returns_explicit_unavailable():
    app = FastAPI()
    app.include_router(router)
    response = TestClient(app).get("/api/first-board/config")
    assert response.status_code == 503
    assert "未就绪" in response.json()["detail"]


def test_snapshot_exposes_industry_coverage_and_block_reason(client):
    members = {f"{600000 + i}.SH": "金融-银行" for i in range(100)}
    quotes = pl.DataFrame({"symbol": list(members)[:4], "change_pct": [.08, .06, .01, .02]})
    sector = evaluate_sector_peers(quotes, members, ["600000.SH"])["600000.SH"]
    service = client.app.state.first_board_service
    service._snapshot["rows"] = [{
        "symbol": "600000.SH", "sector_confirmation": sector,
        "can_buy": False, "event_id": None, "suggested_amount": None,
        "blocked_reasons": [sector["reason"]],
    }]
    response = client.get("/api/first-board/snapshot")
    assert response.status_code == 200
    row = response.json()["rows"][0]
    assert row["sector_confirmation"]["member_count"] == 99
    assert row["sector_confirmation"]["count"] == 3
    assert row["sector_confirmation"]["coverage"] == pytest.approx(3 / 99)
    assert row["sector_confirmation"]["confirmed"] is False
    assert row["sector_confirmation"]["excludes_candidate"] is True
    assert "覆盖率" in row["blocked_reasons"][0]


def _opportunity_events(service, clock):
    def make(symbol, pattern, event_type, minutes, rev=4):
        row = {"symbol": symbol, "name": f"名{symbol}", "pattern": pattern, "pattern_label": pattern, "state": "approaching",
               "price": 10.0, "evidence": {}, "blocked_reasons": []}
        return service._event(row, event_type, fb.FirstBoardConfig(revision=rev), clock + timedelta(minutes=minutes))

    return [make("600001.SH", "platform", "buy_candidate", 0), make("600001.SH", "trend", "buy_candidate", 1),
            make("600001.SH", "platform", "sealed", 2), make("600002.SH", "oversold", "approaching", 1),
            make("600002.SH", "oversold", "sealed", 3), make("600003.SH", "platform", "buy_candidate", 2, rev=5),
            make("600003.SH", "platform", "buy_candidate", 4, rev=6), make("600003.SH", "platform", "watch", 5, rev=7)]


def test_opportunities_endpoint_aggregates_one_row_per_stock_and_is_read_only(client, monkeypatch):
    clock = datetime(2026, 10, 16, 10, tzinfo=CN_TZ)
    monkeypatch.setattr(fb, "_now", lambda: clock + timedelta(hours=1))
    service = client.app.state.first_board_service
    day = clock.date()
    service._write_day(day, {"schema_version": 1, "date": str(day), "last": {}, "events": _opportunity_events(service, clock)})
    path = service.root / "events" / f"{day}.json"
    saved = path.read_bytes()
    response = client.get(f"/api/first-board/opportunities?day={day}&revision=4")
    assert response.status_code == 200, response.text
    report = response.json()
    assert [row["symbol"] for row in report["rows"]] == ["600001.SH", "600002.SH"]
    first, second = report["rows"]
    assert [item["pattern"] for item in first["patterns"]] == ["platform", "trend"] and first["event_count"] == 3
    assert first["first_alert"]["at"] == clock.isoformat() and first["first_touch"]["at"] == (clock + timedelta(minutes=2)).isoformat()
    assert second["sealed_unalerted"] is True and second["first_record"]["event_type"] == "approaching"
    assert report["summary"] == {"stock_count": 2, "alerted_count": 1, "touched_count": 2,
                                 "sealed_unalerted_count": 1, "skipped_count": 0}
    assert report["available_revisions"] == [7, 6, 5, 4]
    assert path.read_bytes() == saved


def test_opportunities_default_revision_matches_rankings(client, monkeypatch):
    clock = datetime(2026, 10, 16, 10, tzinfo=CN_TZ)
    monkeypatch.setattr(fb, "_now", lambda: clock + timedelta(hours=1))
    service = client.app.state.first_board_service
    day = clock.date()
    service._write_day(day, {"schema_version": 1, "date": str(day), "last": {}, "events": _opportunity_events(service, clock)})
    ranking = client.get(f"/api/first-board/first-alert-rankings?day={day}").json()
    report = client.get(f"/api/first-board/opportunities?day={day}").json()
    assert report["revision"] == ranking["revision"] == 6
    assert report["available_revisions"] == ranking["available_revisions"]
    assert client.get(f"/api/first-board/opportunities?day={day}&revision=2").status_code == 400


def test_opportunities_first_alert_time_matches_rankings_signal_at(client, monkeypatch):
    clock = datetime(2026, 10, 16, 10, tzinfo=CN_TZ)
    monkeypatch.setattr(fb, "_now", lambda: clock + timedelta(hours=1))
    service = client.app.state.first_board_service
    day = clock.date()
    service._write_day(day, {"schema_version": 1, "date": str(day), "last": {}, "events": _opportunity_events(service, clock)})
    ranking = {row["symbol"]: row["signal_at"] for row in client.get(f"/api/first-board/first-alert-rankings?day={day}&revision=4").json()["rows"]}
    report = client.get(f"/api/first-board/opportunities?day={day}&revision=4").json()
    alerted = {row["symbol"]: row["first_alert"]["at"] for row in report["rows"] if row["first_alert"]}
    assert alerted == ranking and alerted


def test_opportunities_reports_empty_future_and_invalid_queries(client, monkeypatch):
    monkeypatch.setattr(fb, "_now", lambda: datetime(2026, 10, 16, 10, tzinfo=CN_TZ))
    base = "/api/first-board/opportunities?day=2026-10-16"
    empty = client.get(base)
    assert empty.status_code == 200
    assert empty.json()["rows"] == [] and empty.json()["available_revisions"] == []
    assert client.get(base.replace("10-16", "10-17")).status_code == 400
    assert client.get("/api/first-board/opportunities").status_code == 422
    assert client.get(base + "&revision=-1").status_code == 422


def _seed_opportunities(client, monkeypatch, now):
    monkeypatch.setattr(fb, "_now", lambda: now)
    service = client.app.state.first_board_service
    clock = datetime(2026, 10, 16, 10, tzinfo=CN_TZ)
    service._write_day(clock.date(), {"schema_version": 1, "date": str(clock.date()), "last": {}, "events": _opportunity_events(service, clock)})
    return service


def _daily(rows):
    return pl.DataFrame([{"symbol": s, "date": datetime(2026, 10, 16).date(), "raw_close": c, "raw_high": c, "raw_prev_close": 10.0,
                          "limit_up_price": 10.995} for s, c in rows],
                        schema={"symbol": pl.String, "date": pl.Date, "raw_close": pl.Float64, "raw_high": pl.Float64,
                                "raw_prev_close": pl.Float64, "limit_up_price": pl.Float64})


def test_opportunities_verify_is_off_by_default(client, monkeypatch):
    _seed_opportunities(client, monkeypatch, datetime(2026, 10, 16, 16, tzinfo=CN_TZ))
    report = client.get("/api/first-board/opportunities?day=2026-10-16&revision=4").json()
    assert "verified" not in report and all("close" not in row and "paper" not in row for row in report["rows"])
    assert "close_sealed_count" not in report["summary"]


def test_opportunities_verify_adds_close_and_paper(client, monkeypatch):
    service = _seed_opportunities(client, monkeypatch, datetime(2026, 10, 16, 16, tzinfo=CN_TZ))
    service._versions.append({"revision": 4, "paper_account_id": "acc1"})
    service.repo.get_daily_batch = lambda *_args, **_kwargs: _daily([("600001.SH", 11.0)])
    seen = {}

    def fake_audit(data_dir, account_id, *, events, start, end, revision):
        seen.update(account_id=account_id, start=start, end=end, revision=revision)
        return {"status": "ready", "reason": "ok", "coverage": {"truncated": False}, "trades": [
            {"signal_id": "e", "symbol": "600001.SH", "status": "closed", "reason": "完整平仓", "orders": [{"side": "buy"}, {"side": "sell"}], "closed_net_return": .02}]}

    monkeypatch.setattr("app.services.first_board_trade_audit.build_paper_trade_audit", fake_audit)
    report = client.get("/api/first-board/opportunities?day=2026-10-16&revision=4&verify=true").json()
    first, second = report["rows"]
    assert report["verified"] is True
    assert first["close"]["status"] == "sealed" and second["close"]["status"] == "unverified"
    assert first["paper"] == {"status": "closed", "reason": "完整平仓", "net_return": .02}
    assert second["paper"]["status"] == "no_signal"
    assert report["summary"]["close_sealed_count"] == 1 and report["summary"]["close_checked_count"] == 1
    assert report["summary"]["paper_filled_count"] == 1 and report["summary"]["paper_checked_count"] == 1
    assert seen == {"account_id": "acc1", "start": datetime(2026, 10, 16).date(), "end": datetime(2026, 10, 16).date(), "revision": 4}


def test_opportunities_verify_without_account_skips_audit(client, monkeypatch):
    service = _seed_opportunities(client, monkeypatch, datetime(2026, 10, 16, 16, tzinfo=CN_TZ))
    service.repo.get_daily_batch = lambda *_args, **_kwargs: _daily([])

    def boom(*_args, **_kwargs):
        raise AssertionError("audit must not run without an account")

    monkeypatch.setattr("app.services.first_board_trade_audit.build_paper_trade_audit", boom)
    report = client.get("/api/first-board/opportunities?day=2026-10-16&revision=4&verify=true").json()
    assert {row["symbol"]: row["paper"]["status"] for row in report["rows"]} == {"600001.SH": "no_account", "600002.SH": "no_signal"}


def test_opportunities_verify_before_close_marks_today_unverified(client, monkeypatch):
    service = _seed_opportunities(client, monkeypatch, datetime(2026, 10, 16, 10, 30, tzinfo=CN_TZ))
    service.repo.get_daily_batch = lambda *_args, **_kwargs: _daily([("600001.SH", 11.0), ("600002.SH", 11.0)])
    report = client.get("/api/first-board/opportunities?day=2026-10-16&revision=4&verify=true").json()
    assert {row["close"]["status"] for row in report["rows"]} == {"unverified"}


def test_opportunities_verify_survives_daily_read_failure(client, monkeypatch):
    service = _seed_opportunities(client, monkeypatch, datetime(2026, 10, 16, 16, tzinfo=CN_TZ))

    def broken(*_args, **_kwargs):
        raise ValueError("日线损坏")

    service.repo.get_daily_batch = broken
    response = client.get("/api/first-board/opportunities?day=2026-10-16&revision=4&verify=true")
    assert response.status_code == 200
    assert {row["close"]["status"] for row in response.json()["rows"]} == {"unverified"}


def test_opportunities_verify_survives_unavailable_daily_generation(client, monkeypatch):
    # 日线健康检查失败抛 EnrichedGenerationUnavailableError(RuntimeError), 核验不能因此整体 500。
    from app.enriched_generation import EnrichedGenerationUnavailableError

    service = _seed_opportunities(client, monkeypatch, datetime(2026, 10, 16, 16, tzinfo=CN_TZ))

    def unavailable(*_args, **_kwargs):
        raise EnrichedGenerationUnavailableError("日线暂不可用")

    service.repo.get_daily_batch = unavailable
    response = client.get("/api/first-board/opportunities?day=2026-10-16&revision=4&verify=true")
    assert response.status_code == 200, response.text
    assert {row["close"]["status"] for row in response.json()["rows"]} == {"unverified"}
