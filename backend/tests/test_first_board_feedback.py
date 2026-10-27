from datetime import date, datetime, timedelta
from types import SimpleNamespace

import polars as pl
import pytest

from app.market_time import CN_TZ
from app.services.first_board_feedback import FirstBoardFeedback, expire_feedback

DAY = date(2026, 10, 26)
PREVIOUS = date(2026, 10, 23)
BEFORE = date(2026, 10, 22)
WARMUP = date(2026, 10, 21)
NOW = datetime(2026, 10, 26, 10, 0, tzinfo=CN_TZ)


def history_row(symbol, day, *, price=10.0, limit=10.995, name="测试股票"):
    return {"symbol": symbol, "date": day, "name": name,
            "raw_close": price, "limit_up_price": limit}


def history(symbols=("600000.SH", "300001.SZ")):
    return pl.DataFrame([
        row for symbol in symbols for row in (
            history_row(symbol, BEFORE), history_row(symbol, PREVIOUS, price=11.0),
        )
    ])


def quotes(now=NOW):
    return pl.DataFrame([
        {"symbol": "600000.SH", "date": DAY, "raw_close": 12.1, "close": 6.05,
         "open": 6.0, "change_pct": .1, "limit_up_price": 12.095,
         "quote_ts": int(now.timestamp() * 1000)},
        {"symbol": "300001.SZ", "date": DAY, "raw_close": 10.45, "close": 10.45,
         "open": 11.0, "change_pct": -.05, "limit_up_price": 13.195,
         "quote_ts": int(now.timestamp() * 1000)},
    ])


@pytest.fixture
def setup(tmp_path, monkeypatch):
    state = {"generation": "v1", "history": history(), "reads": 0}

    def read(*_args, **_kwargs):
        state["reads"] += 1
        return state["history"], state["generation"]

    repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        get_first_board_history_signature=lambda *_: state["generation"],
        get_first_board_history=read,
        get_index_daily=lambda *_args, **_kwargs: pl.DataFrame({"date": [WARMUP, BEFORE, PREVIOUS]}),
    )
    service = FirstBoardFeedback(repo)
    monkeypatch.setattr(service._membership, "prepare", lambda **_: None)
    service._membership._membership = ([], "members-v1", None)
    return service, state


def test_fixed_pool_and_fraction_units_with_adjusted_open(setup):
    service, _ = setup
    result = service.evaluate(quotes(), DAY, now=NOW, live=True)
    summary = result["groups"]["all"]
    assert result["sample_date"] == str(PREVIOUS)
    assert summary["sample_count"] == summary["quote_count"] == 2
    assert summary["mean_change_pct"] == pytest.approx(.025)
    assert summary["median_change_pct"] == pytest.approx(.025)
    assert summary["up_ratio"] == .5
    assert summary["loss_count"] == 1
    assert summary["limit_up_ratio"] == .5
    assert summary["mean_open_premium"] == pytest.approx(((6 / 6.05 * 1.1 - 1) + 0) / 2)
    assert summary["mean_open_change"] == pytest.approx(((6.05 / 6 - 1) - .05) / 2)
    assert result["groups"]["main"]["sample_count"] == 1
    assert result["groups"]["growth"]["sample_count"] == 1


def test_exact_two_session_pool_excludes_multiboard_st_bj_and_unknown(setup):
    service, state = setup
    rows = history().to_dicts()
    rows += [history_row("600001.SH", PREVIOUS, price=11)]  # missing T-2
    rows += [history_row("600002.SH", BEFORE, price=11), history_row("600002.SH", PREVIOUS, price=11)]
    rows += [history_row("600003.SH", d, price=11 if d == PREVIOUS else 10, name="ST测试")
             for d in (BEFORE, PREVIOUS)]
    rows += [history_row("920001.BJ", d, price=11 if d == PREVIOUS else 10) for d in (BEFORE, PREVIOUS)]
    rows += [history_row("600004.SH", BEFORE), history_row("600004.SH", PREVIOUS, price=11, limit=None)]
    state["history"] = pl.DataFrame(rows)
    result = service.evaluate(quotes(), DAY, now=NOW, live=True)
    assert [r["symbol"] for r in result["rows"]] == ["300001.SZ", "600000.SH"]
    assert result["history_excluded_count"] == 2


def test_missing_stale_duplicate_quotes_never_shrink_sample_or_become_zero(setup):
    service, _ = setup
    q = quotes().head(1)
    result = service.evaluate(pl.concat([q, q]), DAY, now=NOW, live=True)
    summary = result["groups"]["all"]
    assert summary["sample_count"] == 2
    assert summary["quote_count"] == 0
    assert summary["mean_change_pct"] is None
    assert summary["limit_up_ratio"] is None
    assert {r["status"] for r in result["rows"]} == {"invalid", "missing"}
    stale = service.evaluate(quotes(NOW - timedelta(seconds=31)), DAY, now=NOW, live=True)
    assert stale["groups"]["all"]["quote_count"] == 0


def test_snapshot_retains_after_close_and_unknown_limit_does_not_count_as_failure(setup):
    service, _ = setup
    q = quotes().with_columns(pl.lit(None, dtype=pl.Float64).alias("limit_up_price"))
    result = service.evaluate(q, DAY, now=NOW.replace(hour=16), live=False)
    summary = result["groups"]["all"]
    assert result["quote_mode"] == "snapshot"
    assert summary["quote_count"] == 2
    assert summary["limit_known_count"] == 0
    assert summary["limit_up_ratio"] is None
    assert "收盘确认" not in result["message"]


def test_cache_generation_invalidates_without_history_scan_each_quote(setup):
    service, state = setup
    service.evaluate(quotes(), DAY, now=NOW, live=True)
    service.evaluate(quotes(), DAY, now=NOW, live=True)
    assert state["reads"] == 1
    state["generation"] = "v2"
    state["history"] = history(("600000.SH",))
    result = service.evaluate(quotes(), DAY, now=NOW, live=True)
    assert state["reads"] == 2
    assert result["groups"]["all"]["sample_count"] == 1


def test_history_unavailable_and_generation_change_fail_closed(setup):
    service, state = setup
    state["history"] = None
    assert service.evaluate(quotes(), DAY, now=NOW, live=True)["status"] == "unavailable"
    state["history"] = history()
    state["generation"] = "v2"
    service.repo.get_first_board_history = lambda *_args, **_kwargs: (
        state.update(generation="v3") or (state["history"], state["generation"])
    )
    assert service.evaluate(quotes(), DAY, now=NOW, live=True)["status"] == "unavailable"


def test_sector_cohort_and_whole_group_means_are_distinct(setup):
    service, _ = setup
    members = ["600000.SH", "300001.SZ", "600010.SH", "600011.SH", "600012.SH"]
    service._membership._membership = ([{"id": "test", "kind": "industry", "name": "测试行业",
                                         "members": members}], "members-v1", None)
    q = quotes().to_dicts()
    q += [{**q[0], "symbol": symbol, "change_pct": -.1, "raw_close": 9.9}
          for symbol in members[2:]]
    result = service.evaluate(pl.DataFrame(q), DAY, now=NOW, live=True)
    sector = result["sectors"][0]
    assert sector["sample_count"] == 2
    assert sector["mean_change_pct"] == pytest.approx(.025)
    assert sector["sector_mean_change_pct"] == pytest.approx(-.05)
    assert sector["coverage"] == 1
    assert sector["member_count"] == 5
    partial = service.evaluate(quotes(), DAY, now=NOW, live=True)["sectors"][0]
    assert partial["status"] == "insufficient"
    assert partial["sector_mean_change_pct"] is None


@pytest.mark.parametrize("value", [None, float("nan"), float("inf"), -1.0])
def test_invalid_return_is_not_an_observation(setup, value):
    service, _ = setup
    q = quotes().with_columns(pl.lit(value, dtype=pl.Float64).alias("change_pct"))
    result = service.evaluate(q, DAY, now=NOW, live=True)
    assert result["groups"]["all"]["quote_count"] == 0
    assert result["groups"]["all"]["mean_change_pct"] is None


def test_null_date_and_boolean_price_cannot_appear_valid(setup):
    service, _ = setup
    q = quotes().with_columns(pl.lit(None, dtype=pl.Date).alias("date"))
    assert service.evaluate(q, DAY, now=NOW, live=True)["groups"]["all"]["quote_count"] == 0
    q = quotes().with_columns(pl.lit(True).alias("raw_close"))
    assert service.evaluate(q, DAY, now=NOW, live=True)["groups"]["all"]["quote_count"] == 0


def test_loss_boundary_and_partial_limit_knowledge(setup):
    service, _ = setup
    q = quotes().with_columns(
        pl.when(pl.col("symbol") == "300001.SZ").then(-.04999999999999999)
        .otherwise(pl.col("change_pct")).alias("change_pct"),
        pl.when(pl.col("symbol") == "300001.SZ").then(None)
        .otherwise(pl.col("limit_up_price")).alias("limit_up_price"),
    )
    result = service.evaluate(q, DAY, now=NOW, live=True)
    assert result["groups"]["all"]["loss_count"] == 1
    assert result["groups"]["all"]["limit_known_count"] == 1
    assert result["groups"]["all"]["limit_up_count"] == 1
    assert result["groups"]["all"]["limit_up_ratio"] is None


def test_live_expiration_retains_dated_values_and_snapshot_does_not_expire(setup):
    service, _ = setup
    result = service.evaluate(quotes(), DAY, now=NOW, live=True)
    expire_feedback(result, now=NOW + timedelta(seconds=31))
    assert result["status"] == "stale"
    assert result["groups"]["all"]["mean_change_pct"] == pytest.approx(.025)
    snapshot = service.evaluate(quotes(), DAY, now=NOW.replace(hour=16), live=False)
    expire_feedback(snapshot, now=NOW + timedelta(days=1))
    assert snapshot["status"] == "ready"
    assert snapshot["as_of"] == str(DAY)


@pytest.mark.parametrize("hour", [14, 15, 23])
def test_history_intraday_snapshots_require_completed_session(setup, hour):
    service, state = setup
    frame = state["history"].to_dicts()
    for row in frame:
        row["quote_ts"] = int(datetime.combine(row["date"], NOW.time(), CN_TZ).replace(hour=hour).timestamp() * 1000)
    state["history"] = pl.DataFrame(frame)
    result = service.evaluate(quotes(), DAY, now=NOW, live=True)
    assert result["groups"]["all"]["sample_count"] == (0 if hour < 15 else 2)
    assert result["history_excluded_count"] == (2 if hour < 15 else 0)


def test_unfinished_history_with_unknown_limit_is_reported_as_incomplete(setup):
    service, state = setup
    frame = state["history"].to_dicts()
    for row in frame:
        if row["date"] == PREVIOUS:
            row["quote_ts"] = int(datetime.combine(PREVIOUS, NOW.time(), CN_TZ).replace(hour=14).timestamp() * 1000)
            row["limit_up_price"] = None
        else:
            row["quote_ts"] = None
    state["history"] = pl.DataFrame(frame)
    result = service.evaluate(quotes(), DAY, now=NOW, live=True)
    assert result["status"] == "partial"
    assert result["history_excluded_count"] == 2
    assert result["groups"]["all"]["sample_count"] == 0


def test_missing_previous_history_and_null_yesterday_limit_are_reported(setup):
    service, state = setup
    records = [{**row, "limit_evidence_known": True} for row in state["history"].to_dicts()]
    records.append({**history_row("301314.SZ", PREVIOUS, price=56.14, limit=None),
                    "limit_evidence_known": False})
    state["history"] = pl.DataFrame(records)
    result = service.evaluate(quotes(), DAY, now=NOW, live=True)
    assert result["groups"]["all"]["sample_count"] == 2
    assert result["history_excluded_count"] == 1
    assert result["status"] == "partial"
    assert "缺少首板识别所需的历史或涨停依据" in result["message"]
    assert "301314.SZ" not in {row["symbol"] for row in result["rows"]}


def test_known_no_limit_session_is_not_a_missing_history_warning(setup):
    service, state = setup
    records = [{**row, "limit_evidence_known": True} for row in state["history"].to_dicts()]
    records += [{**history_row("688001.SH", day, price=12, limit=None), "limit_evidence_known": True}
                for day in (BEFORE, PREVIOUS)]
    state["history"] = pl.DataFrame(records)
    result = service.evaluate(quotes(), DAY, now=NOW, live=True)
    assert result["groups"]["all"]["sample_count"] == 2
    assert result["history_excluded_count"] == 0
    assert result["status"] == "ready"


def test_first_limited_session_can_follow_known_no_limit_session(setup):
    service, state = setup
    records = [{**row, "limit_evidence_known": True} for row in state["history"].to_dicts()]
    records += [
        {**history_row("688001.SH", BEFORE, price=10, limit=None), "limit_evidence_known": True},
        {**history_row("688001.SH", PREVIOUS, price=12, limit=11.995), "limit_evidence_known": True},
    ]
    state["history"] = pl.DataFrame(records)
    result = service.evaluate(quotes(), DAY, now=NOW, live=True)
    assert result["groups"]["all"]["sample_count"] == 3
    assert result["history_excluded_count"] == 0
    assert "688001.SH" in {row["symbol"] for row in result["rows"]}


def test_missing_index_day_and_future_quotes_fail_closed(setup, monkeypatch):
    service, _ = setup
    from app.services import data_integrity

    monkeypatch.setattr(data_integrity, "_CAL", (None, [WARMUP, BEFORE, PREVIOUS, date(2026, 10, 24), DAY]))
    result = service.evaluate(quotes(), DAY, now=NOW, live=True)
    assert result["status"] == "unavailable"
    assert "交易日历" in result["message"]
    future = service.evaluate(quotes(), DAY + timedelta(days=1), now=NOW, live=True)
    assert future["status"] == "unavailable"


def test_feedback_is_independent_of_disabled_strategy_and_failed_candidate_preparation(setup, monkeypatch):
    from app.services import first_board as fb

    feedback, _ = setup
    service = fb.FirstBoardService(feedback.repo, history_loader=lambda *_: pl.DataFrame())
    service._feedback = feedback
    monkeypatch.setattr(fb, "_now", lambda: NOW)
    monkeypatch.setattr(service, "_prepare", lambda *_: None)
    service.process_snapshot(quotes(), DAY, now=NOW)
    assert service.snapshot()["status"] == "disabled"
    assert service.snapshot()["yesterday_feedback"]["groups"]["all"]["sample_count"] == 2
    copy = service.snapshot()
    copy["yesterday_feedback"]["rows"].clear()
    assert len(service.snapshot()["yesterday_feedback"]["rows"]) == 2

    def fail(*_):
        raise ValueError("candidate failure")

    monkeypatch.setattr(service, "_prepare", fail)
    with pytest.raises(ValueError, match="candidate failure"):
        service.process_snapshot(quotes(), DAY, now=NOW)
    assert service.snapshot()["yesterday_feedback"]["groups"]["all"]["sample_count"] == 2


def test_feedback_failure_does_not_break_existing_snapshot(setup, monkeypatch):
    from app.services import first_board as fb

    feedback, _ = setup
    service = fb.FirstBoardService(feedback.repo, history_loader=lambda *_: pl.DataFrame())
    monkeypatch.setattr(fb, "_now", lambda: NOW)
    monkeypatch.setattr(service, "_prepare", lambda *_: None)

    def fail(*_args, **_kwargs):
        raise RuntimeError("broken optional context")

    monkeypatch.setattr(service._feedback, "evaluate", fail)
    service.process_snapshot(quotes(), DAY, now=NOW)
    assert service.snapshot()["status"] == "disabled"
    assert service.snapshot()["yesterday_feedback"]["status"] == "unavailable"


def test_transient_preparation_failure_keeps_same_day_result_but_not_other_day(setup, monkeypatch):
    from app.services import first_board as fb

    feedback, state = setup
    service = fb.FirstBoardService(feedback.repo, history_loader=lambda *_: pl.DataFrame())
    service._feedback = feedback
    monkeypatch.setattr(fb, "_now", lambda: NOW)
    monkeypatch.setattr(service, "_prepare", lambda *_: None)
    service.process_snapshot(quotes(), DAY, now=NOW)
    state["history"] = None
    feedback.invalidate()
    service.process_snapshot(quotes(), DAY, now=NOW + timedelta(seconds=1))
    result = service.snapshot()["yesterday_feedback"]
    assert result["status"] == "stale"
    assert result["groups"]["all"]["sample_count"] == 2
    assert result["observed_at"] == NOW.isoformat()
    service.process_snapshot(quotes(), DAY + timedelta(days=1), now=NOW + timedelta(days=1))
    result = service.snapshot()["yesterday_feedback"]
    assert result["status"] == "unavailable"
    assert result["groups"]["all"]["sample_count"] == 0


def test_strategy_config_changes_keep_independent_feedback(setup, monkeypatch):
    from app.services import first_board as fb

    feedback, _ = setup
    service = fb.FirstBoardService(feedback.repo, history_loader=lambda *_: pl.DataFrame())
    service._feedback = feedback
    monkeypatch.setattr(fb, "_now", lambda: NOW)
    monkeypatch.setattr(service, "_prepare", lambda *_: None)
    service.process_snapshot(quotes(), DAY, now=NOW)
    before = service.snapshot()["yesterday_feedback"]
    config = service.get_config()
    service.save_config({**config, "rules": {**config["rules"], "min_change_pct": .1}})
    assert service.snapshot()["yesterday_feedback"] == before
    external = fb.FirstBoardService(feedback.repo, history_loader=lambda *_: pl.DataFrame())
    external.save_config({**external.get_config(), "notify": False})
    service.get_config()  # reload an externally saved strategy revision
    assert service.snapshot()["yesterday_feedback"] == before


def test_api_returns_feedback_and_no_history_reads_on_get(setup, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.first_board import router
    from app.services import first_board as fb

    feedback, state = setup
    service = fb.FirstBoardService(feedback.repo, history_loader=lambda *_: pl.DataFrame())
    service._feedback = feedback
    monkeypatch.setattr(fb, "_now", lambda: NOW)
    monkeypatch.setattr(service, "_prepare", lambda *_: None)
    service.process_snapshot(quotes(), DAY, now=NOW)
    app = FastAPI()
    app.include_router(router)
    app.state.first_board_service = service
    client = TestClient(app)
    for _ in range(2):
        response = client.get("/api/first-board/snapshot")
        assert response.status_code == 200
        assert response.json()["yesterday_feedback"]["groups"]["all"]["mean_change_pct"] == pytest.approx(.025)
    assert state["reads"] == 1
