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


@pytest.mark.parametrize(("price", "expected_count"), [(11.0, 0), (11.29, 0), (11.3, 1), (11.6, 1)])
def test_snapshot_minimum_change_uses_twenty_percent_threshold(service, monkeypatch, price, expected_count):
    prepare = service._prepare

    def prepare_chinext(*args):
        prepare(*args)
        service._candidates = service._candidates.with_columns(symbol=pl.lit("300001.SZ"))

    monkeypatch.setattr(service, "_prepare", prepare_chinext)
    service.process_snapshot(quotes(symbol="300001.SZ", raw_close=price, close=price, raw_high=price,
                                    limit_up_price=11.995), NOW.date(), now=NOW)
    assert service.snapshot()["coverage"]["candidate_count"] == expected_count


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


def test_feedback_gate_is_optional_and_needs_measured_feedback(service, monkeypatch):
    service.process_snapshot(quotes(), NOW.date(), now=NOW)
    row = service.snapshot()["rows"][0]
    assert row["can_buy"] and "feedback_gate" not in row["evidence"]
    service.save_config({**service.get_config(), "feedback_gate": True})
    service.process_snapshot(quotes(), NOW.date(), now=NOW)
    row = service.snapshot()["rows"][0]
    assert not row["can_buy"] and not row["evidence"]["feedback_gate"]["allowed"]
    assert any("昨日首板反馈" in reason for reason in row["blocked_reasons"])
    summary = {"sample_count": 12, "coverage": 1., "mean_change_pct": .02, "up_ratio": .6, "small_sample": False}
    feedback = {"status": "ready", "quote_mode": "live", "as_of": str(NOW.date()), "groups": {"all": summary}}
    monkeypatch.setattr(service._feedback, "evaluate", lambda *args, **kwargs: json.loads(json.dumps(feedback)))
    service.process_snapshot(quotes(), NOW.date(), now=NOW)
    assert service.snapshot()["rows"][0]["can_buy"]
    summary["up_ratio"] = .4
    service.process_snapshot(quotes(), NOW.date(), now=NOW)
    row = service.snapshot()["rows"][0]
    assert not row["can_buy"] and any("未达到反馈门槛" in reason for reason in row["blocked_reasons"])


@pytest.mark.parametrize("change", [
    {"status": "stale"}, {"quote_mode": "snapshot"}, {"as_of": "2026-10-15"},
    {"small_sample": True}, {"coverage": .5}, {"mean_change_pct": None},
])
def test_feedback_gate_fails_closed_without_same_day_live_sample(change):
    summary = {"sample_count": 12, "coverage": 1., "mean_change_pct": .02, "up_ratio": .6, "small_sample": False}
    feedback = {"status": "ready", "quote_mode": "live", "as_of": str(NOW.date()), "groups": {"all": summary}}
    config = fb.FirstBoardConfig(feedback_gate=True)
    assert fb._feedback_gate(feedback, config, NOW.date())["allowed"]
    for key, value in change.items():
        (feedback if key in feedback else summary)[key] = value
    gate = fb._feedback_gate(feedback, config, NOW.date())
    assert not gate["allowed"] and "无法确认" in gate["reason"]
    assert fb._feedback_gate(feedback, fb.FirstBoardConfig(), NOW.date()) is None


def test_first_approach_time_survives_restart_and_feeds_priority(service, monkeypatch):
    service.process_snapshot(quotes(), NOW.date(), now=NOW)
    assert service.snapshot()["rows"][0]["evidence"]["first_approach_at"] == NOW.isoformat()
    later = NOW + timedelta(minutes=20)
    monkeypatch.setattr(fb, "_now", lambda: later)
    reopened = fb.FirstBoardService(service.repo, history_loader=lambda *_: pl.DataFrame())
    monkeypatch.setattr(reopened, "_prepare", lambda *_: None)
    reopened._candidates, reopened._environment = service._candidates, service._environment
    reopened._sectors = service._sectors
    reopened.process_snapshot(quotes(now=later), later.date(), now=later)
    row = reopened.snapshot()["rows"][0]
    assert row["evidence"]["first_approach_at"] == NOW.isoformat()
    assert "首次临板为开盘后 30 分钟" in row["priority_score"]["parts"]["stock"]["reason"]


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
    snapshot = service.snapshot()
    assert snapshot["status"] == "waiting"
    row = snapshot["rows"][0]
    assert not row["can_buy"]
    assert service.events(NOW.date()) == []
    # 发布时与读取时都会检查实时行情, 同一原因只记录一次。
    reason = snapshot["message"]
    assert row["blocked_reasons"].count(reason) == 1
    assert reason in snapshot["global_blocked_reasons"]
    assert set(snapshot["global_blocked_reasons"]) <= set(row["blocked_reasons"])


def test_global_blocked_reasons_exclude_stock_specific_reasons(service):
    service.process_snapshot(quotes(), NOW.date(), now=NOW)
    snapshot = service.snapshot()
    assert snapshot["rows"][0]["can_buy"]
    assert snapshot["global_blocked_reasons"] == []

    service.save_config({**service.get_config(), "require_sector_confirmation": True})
    # 夹具的 _prepare 会重置环境为允许; 此处固定为弱势环境。
    service._prepare = lambda *_: None
    service._environment = {"allowed": False, "reason": "市场环境弱势, 暂停提示"}
    service.process_snapshot(quotes(), NOW.date(), now=NOW)
    snapshot = service.snapshot()
    row = snapshot["rows"][0]
    assert snapshot["global_blocked_reasons"] == ["市场环境弱势, 暂停提示"]
    stock_reasons = [item for item in row["blocked_reasons"] if item not in snapshot["global_blocked_reasons"]]
    assert stock_reasons and all("行业" in item or "同行" in item for item in stock_reasons)


def test_missing_quote_times_are_global_only_when_no_candidate_has_a_fresh_quote(service):
    old = int((NOW - timedelta(seconds=60)).timestamp() * 1000)
    service.process_snapshot(quotes(quote_ts=old), NOW.date(), now=NOW)
    snapshot = service.snapshot()
    assert "行情时间缺失、重复或已过期" in snapshot["global_blocked_reasons"]
    assert snapshot["rows"][0]["blocked_reasons"].count("行情时间缺失、重复或已过期") == 1

    other = quotes(symbol="600001.SH", quote_ts=old)
    service.process_snapshot(pl.concat([quotes(), other]), NOW.date(), now=NOW)
    assert "行情时间缺失、重复或已过期" not in service.snapshot()["global_blocked_reasons"]


def test_global_blocked_reasons_cover_closed_and_stale_snapshots(service, monkeypatch):
    service.process_snapshot(quotes(), NOW.date(), now=NOW)
    monkeypatch.setattr(fb, "_now", lambda: NOW + timedelta(seconds=31))
    stale = service.snapshot()
    assert stale["status"] == "stale"
    assert stale["global_blocked_reasons"] == ["行情已过期或当前不在连续竞价时段"]
    assert stale["rows"][0]["blocked_reasons"].count("行情已过期或当前不在连续竞价时段") == 1

    closed_at = NOW.replace(hour=16)
    monkeypatch.setattr(fb, "_now", lambda: closed_at)
    service.process_snapshot(quotes(now=closed_at), NOW.date(), now=closed_at)
    closed = service.snapshot()
    assert closed["status"] == "closed"
    assert "当前不在有效盘中时段" in closed["global_blocked_reasons"]
    for row in closed["rows"]:
        assert all(row["blocked_reasons"].count(item) == 1 for item in closed["global_blocked_reasons"])


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


def test_snapshot_reads_only_refresh_without_recent_evaluation(service, monkeypatch):
    clock = [fb.time.monotonic() + 100]
    monkeypatch.setattr(fb.time, "monotonic", lambda: clock[0])

    def passive_read() -> bool:
        service._refresh_requested = service._force_refresh_requested = False
        service.request_refresh()
        return service._refresh_requested

    assert passive_read()  # 尚未评估过: 页面读取需要补算
    service.process_snapshot(quotes(), NOW.date(), now=NOW)
    clock[0] += 10
    assert not passive_read()  # 刚由行情投递完成评估, 读取不再重复计算
    clock[0] += fb._PASSIVE_REFRESH_SECONDS
    assert passive_read()  # 盘后长时间无投递时低频补算
    service.process_snapshot(quotes(), NOW.date(), now=NOW)
    clock[0] += 10
    service.request_refresh(force=True)
    assert service._refresh_requested and service._force_refresh_requested
    other = fb.FirstBoardService(service.repo, history_loader=lambda *_: pl.DataFrame())
    other.save_config({**other.get_config(), "notify": False})
    clock[0] += 10
    service.get_config()  # 其他实例保存新版本后快照清空, 下一次读取须立即补算
    assert passive_read()


def test_returned_snapshot_and_events_are_isolated_copies(service):
    service.process_snapshot(quotes(), NOW.date(), now=NOW)
    snapshot = service.snapshot()
    snapshot["rows"][0]["attention_context"]["reasons"].append("调用方修改")
    snapshot["rows"][0]["evidence"]["environment"]["allowed"] = False
    events = service.events(NOW.date())
    events[0]["evidence"]["environment"]["allowed"] = False
    again = service.snapshot()["rows"][0]
    assert "调用方修改" not in again["attention_context"]["reasons"]
    assert again["evidence"]["environment"]["allowed"] is True
    assert service.events(NOW.date())[0]["evidence"]["environment"]["allowed"] is True


def test_multi_pattern_rows_share_consistent_context_expiry(service, monkeypatch):
    prepare = service._prepare

    def prepare_patterns(*args):
        prepare(*args)
        candidate = service._candidates.to_dicts()[0]
        service._candidates = pl.DataFrame([{**candidate, "pattern": p} for p in ("platform", "trend")])

    monkeypatch.setattr(service, "_prepare", prepare_patterns)
    service.process_snapshot(quotes(), NOW.date(), now=NOW)
    monkeypatch.setattr(fb, "_now", lambda: NOW + timedelta(seconds=31))
    rows = service.snapshot()["rows"]
    assert len(rows) == 2
    for row in rows:
        assert row["attention_context"]["status"] == "stale"
        assert row["attention_context"]["reasons"].count("行情已过期或当前不在有效观察时段") == 1
        assert not row["can_buy"]


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
    # 本股报价不计入同行覆盖: 80 只报价只覆盖 79/99 只同行。
    for fresh_count, expected in [(3, False), (80, False), (81, True), (3, False)]:
        frame = pl.concat([quotes(symbol=symbol) for symbol in symbols[:fresh_count]])
        service.process_snapshot(frame, NOW.date(), now=NOW)
        row = service.snapshot()["rows"][0]
        assert row["can_buy"] is expected
        assert row["sector_confirmation"]["coverage"] == pytest.approx((fresh_count - 1) / 99)
        assert row["sector_confirmation"]["member_count"] == 99
        if not expected:
            assert any("覆盖率" in reason for reason in row["blocked_reasons"])


@pytest.mark.parametrize("peer_changes,expected", [
    # 本股 +8% 不能替同行凑数: 只有 1 只同行涨 5% 时不得确认联动。
    ([.06, 0., 0.], False),
    ([.06, .05, 0.], True),
    # 本股涨幅不得抬高同行均值: 同行 2 只强势但均涨不足 1%。
    ([.05, .05, -.04, -.035], False),
])
def test_industry_confirmation_excludes_the_candidate_itself(service, monkeypatch, peer_changes, expected):
    service.save_config({**service.get_config(), "require_sector_confirmation": True})
    peers = [f"{600001 + i}.SH" for i in range(len(peer_changes))]
    original = service._prepare

    def prepare(*args):
        original(*args)
        service._sectors = dict.fromkeys([SYMBOL, *peers], "金融-银行")

    monkeypatch.setattr(service, "_prepare", prepare)
    frame = pl.concat([quotes(), *[quotes(symbol=symbol, change_pct=change)
                                   for symbol, change in zip(peers, peer_changes, strict=True)]])
    service.process_snapshot(frame, NOW.date(), now=NOW)
    row = service.snapshot()["rows"][0]
    sector = row["sector_confirmation"]
    assert row["can_buy"] is expected
    assert sector["confirmed"] is expected
    assert sector["excludes_candidate"] is True
    assert sector["member_count"] == len(peers) and sector["count"] == len(peers)
    assert sector["mean_change_pct"] == pytest.approx(sum(peer_changes) / len(peers))
    assert "剔除本股" in sector["reason"]
    if not expected:
        assert sector["reason"] in row["blocked_reasons"]


@pytest.mark.parametrize("manual_price,current_price,should_exit",[(12., 10.5, False), (8., 9.5, True)])
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


ROLE_PEERS = [f"60000{i}.SH" for i in range(1, 6)]


def _role_service(service, monkeypatch, roles):
    service.save_config({**service.get_config(), "stock_roles": roles, "core_top_n": 1})
    original = service._prepare

    def prepare(*args):
        original(*args)
        service._attention_data._membership = (
            [{"id": "g1", "kind": "concept", "name": "概念A", "source_id": "s", "field": "f",
              "members": [SYMBOL, *ROLE_PEERS]}], "v1", None)
        service._float_shares = {}

    monkeypatch.setattr(service, "_prepare", prepare)


def _role_quotes(top_peer_change):
    return pl.concat([quotes(), *[quotes(symbol=symbol, change_pct=change, amount=1e9)
                                  for symbol, change in zip(ROLE_PEERS, [top_peer_change, 0., 0., 0., 0.], strict=True)]])


def test_config_without_role_fields_observes_all_and_rejects_empty_roles(service):
    legacy = {key: value for key, value in service.get_config().items() if key not in {"stock_roles", "core_top_n"}}
    assert fb.FirstBoardConfig.model_validate(legacy).stock_roles is None
    with pytest.raises(ValueError):
        fb.FirstBoardConfig.model_validate({**legacy, "stock_roles": []})
    with pytest.raises(ValueError):
        fb.FirstBoardConfig.model_validate({**legacy, "stock_roles": ["leader", "leader"]})


def test_all_roles_keep_existing_behaviour_but_label_the_role(service, monkeypatch):
    _role_service(service, monkeypatch, None)
    service.process_snapshot(_role_quotes(.09), NOW.date(), now=NOW)
    snapshot = service.snapshot()
    row = snapshot["rows"][0]
    assert row["can_buy"] is True
    assert row["stock_role"]["leader"] is False
    assert snapshot["coverage"]["role_hidden_count"] == 0


def test_leader_filter_allows_sector_leader(service, monkeypatch):
    published = []
    service._publish = published.extend
    _role_service(service, monkeypatch, ["leader"])
    service.process_snapshot(_role_quotes(.01), NOW.date(), now=NOW)
    row = service.snapshot()["rows"][0]
    assert row["can_buy"] is True and row["stock_role"]["leader"] is True
    assert row["evidence"]["stock_role"]["matches"][0]["name"] == "概念A"
    assert len(published) == 1


def test_non_leader_is_hidden_without_events(service, monkeypatch):
    published = []
    service._publish = published.extend
    _role_service(service, monkeypatch, ["leader"])
    service.process_snapshot(_role_quotes(.09), NOW.date(), now=NOW)
    snapshot = service.snapshot()
    assert snapshot["rows"] == []
    assert snapshot["coverage"]["role_hidden_count"] == 1
    assert published == [] and service.events(NOW.date()) == []


def test_core_or_leader_any_selected_role_matches(service, monkeypatch):
    _role_service(service, monkeypatch, ["leader", "core"])
    # 非龙头, 但成交额 (8000 万 vs 同行 10 亿) 也不在前 1, 缺流通股本 → 中军待确认, 保留并阻断。
    service.process_snapshot(_role_quotes(.09), NOW.date(), now=NOW)
    row = service.snapshot()["rows"][0]
    assert row["can_buy"] is False
    assert any("待确认" in reason for reason in row["blocked_reasons"])
    service._float_shares = dict.fromkeys(ROLE_PEERS, 1e6) | {SYMBOL: 1e9}
    monkeypatch.setattr(service, "_prepare", lambda *_: None)
    service.process_snapshot(_role_quotes(.09), NOW.date(), now=NOW)
    row = service.snapshot()["rows"][0]
    assert row["can_buy"] is True and row["stock_role"]["core"] is True


def test_alerted_stock_that_loses_leadership_stays_visible_and_blocked(service, monkeypatch):
    published = []
    service._publish = published.extend
    _role_service(service, monkeypatch, ["leader"])
    service.process_snapshot(_role_quotes(.01), NOW.date(), now=NOW)
    service.process_snapshot(_role_quotes(.09), NOW.date(), now=NOW)
    row = service.snapshot()["rows"][0]
    assert row["can_buy"] is False
    assert any("不属于所选角色" in reason for reason in row["blocked_reasons"])
    assert [event["event_type"] for event in published] == ["buy_candidate", "approaching"]

def test_closed_session_labels_roles_from_latest_displayed_quotes(service, monkeypatch):
    _role_service(service, monkeypatch, None)
    closed = NOW.replace(hour=18)
    monkeypatch.setattr(fb, "_now", lambda: closed)
    last = closed.replace(hour=15)  # 收盘后报价已超过有效时间
    frame = pl.concat([quotes(now=last), *[quotes(now=last, symbol=symbol, change_pct=.0, amount=1e9)
                                             for symbol in ROLE_PEERS]])
    service.process_snapshot(frame, closed.date(), now=closed)
    row = service.snapshot()["rows"][0]
    assert row["can_buy"] is False
    assert row["stock_role"]["leader"] is True
    assert service.events(closed.date()) == []


def test_mainline_evidence_is_recorded_and_gate_is_opt_in(service):
    service.process_snapshot(quotes(), NOW.date(), now=NOW)
    row = service.snapshot()["rows"][0]
    # 默认不限制: 主线数据缺失只记为待确认, 不影响提示。
    assert row["can_buy"] and row["mainline"]["hit"] is None
    assert service.snapshot()["mainlines"] == {}

    service.save_config({**service.get_config(), "require_mainline": True})
    service.process_snapshot(quotes(), NOW.date(), now=NOW)
    row = service.snapshot()["rows"][0]
    assert not row["can_buy"] and any("主线归属待确认" in reason for reason in row["blocked_reasons"])

    item = {"kind": "concept", "name": "机器人", "rank": 1, "limit_up_count": 9.0, "max_boards": 4.0}
    service._mainlines = {"date": "2026-10-15", "status": "ready", "reason": "", "top_n": 5, "items": [item]}
    service.process_snapshot(quotes(), NOW.date(), now=NOW)
    row = service.snapshot()["rows"][0]
    assert not row["can_buy"] and any("不属于参考交易日" in reason for reason in row["blocked_reasons"])

    service._mainline_members = {SYMBOL: [item]}
    service.process_snapshot(quotes(), NOW.date(), now=NOW)
    row = service.snapshot()["rows"][0]
    assert row["can_buy"] and row["evidence"]["mainline"]["matches"][0]["name"] == "机器人"
    assert service.snapshot()["mainlines"]["items"][0]["name"] == "机器人"


def test_phase_and_mainline_settings_default_off_and_validate():
    config = fb.FirstBoardConfig()
    assert config.allowed_market_phases is None and config.require_mainline is False
    assert fb.FirstBoardConfig(allowed_market_phases=["ignite", "rally"]).allowed_market_phases == ["ignite", "rally"]
    for invalid in ([], ["rally", "rally"], ["boom"]):
        with pytest.raises(ValueError):
            fb.FirstBoardConfig(allowed_market_phases=invalid)
