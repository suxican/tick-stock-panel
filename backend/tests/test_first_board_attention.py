"""First-board attention evidence must be causal, source-specific and tri-state."""
from datetime import datetime, timedelta
from unittest.mock import Mock

import polars as pl
import pytest

from app.market_time import CN_TZ
from app.services import first_board_attention as mod
from app.services.first_board_attention import AttentionEvaluator


def stamp(hour=10, minute=0, day=21):
    return datetime(2026, 10, day, hour, minute, tzinfo=CN_TZ)


def quotes():
    return pl.DataFrame([
        {"symbol": f"{i:06d}.SZ", "name": f"股票{i}", "raw_close": 10.8 if i else 11.0,
         "limit_up_price": 10.995, "change_pct": .08 if i < 5 else -.01}
        for i in range(10)
    ])


def groups():
    return [
        {"id": "hot", "name": "科技", "kind": "concept", "source_id": "one", "field": "concept",
         "members": [f"{i:06d}.SZ" for i in range(5)]},
        {"id": "cold", "name": "材料", "kind": "concept", "source_id": "one", "field": "concept",
         "members": [f"{i:06d}.SZ" for i in range(5, 10)]},
    ]


def minutes(now=None, symbol="000001.SZ"):
    now = now or stamp()
    start = now.replace(hour=9, minute=30, tzinfo=None)
    cutoff = now.replace(tzinfo=None)
    rows = []
    while start < cutoff:
        if start.hour < 11 or (start.hour == 11 and start.minute < 30) or start.hour >= 13:
            price = 10 + len(rows) * .002
            rows.append({"symbol": symbol, "datetime": start, "open": price,
                         "high": price, "low": price, "close": price,
                         "volume": 100.0, "amount": price * 100 * 100})
        start += timedelta(minutes=1)
    return pl.DataFrame(rows)


def evaluate(engine=None, *, now=None, frame=None, members=None, bars=None, **kwargs):
    return (engine or AttentionEvaluator()).evaluate(
        quotes() if frame is None else frame, groups() if members is None else members,
        ["000001.SZ"], minutes(now) if bars is None else bars,
        now=now or stamp(), membership_version=kwargs.pop("membership_version", "v1"),
        minute_basis_raw=kwargs.pop("minute_basis_raw", True), **kwargs,
    )["000001.SZ"]


def observed_engine():
    engine = AttentionEvaluator()
    evaluate(engine, now=stamp(9, 40))
    return engine


def test_strength_uses_fraction_breadth_full_group_and_market_comparison():
    result = evaluate()
    group = result["groups"][0]
    assert result["strong_sector"] is True
    assert group["mean_change_pct"] == pytest.approx(.08)
    assert group["market_mean_change_pct"] == pytest.approx(.035)
    assert "相对同来源分类覆盖股票" in group["reason"]
    assert group["coverage"] == 1
    assert group["rank"] == 1 and group["total"] == 2
    assert group["member_count"] == 5 and group["valid_count"] == 5
    assert result["intraday_rising"] is True
    assert result["leader_follow"] is False  # first observation is after the rise window
    assert result["group"] == "S1F0"


def test_follow_requires_leader_observed_before_candidate_window_and_excludes_self():
    result = evaluate(observed_engine())
    assert result["leader_follow"] is True
    assert result["priority"] == 2 and result["group"] == "S1F1"
    assert result["groups"][0]["leader"]["symbol"] == "000000.SZ"
    frame = quotes().with_columns(pl.when(pl.col("symbol") == "000001.SZ").then(11.)
                                 .otherwise(pl.col("raw_close")).alias("raw_close"))
    assert evaluate(observed_engine(), frame=frame)["leader_follow"] is False


def test_limit_threshold_does_not_apply_exchange_tolerance_twice():
    below = quotes().with_columns(pl.lit(10.994).alias("raw_close"))
    engine = AttentionEvaluator()
    evaluate(engine, frame=below, now=stamp(9, 40))
    assert evaluate(engine, frame=below)["groups"][0]["leader"] is None
    at_limit = below.with_columns(pl.when(pl.col("symbol") == "000000.SZ").then(10.995)
                                 .otherwise(pl.col("raw_close")).alias("raw_close"))
    evaluate(engine, frame=at_limit, now=stamp(9, 50))
    assert evaluate(engine, frame=at_limit)["leader_follow"] is True


def test_opened_leader_is_evidence_but_no_longer_follow():
    engine = observed_engine()
    frame = quotes().with_columns(pl.lit(10.8).alias("raw_close"))
    result = evaluate(engine, frame=frame)
    assert result["leader_follow"] is False
    assert result["groups"][0]["leader"]["at_limit"] is False


@pytest.mark.parametrize("reset", ["day", "version", "backwards"])
def test_observations_reset_on_day_version_or_time_reversal(reset):
    engine = observed_engine()
    args = {"membership_version": "v2"} if reset == "version" else {}
    now = stamp(day=22) if reset == "day" else stamp(9, 38) if reset == "backwards" else stamp()
    result = evaluate(engine, now=now, **args)
    assert result["leader_follow"] is False
    assert result["groups"][0]["leader"]["observed_limit_at"] == now.isoformat()


def test_source_names_do_not_merge_and_ties_share_rank():
    members = [*groups(), {**groups()[0], "id": "other", "source_id": "two"}]
    result = evaluate(members=members)
    first, second = result["groups"]
    assert first["id"] == "hot" and second["id"] == "other"
    assert first["strong"] is True and first["total"] == 2
    assert second["strong"] is False and second["total"] == 1
    tied = [*groups(), {**groups()[0], "id": "tie", "name": "并列"}]
    evidence = evaluate(members=tied)["groups"]
    assert all(group["rank"] == 1 and group["strong"] for group in evidence)


def test_missing_and_duplicate_quotes_are_unknown_not_negative():
    frame = pl.concat([quotes().head(3), quotes().head(1)])
    result = evaluate(frame=frame, bars=pl.DataFrame(), minute_reason="分钟能力不可用")
    assert result["strong_sector"] is None
    assert result["leader_follow"] is None
    assert result["intraday_rising"] is None
    assert result["group"] is None and result["priority"] is None
    assert result["groups"][0]["valid_count"] == 2
    assert result["status"] == "unavailable"
    assert "分钟能力不可用" in result["reasons"]


def test_no_membership_is_unknown_and_candidate_prices_above_limit_are_rejected():
    assert evaluate(members=[])["strong_sector"] is None
    invalid = quotes().with_columns(pl.lit(1000.).alias("raw_close"))
    assert evaluate(observed_engine(), frame=invalid)["leader_follow"] is None


def test_future_and_current_unfinished_bars_cannot_change_evidence():
    base = minutes()
    future = base.tail(1).with_columns(pl.lit(stamp().replace(tzinfo=None)).alias("datetime"),
                                     pl.lit(1.).alias("close"))
    before = evaluate(observed_engine(), bars=base)
    after = evaluate(observed_engine(), bars=pl.concat([base, future]))
    # Polars' parallel market reduction can differ at the final binary digit;
    # keep all causal minute evidence and classification comparisons exact.
    for left, right in zip(before["groups"], after["groups"], strict=True):
        assert left.pop("market_mean_change_pct") == pytest.approx(
            right.pop("market_mean_change_pct"), rel=0, abs=1e-15,
        )
    assert before == after
    assert before["intraday"]["as_of"] == stamp(9, 59).isoformat()


def test_five_minute_return_and_vwap_use_volume_in_lots_amount_in_yuan():
    result = evaluate()
    expected = (10 + 29 * .002) / (10 + 24 * .002) - 1
    assert result["intraday"]["return_5m"] == pytest.approx(expected)
    assert result["intraday"]["above_vwap"] is True
    wrong_amount = minutes().with_columns((pl.col("amount") * 100).alias("amount"))
    # Invalid amount/volume price must not create a trusted feature.
    assert evaluate(bars=wrong_amount)["intraday_rising"] is None


@pytest.mark.parametrize("problem", ["missing", "duplicate", "basis", "stale", "nan"])
def test_minute_gaps_duplicates_unknown_basis_staleness_and_nan_fail_closed(problem):
    frame = minutes()
    kwargs = {}
    if problem == "missing":
        frame = frame.filter(pl.col("datetime").dt.minute() != 45)
    elif problem == "duplicate":
        frame = pl.concat([frame, frame.tail(1)])
    elif problem == "basis":
        kwargs["minute_basis_raw"] = False
    elif problem == "stale":
        frame = frame.head(20)
    else:
        frame = frame.with_columns(pl.lit(float("nan")).alias("amount"))
    result = evaluate(observed_engine(), bars=frame, **kwargs)
    assert result["intraday_rising"] is None
    assert result["leader_follow"] is None
    assert result["status"] == "partial"


def test_lunch_must_not_fill_window_from_morning_and_afternoon_requires_day_vwap():
    result = evaluate(observed_engine(), now=stamp(13, 3))
    assert result["intraday_rising"] is None
    now = stamp(13, 10)
    afternoon = minutes(now).filter(pl.col("datetime").dt.hour() >= 13)
    assert evaluate(observed_engine(), now=now, bars=afternoon)["intraday_rising"] is None
    assert evaluate(observed_engine(), now=now)["intraday_rising"] is True


def test_positive_return_with_large_drawdown_does_not_qualify():
    frame = minutes().with_row_index("_i").with_columns(
        pl.when(pl.col("_i") == 27).then(10.9).otherwise(pl.col("close")).alias("close")
    ).with_columns(pl.col("close").alias("open"), pl.col("close").alias("high"),
                   pl.col("close").alias("low"), (pl.col("close") * 10000).alias("amount"))
    result = evaluate(observed_engine(), bars=frame)
    assert result["intraday"]["max_drawdown"] > .01
    assert result["intraday_rising"] is False and result["leader_follow"] is False


def test_strong_and_follow_in_different_concepts_do_not_form_joint_group():
    members = [*groups(), {**groups()[1], "id": "follow", "members":
                           ["000001.SZ", "000000.SZ", "000005.SZ", "000006.SZ", "000007.SZ"]}]
    members[0] = {**members[0], "members": ["000001.SZ", "000002.SZ", "000003.SZ", "000004.SZ", "000008.SZ"]}
    engine = AttentionEvaluator()
    evaluate(engine, members=members, now=stamp(9, 40))
    result = evaluate(engine, members=members)
    assert result["strong_sector"] is True and result["leader_follow"] is True
    assert result["priority"] == 1
    assert result["group"] is None  # no mutually exclusive same-group research classification
    assert not any(group["strong"] and group["follow"] for group in result["groups"])


def test_unknown_condition_cannot_be_priority_zero():
    # Strong is known false, while a known earlier leader needs unavailable bars.
    members = [groups()[0]]
    engine = AttentionEvaluator()
    evaluate(engine, members=members, now=stamp(9, 40))
    result = evaluate(engine, members=members, bars=pl.DataFrame())
    assert result["strong_sector"] is False and result["leader_follow"] is None
    assert result["priority"] is None and result["group"] is None
    # Follow is known false, but the group's breadth cannot be determined.
    no_leader = quotes().head(3).with_columns(pl.lit(10.8).alias("raw_close"))
    result = evaluate(frame=no_leader.with_columns(
        pl.when(pl.col("symbol") == "000001.SZ").then(11.)
        .otherwise(pl.col("raw_close")).alias("raw_close")))
    assert result["strong_sector"] is None and result["leader_follow"] is False
    assert result["priority"] is None


def test_minute_features_reuse_same_batch_but_refresh_new_frame_cutoff_or_basis(monkeypatch):
    compute = Mock(wraps=mod._intraday)
    monkeypatch.setattr(mod, "_intraday", compute)
    engine, frame = AttentionEvaluator(), minutes()
    assert evaluate(engine, bars=frame)["intraday_rising"] is True
    assert evaluate(engine, bars=frame, now=stamp() + timedelta(seconds=30))["intraday_rising"] is True
    assert compute.call_count == 1
    falling = frame.with_columns(pl.col(column).reverse() for column in ("open", "high", "low", "close", "amount"))
    assert evaluate(engine, bars=falling)["intraday_rising"] is False
    assert compute.call_count == 2
    evaluate(engine, bars=frame, now=stamp(10, 1))
    assert compute.call_count == 3
    evaluate(engine, bars=frame, now=stamp(10, 1), minute_basis_raw=False)
    assert compute.call_count == 4


@pytest.mark.parametrize("label", ["start", "end"])
def test_complete_morning_and_afternoon_accept_consistent_provider_labels(label):
    # Match the two existing repository fixtures: intraday_signals._bars uses
    # 09:30..11:29/13:00; kline_minute_lunch_expected uses 09:31..11:30.
    now = stamp(13, 10)
    bars = minutes(now)
    if label == "end":
        bars = bars.with_columns(pl.col("datetime") + pl.duration(minutes=1))
    result = evaluate(observed_engine(), now=now, bars=bars)
    assert result["intraday_rising"] is True
    assert result["intraday"]["as_of"] == stamp(13, 9).isoformat()
    assert bars.filter(pl.col("datetime").dt.hour() < 13).height == 120


@pytest.mark.parametrize("problem", ["start_missing_last", "end_missing_last", "mixed", "extra"])
def test_morning_session_grid_never_fills_missing_or_mixed_labels(problem):
    now = stamp(13, 10)
    bars = minutes(now)
    if problem == "end_missing_last":
        bars = bars.with_columns(pl.col("datetime") + pl.duration(minutes=1)).filter(
            pl.col("datetime") != stamp(11, 30).replace(tzinfo=None))
    elif problem == "start_missing_last":
        bars = bars.filter(pl.col("datetime") != stamp(11, 29).replace(tzinfo=None))
    elif problem == "mixed":
        bars = bars.with_columns(pl.when(pl.col("datetime").dt.hour() >= 13)
                                 .then(pl.col("datetime") + pl.duration(minutes=1))
                                 .otherwise(pl.col("datetime")).alias("datetime"))
    else:
        extra = bars.filter(pl.col("datetime") == stamp(11, 29).replace(tzinfo=None)).with_columns(
            pl.lit(stamp(11, 30).replace(tzinfo=None)).alias("datetime"))
        bars = pl.concat([bars, extra])
    assert evaluate(observed_engine(), now=now, bars=bars)["intraday_rising"] is None
