import copy
import gzip
import json
import threading
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import polars as pl
import pytest

from app.market_time import CN_TZ
from app.services import first_board as fb
from app.services import first_board_attention_data as attention_data
from app.services.ext_data import ExtConfig, ExtConfigStore, ExtField, write_ext_parquet
from app.services.first_board_attention import AttentionEvaluator
from app.services.first_board_observations import FirstBoardObservationStore
from app.services.minute_adjust import mark_minute_basis_raw

NOW = datetime(2026, 10, 21, 10, 0, 17, tzinfo=CN_TZ)
SYMBOLS = ["600000.SH", "000001.SZ", "000002.SZ"]
VERSION = "a" * 64
GROUPS = [{"id": "concept:test", "kind": "concept", "name": "测试概念",
           "source_id": "test", "field": "concept", "members": SYMBOLS}]


def context(*, positive=True, now=NOW):
    return {
        "schema_version": 1, "rule_version": "attention-v1", "as_of": now.isoformat(),
        "membership_version": VERSION, "status": "ready" if positive is not None else "unavailable",
        "strong_sector": positive, "leader_follow": positive, "intraday_rising": positive,
        "priority": 2 if positive else 0 if positive is False else None,
        "group": "S1F1" if positive else "S0F0" if positive is False else None,
        "groups": [{"id": "concept:test", "name": "测试概念", "kind": "concept",
                    "source_id": "test", "field": "concept", "strong": positive,
                    "follow": positive, "leader": {"symbol": "600001.SH", "at_limit": True}}],
        "intraday": {"as_of": now.isoformat(), "return_5m": .01, "above_vwap": True},
        "reasons": [],
    }


def quotes(now=NOW, *, price=10.8):
    return pl.DataFrame([{
        "symbol": symbol, "date": now.date(), "name": f"测试股{index}",
        "quote_ts": int(now.timestamp() * 1000), "raw_close": price, "close": price,
        "raw_high": price + .01, "prev_close": 10.0, "limit_up_price": 10.995,
        "turnover_rate": 5.0, "amount": 80_000_000.0, "change_pct": price / 10 - 1,
    } for index, symbol in enumerate(SYMBOLS)])


@pytest.fixture
def rig(tmp_path, monkeypatch):
    clock = {"now": NOW}
    monkeypatch.setattr(fb, "_now", lambda: clock["now"])
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    source_status = {"enabled": True, "realtime_allowed": True, "mode": "poll", "paused": False}
    published = []
    svc = fb.FirstBoardService(
        repo, history_loader=lambda *_: pl.DataFrame(), publish=published.extend,
        quote_service=SimpleNamespace(status=lambda: source_status),
    )
    svc.save_config({**svc.get_config(), "enabled": True, "require_sector_confirmation": False})
    candidate_rows = [{
        "symbol": symbol, "name": f"测试股{index}", "pattern": "platform", "pattern_label": "平台突破",
        "reference_date": date(2026, 10, 20), "reference_price": 10.0,
        "reference_adjusted_close": 10.0, "breakout_price": 10.2,
        "history_bars": 65, "first_board_count": 0,
    } for index, symbol in enumerate(SYMBOLS)]

    def prepare(_day, _config):
        svc._candidates = pl.DataFrame(candidate_rows)
        svc._environment = {"allowed": True, "date": "2026-10-20", "state": "range"}
        svc._sectors = dict.fromkeys(SYMBOLS, "测试行业")

    monkeypatch.setattr(svc, "_prepare", prepare)
    data = SimpleNamespace(
        groups=copy.deepcopy(GROUPS), version=VERSION, reason=None,
        prepare=lambda **_: None,
        minutes=lambda *_args, **_kwargs: (pl.DataFrame(), False, "测试无分钟"),
    )
    contexts = {symbol: context() for symbol in SYMBOLS}

    def evaluate(_current, _groups, candidate_symbols, _minute_df, **_kwargs):
        return {symbol: copy.deepcopy(contexts[symbol]) for symbol in candidate_symbols}

    svc._attention_data = data
    svc._attention_evaluator = SimpleNamespace(evaluate=evaluate)
    observation_root = tmp_path / "observations"
    svc._observation_store = FirstBoardObservationStore(observation_root)
    return SimpleNamespace(svc=svc, clock=clock, data=data, contexts=contexts,
                           root=observation_root, published=published, candidates=candidate_rows,
                           source_status=source_status)


def samples(rig):
    return [json.loads(gzip.decompress(path.read_bytes()))
            for path in sorted(rig.root.glob("*/*/[0-9][0-9][0-9][0-9].json.gz"))]


def test_strong_attention_never_promotes_a_watch_candidate_to_a_buy(rig):
    rig.svc.process_snapshot(quotes(price=10.5), NOW.date(), now=NOW)
    rows = rig.svc.snapshot()["rows"]
    assert len(rows) == 3
    assert all(row["attention_context"]["group"] == "S1F1" for row in rows)
    assert all(row["state"] == "watch" and row["can_buy"] is False for row in rows)
    assert rig.published == []


@pytest.mark.parametrize("failed_stage", ["evaluate", "minutes", "membership", "record"])
def test_optional_attention_failures_leave_existing_buy_logic_available(rig, failed_stage):
    def fail(*_args, **_kwargs):
        raise OSError("synthetic optional subsystem failure")

    if failed_stage == "evaluate":
        rig.svc._attention_evaluator.evaluate = fail
    elif failed_stage == "minutes":
        rig.data.minutes = fail
    elif failed_stage == "membership":
        rig.svc._observation_store.write_membership = fail
    else:
        rig.svc._observation_store.record = fail
    rig.svc.process_snapshot(quotes(), NOW.date(), now=NOW)
    snapshot = rig.svc.snapshot()
    assert snapshot["status"] == "ready"
    assert all(row["state"] == "approaching" and row["can_buy"] for row in snapshot["rows"])
    assert len(rig.published) == 3
    if failed_stage in {"membership", "record"}:
        assert snapshot["attention_sampling"]["status"] == "error"
    else:
        assert all(row["attention_context"]["strong_sector"] is None for row in snapshot["rows"])


@pytest.mark.parametrize("transition", ["expired", "closed", "error", "paused", "next_day"])
def test_non_current_snapshots_never_keep_positive_attention_badges(rig, transition):
    rig.svc.process_snapshot(quotes(), NOW.date(), now=NOW)
    assert rig.svc.snapshot()["rows"][0]["attention_context"]["priority"] == 2
    if transition == "expired":
        rig.clock["now"] = NOW + timedelta(seconds=31)
    elif transition == "closed":
        rig.clock["now"] = NOW.replace(hour=12)
    elif transition == "error":
        rig.svc._snapshot["status"] = "error"
    elif transition == "paused":
        rig.source_status["paused"] = True
    else:
        rig.clock["now"] = NOW + timedelta(days=1)
    for row in rig.svc.snapshot()["rows"]:
        attention = row["attention_context"]
        assert attention["status"] == "stale"
        assert attention["priority"] is None and attention["group"] is None
        assert attention["strong_sector"] is None
        assert attention["leader_follow"] is None
        assert attention["intraday_rising"] is None
        assert not any(group.get("strong") is True or group.get("follow") is True
                       for group in attention["groups"])
        assert row["can_buy"] is False
    # Expiring a UI copy must preserve the original evidence for research.
    assert samples(rig)[0]["rows"][0]["attention_context"]["strong_sector"] is True


def test_sampling_keeps_false_unknown_and_multiple_patterns_in_denominator(rig):
    rig.contexts[SYMBOLS[1]] = context(positive=False)
    rig.contexts[SYMBOLS[2]] = context(positive=None)
    rig.candidates.append({**rig.candidates[0], "pattern": "oversold", "pattern_label": "超跌反弹"})
    rig.svc.process_snapshot(quotes(price=9.9), NOW.date(), now=NOW)
    sample = samples(rig)[0]
    assert sample["candidate_count"] == 4
    assert sample["symbol_count"] == 3
    indexed = {row["symbol"]: row for row in sample["rows"]}
    assert indexed[SYMBOLS[0]]["patterns"] == ["platform", "oversold"]
    assert indexed[SYMBOLS[1]]["attention_context"]["strong_sector"] is False
    assert indexed[SYMBOLS[2]]["attention_context"]["strong_sector"] is None
    assert all(row["can_buy"] is False for row in sample["rows"])
    assert rig.svc.snapshot()["rows"] == []
    assert rig.svc.snapshot()["coverage"]["candidate_count"] == 0
    assert rig.svc.snapshot()["coverage"]["symbol_count"] == 0
    assert sample["metadata"]["coverage"]["candidate_count"] == 4
    assert sample["metadata"]["coverage"]["symbol_count"] == 3
    assert sample["membership_version"] == VERSION
    assert sample["metadata"]


def test_service_sampling_is_once_per_anchor_without_forging_observation_time(rig):
    for offset in (0, 15, 60, 300):
        now = NOW + timedelta(seconds=offset)
        rig.clock["now"] = now
        rig.svc.process_snapshot(quotes(now), now.date(), now=now)
    saved = samples(rig)
    assert len(saved) == 2
    assert [sample["observed_at"] for sample in saved] == [NOW.isoformat(), (NOW + timedelta(minutes=5)).isoformat()]
    assert [sample["anchor"] for sample in saved] == [NOW.replace(second=0).isoformat(),
                                                       (NOW + timedelta(minutes=5)).replace(second=0).isoformat()]
    assert rig.svc.snapshot()["attention_sampling"]["sample_count"] == 2


@pytest.mark.parametrize("transition", ["stop", "revision"])
def test_inflight_attention_cannot_sample_after_stop_or_config_change(rig, transition):
    evaluate = rig.svc._attention_evaluator.evaluate

    def interleave(*args, **kwargs):
        result = evaluate(*args, **kwargs)
        if transition == "stop":
            rig.svc.stop()
        else:
            rig.svc.save_config({**rig.svc.get_config(), "max_positions": 3})
        return result

    rig.svc._attention_evaluator.evaluate = interleave
    rig.svc.process_snapshot(quotes(), NOW.date(), now=NOW)
    assert samples(rig) == []
    assert rig.published == []


def test_disabled_monitor_does_not_collect_research_samples(rig):
    rig.svc.save_config({**rig.svc.get_config(), "enabled": False})
    rig.svc.process_snapshot(quotes(), NOW.date(), now=NOW)
    assert samples(rig) == []
    assert rig.svc.snapshot()["status"] == "disabled"


def test_slow_computation_does_not_backfill_an_anchor_that_has_already_passed(rig):
    evaluate = rig.svc._attention_evaluator.evaluate

    def delayed(*args, **kwargs):
        result = evaluate(*args, **kwargs)
        rig.clock["now"] = NOW + timedelta(minutes=1)
        return result

    rig.svc._attention_evaluator.evaluate = delayed
    rig.svc.process_snapshot(quotes(), NOW.date(), now=NOW)
    assert samples(rig) == []


def test_notification_failure_does_not_remove_the_research_denominator(rig):
    def fail(_events):
        raise OSError("notification unavailable")

    rig.svc._publish = fail
    with pytest.raises(OSError, match="notification unavailable"):
        rig.svc.process_snapshot(quotes(), NOW.date(), now=NOW)
    assert samples(rig)[0]["symbol_count"] == 3


@pytest.mark.parametrize("delay", [10, 31])
def test_sampling_uses_real_available_time_and_expires_delayed_quote_evidence(rig, delay):
    evaluate = rig.svc._attention_evaluator.evaluate

    def delayed(*args, **kwargs):
        result = evaluate(*args, **kwargs)
        rig.clock["now"] = NOW + timedelta(seconds=delay)
        return result

    rig.svc._attention_evaluator.evaluate = delayed
    rig.svc.process_snapshot(quotes(), NOW.date(), now=NOW)
    sample = samples(rig)[0]
    assert sample["observed_at"] == rig.clock["now"].isoformat()
    assert sample["metadata"]["evaluated_at"] == NOW.isoformat()
    if delay > 30:
        assert all(row["attention_context"]["status"] == "stale" for row in sample["rows"])
        assert all(row["attention_context"]["strong_sector"] is None for row in sample["rows"])
        assert all(row["can_buy"] is False for row in sample["rows"])
        assert sample["metadata"]["coverage"]["fresh_count"] == 0
    else:
        assert all(row["attention_context"]["strong_sector"] is True for row in sample["rows"])


def test_missing_membership_still_collects_unknown_observations(rig):
    rig.data.groups, rig.data.version, rig.data.reason = [], None, "板块成员读取失败"
    rig.svc._attention_evaluator = AttentionEvaluator()
    rig.svc.process_snapshot(quotes(), NOW.date(), now=NOW)
    sample = samples(rig)[0]
    assert sample["membership_version"] is None
    assert sample["symbol_count"] == 3
    assert all(row["attention_context"]["strong_sector"] is None for row in sample["rows"])
    assert sample["metadata"]["membership_reason"] == "板块成员读取失败"
    assert sample["metadata"]["minute_price_basis"] == "unknown"
    assert sample["metadata"]["minute_reason"] == "测试无分钟"


def test_research_disk_work_does_not_hold_the_snapshot_lock(rig):
    original_record = rig.svc._observation_store.record
    observed = []

    def while_writing(*args, **kwargs):
        reader = threading.Thread(target=lambda: observed.append(rig.svc.snapshot()))
        reader.start()
        reader.join(timeout=1)
        assert not reader.is_alive(), "研究写入不应阻塞快照读取锁"
        return original_record(*args, **kwargs)

    rig.svc._observation_store.record = while_writing
    rig.svc.process_snapshot(quotes(), NOW.date(), now=NOW)
    assert len(observed) == 1 and observed[0]["status"] == "ready"


def test_real_catalog_adapter_and_evaluator_flow_into_immutable_samples(rig, monkeypatch):
    data_dir = rig.svc.repo.store.data_dir
    hot = [SYMBOLS[0], SYMBOLS[1], "600001.SH", "600002.SH", "600003.SH"]
    cold = [SYMBOLS[2], "600004.SH", "600005.SH", "600006.SH", "600007.SH"]
    all_symbols = hot + cold
    config = ExtConfig(id="attention_members", label="板块成员", mode="snapshot", fields=[
        ExtField("symbol", "string", "代码"), ExtField("concept", "string", "概念"),
    ])
    ExtConfigStore(data_dir).upsert(config)
    write_ext_parquet(pl.DataFrame({"symbol": all_symbols, "concept": ["科技"] * 5 + ["材料"] * 5}),
                      config, data_dir)
    rig.svc.repo.get_instruments = lambda: pl.DataFrame({
        "symbol": all_symbols, "name": [f"普通股{index}" for index in range(10)],
    })
    mark_minute_basis_raw(data_dir)
    minute_rows = []
    for symbol in SYMBOLS[:2]:
        for index in range(31):
            price = 10 + index * .002
            minute_rows.append({
                "symbol": symbol, "datetime": NOW.replace(hour=9, minute=30, second=0, tzinfo=None)
                + timedelta(minutes=index), "open": price, "high": price, "low": price,
                "close": price, "volume": 100., "amount": price * 100 * 100,
            })
    minute_path = data_dir / "kline_minute" / f"date={NOW.date()}" / "part.parquet"
    minute_path.parent.mkdir(parents=True)
    pl.DataFrame(minute_rows).write_parquet(minute_path)
    reads = []

    def get_minutes(symbols, days, *, asset_type):
        reads.append((symbols, days, asset_type))
        return pl.read_parquet(minute_path)

    rig.svc.repo.get_minute_by_dates = get_minutes
    monkeypatch.setattr(attention_data, "capability_gate", lambda name: (name == "minute", "test"))
    rig.svc._attention_data = attention_data.AttentionData(rig.svc.repo)
    rig.svc._attention_evaluator = AttentionEvaluator()
    base_prepare = rig.svc._prepare

    def prepare(day, rules):
        base_prepare(day, rules)
        rig.svc._attention_data.prepare(universe=rules.rules.universe)

    rig.svc._prepare = prepare
    for now in (NOW - timedelta(minutes=20), NOW):
        rig.clock["now"] = now
        frame = []
        for symbol in all_symbols:
            price = 11. if symbol == "600001.SH" else 10.8 if symbol in hot else 9.9
            frame.append({"symbol": symbol, "name": symbol, "date": now.date(),
                          "quote_ts": int(now.timestamp() * 1000), "raw_close": price,
                          "close": price, "raw_high": price, "prev_close": 10.,
                          "limit_up_price": 10.995, "change_pct": price / 10 - 1,
                          "turnover_rate": 5., "amount": 80_000_000.})
        rig.svc.process_snapshot(pl.DataFrame(frame), now.date(), now=now)
    rows = {row["symbol"]: row for row in rig.svc.snapshot()["rows"]}
    strong = rows[SYMBOLS[0]]["attention_context"]
    assert strong["group"] == "S1F1" and strong["intraday_rising"] is True
    assert strong["groups"][0]["member_count"] == 5
    assert strong["groups"][0]["leader"]["symbol"] == "600001.SH"
    assert strong["intraday"]["as_of"] == NOW.replace(hour=9, minute=59, second=0).isoformat()
    assert SYMBOLS[2] not in rows
    assert rig.svc.snapshot()["coverage"]["candidate_count"] == 2
    assert rig.svc.snapshot()["coverage"]["symbol_count"] == 2
    saved = samples(rig)
    assert len(saved) == 2 and saved[-1]["symbol_count"] == 3
    weak_row = next(row for row in saved[-1]["rows"] if row["symbol"] == SYMBOLS[2])
    weak = weak_row["attention_context"]
    assert weak["strong_sector"] is False and weak["intraday_rising"] is None
    assert weak_row["can_buy"] is False
    assert all(candidate["state"] == "watch" for candidate in weak_row["candidates"])
    assert saved[-1]["membership_version"] == rig.svc._attention_data.version
    assert saved[-1]["metadata"]["minute_price_basis"] == "raw"
    assert saved[-1]["metadata"]["minute_volume_unit"] == "lot"
    assert saved[-1]["metadata"]["minute_amount_unit"] == "yuan"
    assert all(read == (sorted(SYMBOLS), [NOW.date()], "stock") for read in reads)
    assert len(reads) == 2
