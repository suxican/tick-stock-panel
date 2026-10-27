import json
import threading
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import polars as pl
import pytest

from app.market_time import CN_TZ
from app.services import first_board as fb
from app.strategy import paper

NOW = datetime(2026, 10, 16, 10, 0, tzinfo=CN_TZ)
SYMBOL = "600000.SH"


def quotes(now=NOW, **overrides):
    row = {
        "symbol": SYMBOL, "date": now.date(), "name": "浦发银行",
        "quote_ts": int(now.timestamp() * 1000), "raw_close": 10.8,
        "close": 10.8, "raw_high": 10.85, "prev_close": 10.0,
        "limit_up_price": 10.995, "turnover_rate": 5.0, "amount": 80_000_000.0,
        "change_pct": .08,
    }
    return pl.DataFrame([{**row, **overrides}])


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.setattr(fb, "_now", lambda: NOW)
    monkeypatch.setattr(paper, "cn_today", lambda: NOW.date())
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    state = {"enabled": True, "realtime_allowed": True, "mode": "poll", "paused": False}
    svc = fb.FirstBoardService(repo, history_loader=lambda *_: pl.DataFrame(),
                               quote_service=SimpleNamespace(status=lambda: state))
    svc.save_config({**svc.get_config(), "enabled": True, "require_sector_confirmation": False})

    def prepare(_day, _config):
        svc._candidates = pl.DataFrame([{
            "symbol": SYMBOL, "name": "浦发银行", "pattern": "platform",
            "pattern_label": "平台突破", "reference_date": date(2026, 10, 15),
            "reference_price": 10.0, "reference_adjusted_close": 10.0,
            "breakout_price": 10.2, "history_bars": 65, "first_board_count": 0,
        }])
        svc._environment = {"allowed": True, "date": "2026-10-15", "state": "range"}
        svc._sectors = {SYMBOL: "金融-银行"}

    monkeypatch.setattr(svc, "_prepare", prepare)
    return svc


def test_config_versions_survive_restart_and_reject_lost_updates(service):
    first = service.get_config()
    changed = service.save_config({**first, "notify": False})
    assert changed["revision"] == first["revision"] + 1
    with pytest.raises(ValueError, match="版本已变化"):
        service.save_config(first)
    reopened = fb.FirstBoardService(service.repo, history_loader=lambda *_: pl.DataFrame())
    assert reopened.get_config() == changed
    assert [v["revision"] for v in reopened.versions()] == [2, 1, 0]


def test_corrupt_config_is_preserved_and_cannot_start(service):
    service.config_path.write_text("broken configuration", encoding="utf-8")
    with pytest.raises(ValueError, match="配置损坏"):
        fb.FirstBoardService(service.repo, history_loader=lambda *_: pl.DataFrame())
    assert service.config_path.read_text(encoding="utf-8") == "broken configuration"


def test_live_candidate_persists_event_and_restart_deduplicates(service, monkeypatch):
    published = []
    service._publish = published.extend
    service.process_snapshot(quotes(), NOW.date(), now=NOW)
    row = service.snapshot()["rows"][0]
    assert row["can_buy"] and row["event_id"]
    assert row["change_pct"] == pytest.approx(.08)
    assert row["turnover_rate"] == 5.0
    assert len(published) == 1
    reopened = fb.FirstBoardService(service.repo, history_loader=lambda *_: pl.DataFrame())
    monkeypatch.setattr(reopened, "_prepare", lambda *_: None)
    reopened._candidates = service._candidates
    reopened._environment = service._environment
    reopened._sectors = service._sectors
    reopened.process_snapshot(quotes(), NOW.date(), now=NOW)
    assert len(reopened.events(NOW.date())) == 1
    assert reopened.snapshot()["rows"][0]["event_id"] == row["event_id"]


@pytest.mark.parametrize(("price", "expected_count"), [
    (9.99, 0), (10.0, 0), (10.49, 0), (10.5, 3), (10.8, 3),
])
def test_snapshot_minimum_change_filters_all_patterns(service, monkeypatch, price, expected_count):
    prepare = service._prepare

    def prepare_patterns(*args):
        prepare(*args)
        candidate = service._candidates.to_dicts()[0]
        service._candidates = pl.DataFrame([
            {**candidate, "pattern": pattern} for pattern in ("platform", "trend", "oversold")
        ])

    monkeypatch.setattr(service, "_prepare", prepare_patterns)
    # Ignore the upstream percentage field; use the same price basis as the displayed change.
    service.process_snapshot(quotes(raw_close=price, close=price, change_pct=.08), NOW.date(), now=NOW)
    snapshot = service.snapshot()
    assert len(snapshot["rows"]) == snapshot["coverage"]["candidate_count"] == expected_count
    assert snapshot["coverage"]["symbol_count"] == int(expected_count > 0)
    assert all(row["change_pct"] == pytest.approx(price / 10 - 1) for row in snapshot["rows"])


def test_snapshot_minimum_change_includes_exact_boundary_despite_float_rounding(service):
    service.process_snapshot(quotes(raw_close=1.68, close=1.68, prev_close=1.6,
                                    raw_high=1.68, limit_up_price=1.755), NOW.date(), now=NOW)
    assert service.snapshot()["coverage"]["candidate_count"] == 1
    assert service.snapshot()["rows"][0]["change_pct"] == pytest.approx(.05)


def test_minimum_change_config_refresh_and_restart(service, monkeypatch):
    service.process_snapshot(quotes(), NOW.date(), now=NOW)
    config = service.get_config()
    changed = service.save_config({**config, "rules": {**config["rules"], "min_change_pct": .09}})
    assert service.snapshot()["rows"] == []
    service.process_snapshot(quotes(), NOW.date(), now=NOW)
    assert service.snapshot()["rows"] == []
    assert service.snapshot()["revision"] == changed["revision"]

    reopened = fb.FirstBoardService(service.repo, history_loader=lambda *_: pl.DataFrame())
    monkeypatch.setattr(reopened, "_prepare", lambda *_: None)
    reopened._candidates = service._candidates
    reopened.process_snapshot(quotes(), NOW.date(), now=NOW)
    assert reopened.snapshot()["rows"] == []

    config = service.get_config()
    service.save_config({**config, "rules": {**config["rules"], "min_change_pct": .05}})
    service.process_snapshot(quotes(), NOW.date(), now=NOW)
    assert service.snapshot()["coverage"]["candidate_count"] == 1


def test_falling_below_minimum_leaves_pool_and_reentry_has_a_new_signal(service):
    service.process_snapshot(quotes(), NOW.date(), now=NOW)
    first_id = service.snapshot()["rows"][0]["event_id"]
    service.process_snapshot(quotes(raw_close=10.4, close=10.4), NOW.date(), now=NOW)
    assert service.snapshot()["rows"] == []
    assert service.events(NOW.date())[0]["event_type"] == "watch"
    service.process_snapshot(quotes(), NOW.date(), now=NOW)
    assert service.snapshot()["rows"][0]["event_id"] not in (None, first_id)


def test_broken_stock_below_minimum_leaves_pool_but_keeps_event(service):
    service.process_snapshot(quotes(raw_close=11.0, close=11.0, raw_high=11.0), NOW.date(), now=NOW)
    service.process_snapshot(quotes(raw_close=10.4, close=10.4, raw_high=11.0), NOW.date(), now=NOW)
    assert service.snapshot()["rows"] == []
    assert service.events(NOW.date())[0]["event_type"] == "broken"


@pytest.mark.parametrize("change", [
    {"quote_ts": int((NOW - timedelta(seconds=31)).timestamp() * 1000)},
    {"quote_ts": int((NOW + timedelta(seconds=1)).timestamp() * 1000)},
    {"quote_ts": None}, {"date": NOW.date() - timedelta(days=1)},
])
def test_invalid_quote_never_emits_or_allows_buy(service, change):
    service.process_snapshot(quotes(**change), NOW.date(), now=NOW)
    assert service.snapshot()["rows"][0]["can_buy"] is False
    assert service.events(NOW.date()) == []


def test_duplicate_quote_and_missing_timestamp_fail_closed(service):
    for frame in (pl.concat([quotes(), quotes()]), quotes().drop("quote_ts")):
        service.process_snapshot(frame, NOW.date(), now=NOW)
        assert not service.snapshot()["rows"][0]["can_buy"]
        assert service.events(NOW.date()) == []


def test_snapshot_expires_without_another_quote(service, monkeypatch):
    service.process_snapshot(quotes(), NOW.date(), now=NOW)
    monkeypatch.setattr(fb, "_now", lambda: NOW + timedelta(seconds=31))
    snapshot = service.snapshot()
    assert snapshot["status"] == "stale"
    assert snapshot["rows"][0]["event_id"] is None
    assert snapshot["rows"][0]["suggested_amount"] is None
    assert not snapshot["rows"][0]["can_buy"]


@pytest.mark.parametrize("status", [
    {"enabled": False}, {"enabled": True, "realtime_allowed": False},
    {"enabled": True, "paused": True}, {"enabled": True, "mode": "none"},
])
def test_unavailable_realtime_blocks_events(service, status):
    service._quote_service = SimpleNamespace(status=lambda: status)
    service.process_snapshot(quotes(), NOW.date(), now=NOW)
    assert service.snapshot()["status"] == "waiting"
    assert not service.snapshot()["rows"][0]["can_buy"]
    assert service.events(NOW.date()) == []


def test_disabled_service_does_not_queue_history_work(service):
    service.save_config({**service.get_config(), "enabled": False})
    service.offer_snapshot(quotes(), NOW.date())
    assert service._pending is None
    service.process_snapshot(quotes(), NOW.date(), now=NOW)
    assert service.snapshot()["status"] == "disabled"
    assert not service.snapshot()["rows"][0]["can_buy"]


def test_event_write_failure_does_not_advance_dedup_or_publish(service, monkeypatch):
    published = []
    service._publish = published.extend
    original = fb.atomic_write_text
    monkeypatch.setattr(fb, "atomic_write_text", lambda *_: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError, match="disk full"):
        service.process_snapshot(quotes(), NOW.date(), now=NOW)
    assert not published
    monkeypatch.setattr(fb, "atomic_write_text", original)
    service.process_snapshot(quotes(), NOW.date(), now=NOW)
    assert len(service.events(NOW.date())) == len(published) == 1


def test_failed_publication_retries_same_event_without_repeating_history(service):
    attempts, published = [], []

    def publish(events):
        attempts.append([event["id"] for event in events])
        if len(attempts) == 1:
            raise OSError("alert store unavailable")
        published.extend(events)

    service._publish = publish
    with pytest.raises(OSError, match="alert store"):
        service.process_snapshot(quotes(), NOW.date(), now=NOW)
    event_id = service.events(NOW.date())[0]["id"]
    service.process_snapshot(quotes(), NOW.date(), now=NOW)
    service.process_snapshot(quotes(), NOW.date(), now=NOW)
    assert attempts == [[event_id], [event_id]]
    assert [event["id"] for event in published] == [event_id]
    assert [event["id"] for event in service.events(NOW.date())] == [event_id]


def test_pending_publication_survives_same_day_restart(service, monkeypatch):
    service._publish = lambda _: (_ for _ in ()).throw(OSError("temporarily unavailable"))
    with pytest.raises(OSError):
        service.process_snapshot(quotes(), NOW.date(), now=NOW)
    event_id = service.events(NOW.date())[0]["id"]
    published = []
    reopened = fb.FirstBoardService(service.repo, history_loader=lambda *_: pl.DataFrame(),
                                    publish=published.extend, quote_service=service._quote_service)
    monkeypatch.setattr(reopened, "_prepare", lambda *_: None)
    reopened._candidates, reopened._environment, reopened._sectors = (
        service._candidates, service._environment, service._sectors)
    reopened.process_snapshot(quotes(), NOW.date(), now=NOW)
    assert [event["id"] for event in published] == [event_id]
    assert len(reopened.events(NOW.date())) == 1
    state = json.loads((reopened.root / "events" / f"{NOW.date()}.json").read_text(encoding="utf-8"))
    assert state["pending_alert_ids"] == []


def test_legacy_day_without_pending_ids_does_not_replay_history(service, monkeypatch):
    service.process_snapshot(quotes(), NOW.date(), now=NOW)
    path = service.root / "events" / f"{NOW.date()}.json"
    legacy = json.loads(path.read_text(encoding="utf-8"))
    legacy.pop("pending_alert_ids", None)
    path.write_text(json.dumps(legacy), encoding="utf-8")
    published = []
    reopened = fb.FirstBoardService(service.repo, history_loader=lambda *_: pl.DataFrame(),
                                    publish=published.extend)
    monkeypatch.setattr(reopened, "_prepare", lambda *_: None)
    reopened._candidates, reopened._environment, reopened._sectors = (
        service._candidates, service._environment, service._sectors)
    reopened.process_snapshot(quotes(), NOW.date(), now=NOW)
    assert not published
    assert len(reopened.events(NOW.date())) == 1
    assert json.loads(path.read_text(encoding="utf-8")) == legacy


@pytest.mark.parametrize("pending", [["unknown"], [42], "not-a-list"])
def test_invalid_pending_ids_preserve_evidence_file(service, pending):
    service.process_snapshot(quotes(), NOW.date(), now=NOW)
    path = service.root / "events" / f"{NOW.date()}.json"
    state = json.loads(path.read_text(encoding="utf-8"))
    state["pending_alert_ids"] = pending
    content = json.dumps(state)
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ValueError, match="信号记录损坏"):
        service.events(NOW.date())
    assert path.read_text(encoding="utf-8") == content


def test_pending_alert_waits_for_realtime_capability_before_retry(service):
    service._publish = lambda _: (_ for _ in ()).throw(OSError("temporarily unavailable"))
    with pytest.raises(OSError):
        service.process_snapshot(quotes(), NOW.date(), now=NOW)
    original_id = service.events(NOW.date())[0]["id"]
    published = []
    service._publish = published.extend
    state = {"enabled": True, "realtime_allowed": False}
    service._quote_service = SimpleNamespace(status=lambda: state)
    service.process_snapshot(quotes(), NOW.date(), now=NOW)
    assert not published
    state["realtime_allowed"] = True
    service.process_snapshot(quotes(), NOW.date(), now=NOW)
    assert [event["id"] for event in published] == [original_id]
    assert len(service.events(NOW.date())) == 1


def test_pending_alerts_share_the_existing_daily_event_bound(service, monkeypatch):
    monkeypatch.setattr(fb, "_MAX_EVENTS_PER_DAY", 1)
    service._publish = lambda _: (_ for _ in ()).throw(OSError("temporarily unavailable"))
    with pytest.raises(OSError):
        service.process_snapshot(quotes(), NOW.date(), now=NOW)
    path = service.root / "events" / f"{NOW.date()}.json"
    original = path.read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="当日事件超过保护上限"):
        service.process_snapshot(quotes(raw_close=11.0, close=11.0, raw_high=11.0), NOW.date(), now=NOW)
    assert path.read_text(encoding="utf-8") == original
    assert len(json.loads(original)["pending_alert_ids"]) == 1


@pytest.mark.parametrize("change", ["expired", "disabled", "notify_off", "revision", "superseded"])
def test_pending_alert_does_not_outlive_evidence_or_configuration(service, monkeypatch, change):
    service._publish = lambda _: (_ for _ in ()).throw(OSError("temporarily unavailable"))
    with pytest.raises(OSError):
        service.process_snapshot(quotes(), NOW.date(), now=NOW)
    original_id = service.events(NOW.date())[0]["id"]
    published = []
    service._publish = published.extend
    now = NOW
    overrides = {}
    if change == "expired":
        now += timedelta(seconds=31)
        monkeypatch.setattr(fb, "_now", lambda: now)
    elif change == "superseded":
        overrides = {"raw_close": 11.0, "close": 11.0, "raw_high": 11.0}
    else:
        values = {"enabled": False} if change == "disabled" else {"notify": False} if change == "notify_off" else {"max_positions": 3}
        service.save_config({**service.get_config(), **values})
    service.process_snapshot(quotes(now, **overrides), now.date(), now=now)
    assert original_id not in [event["id"] for event in published]
    state = json.loads((service.root / "events" / f"{NOW.date()}.json").read_text(encoding="utf-8"))
    assert state["pending_alert_ids"] == []


def test_stop_during_prepare_prevents_late_events_and_new_work(service, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original_prepare = service._prepare
    published = []
    service._publish = published.extend

    def slow_prepare(*args):
        entered.set()
        assert release.wait(timeout=5)
        original_prepare(*args)

    monkeypatch.setattr(service, "_prepare", slow_prepare)
    service.offer_snapshot(quotes(), NOW.date())
    service.start()
    thread = service._thread
    assert entered.wait(timeout=2)
    join = thread.join
    # Exercise the timeout branch without spending five seconds blocked on a repository read.
    monkeypatch.setattr(thread, "join", lambda timeout: None)
    try:
        service.stop()
        service.offer_snapshot(quotes(), NOW.date())
        service.request_refresh(force=True)
    finally:
        release.set()
        join(timeout=2)
    assert not thread.is_alive()
    assert service.events(NOW.date()) == []
    assert not published
    assert not (service.root / "events").exists()
    assert service._pending is None
    assert not service._refresh_requested


def test_worker_stops_without_blocking_process(service):
    service.start()
    thread = service._thread
    service.stop()
    assert thread is not None and not thread.is_alive()


def test_simulated_order_uses_live_event_and_prevents_duplicates(service):
    paper.create_account(service.repo.store.data_dir, 100_000)
    service.save_config({**service.get_config(), "paper_account_id": "default"})
    service.process_snapshot(quotes(), NOW.date(), now=NOW)
    event_id = service.snapshot()["rows"][0]["event_id"]
    order = service.place_order(event_id, "buy", qty=100, now=NOW)
    assert order["source"] == f"first_board:{event_id}"
    assert order["qty"] == 100 and order["status"] == "pending"
    with pytest.raises(ValueError, match="已提交"):
        service.place_order(event_id, "buy", qty=100, now=NOW)


def test_simulated_order_rechecks_quote_age_and_account_queue(service):
    paper.create_account(service.repo.store.data_dir, 100_000)
    service.save_config({**service.get_config(), "paper_account_id": "default"})
    service.process_snapshot(quotes(), NOW.date(), now=NOW)
    event_id = service.snapshot()["rows"][0]["event_id"]
    with pytest.raises(ValueError, match="行情已过期"):
        service.place_order(event_id, "buy", qty=100, now=NOW + timedelta(seconds=31))
    paper.update_settings(service.repo.store.data_dir, queue_limit_orders=True)
    with pytest.raises(ValueError, match="排队"):
        service.place_order(event_id, "buy", qty=100, now=NOW)
    assert paper.load_orders(service.repo.store.data_dir) == []


def test_research_rejects_concurrent_run(service):
    service._research_lock.acquire()
    try:
        with pytest.raises(ValueError, match="正在运行"):
            service.research(start=date(2026, 1, 1), end=date(2026, 1, 2))
    finally:
        service._research_lock.release()


def test_insufficient_industry_coverage_blocks_live_buy_until_recovered(service, monkeypatch):
    service.save_config({**service.get_config(), "require_sector_confirmation": True})
    original = service._prepare
    symbols = [f"{600000 + i}.SH" for i in range(100)]

    def prepare(*args):
        original(*args)
        service._sectors = dict.fromkeys(symbols, "金融-银行")

    monkeypatch.setattr(service, "_prepare", prepare)
    for fresh_count, expected in [(3, False), (80, True), (3, False)]:
        frame = pl.concat([quotes(symbol=symbol) for symbol in symbols[:fresh_count]])
        service.process_snapshot(frame, NOW.date(), now=NOW)
        row = service.snapshot()["rows"][0]
        assert row["can_buy"] is expected
        assert row["sector_confirmation"]["coverage"] == fresh_count / 100
        assert row["sector_confirmation"]["member_count"] == 100
        if not expected:
            assert any("覆盖率" in reason for reason in row["blocked_reasons"])


@pytest.mark.parametrize("manual_price,current_price,should_exit", [(12., 10.5, False), (8., 9.5, True)])
def test_live_exit_and_order_use_first_board_batch_cost(service, monkeypatch, manual_price, current_price, should_exit):
    data_dir = service.repo.store.data_dir
    paper.create_account(data_dir, 100_000, slippage_bps=0)
    service.save_config({**service.get_config(), "paper_account_id": "default"})
    for day, price, source in [(date(2026, 10, 14), 10., "first_board:entry"),
                               (date(2026, 10, 15), manual_price, "manual")]:
        monkeypatch.setattr(paper, "cn_today", lambda day=day: day)
        _, error = paper.create_order(data_dir, SYMBOL, "buy", qty=200, ref_price=price, source=source)
        assert error is None
        assert len(paper.evaluate_intraday(data_dir, {SYMBOL: price})) == 1
    monkeypatch.setattr(paper, "cn_today", lambda: NOW.date())
    service.process_snapshot(quotes(raw_close=current_price, close=current_price), NOW.date(), now=NOW)
    exits = [event for event in service.events(NOW.date()) if event["event_type"] == "exit_candidate"]
    assert bool(exits) is should_exit
    if should_exit:
        assert exits[0]["evidence"]["qty"] == 200
        order = service.place_order(exits[0]["id"], "sell", now=NOW)
        assert order["qty"] == 200 and order["status"] == "pending"
