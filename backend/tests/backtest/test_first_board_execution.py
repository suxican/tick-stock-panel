from datetime import date, datetime, time, timedelta
from types import SimpleNamespace

import polars as pl
import pytest

from app.backtest import first_board_execution as execution
from app.market_time import CN_TZ
from app.services.first_board_observations import FirstBoardObservationStore
from app.services.kline_sync import _write_minute_partition
from app.services.minute_adjust import mark_minute_basis_raw
from app.tickflow.repository import DataStore, KlineRepository

DAY = date(2026, 10, 19)
SYMBOL = "600000.SH"


def bars(day, price=10.0):
    start = datetime.combine(day, datetime.min.time()).replace(hour=9, minute=30)
    times = [start + timedelta(minutes=i) for i in range(121)]
    times += [start.replace(hour=13, minute=1) + timedelta(minutes=i) for i in range(120)]
    return pl.DataFrame({"symbol": [SYMBOL] * 241, "datetime": times,
                         **{key: [price] * 241 for key in ("open", "high", "low", "close")},
                         "volume": [10000.] * 241, "amount": [price * 1_000_000] * 241,
                         "minute_price_basis": ["raw"] * 241, "minute_time_grid": ["auction_end"] * 241})


def sample(*, day=DAY, minute=0, second=17, flag=True, group="S1F1"):
    stamp = datetime.combine(day, datetime.min.time()).replace(hour=10, minute=minute, second=second, tzinfo=CN_TZ)
    candidate = {"symbol": SYMBOL, "pattern": "platform", "can_buy": flag, "quote_time": stamp.isoformat(),
                 "state": "approaching", "evidence": {"limit_up_price": 11., "raw_prev_close": 10.}}
    return {"schema_version": 1, "rule_revision": 1, "observed_at": stamp.isoformat(),
            "trade_date": str(day), "anchor": stamp.replace(second=0).isoformat(),
            "metadata": {"config": {"revision": 1, "exit_time": "10:30", "exit_loss_pct": .03}},
            "rows": [{"symbol": SYMBOL, "name": "样本", "can_buy": flag,
                      "attention_context": {"group": group}, "candidates": [candidate]}]}


class Store:
    def __init__(self, rows):
        self.rows = rows

    def iter_samples(self, start, end, revision):
        return (row for row in self.rows if start <= date.fromisoformat(row["observed_at"][:10]) <= end)


class Repo:
    def __init__(self, tmp_path, frames):
        self.store = SimpleNamespace(data_dir=tmp_path)
        self.frames = frames
        self.calls = []
        self.daily = pl.DataFrame([{"symbol": SYMBOL, "date": day, "close": 10., "raw_close": 10.,
                                    "raw_prev_close": 10., "limit_up_price": 10.995,
                                    "limit_down_price": 9.005} for day in frames])

    def get_minute_by_dates(self, symbols, dates, asset_type="stock", *, raw=False):
        assert raw and asset_type == "stock" and len(dates) == 1
        self.calls.append(dates[0])
        return self.frames.get(dates[0], pl.DataFrame())

    def get_daily_batch(self, symbols, start, end, columns=None):
        return self.daily.filter(pl.col("date").is_between(start, end))

    def get_execution_daily_batch(self, symbols, start, end, **kwargs):
        return self.get_daily_batch(symbols, start, end)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    days = [DAY + timedelta(days=i) for i in range(5)]
    monkeypatch.setattr(execution, "load_market_calendar", lambda path, start, end: [d for d in days if start <= d <= end])
    return Repo(tmp_path, {d: bars(d, 10. if d == DAY else 10.2) for d in days}), days


def run(repo, samples, through=None, **options):
    return execution.run_execution_validation(repo, Store(samples), start=DAY, end=DAY, revision=1,
                                               options=execution.ExecutionOptions(**options),
                                               through=through or DAY + timedelta(days=4))


def test_recorded_signal_delays_entry_and_t1_exit_and_charges_real_cash_fees(setup):
    repo, days = setup
    result = run(repo, [sample()])
    trade = result["trades"][0]
    assert trade["status"] == "closed"
    assert trade["entry_bar_at"].endswith("10:02:00+08:00")
    assert trade["entry_at"].endswith("10:03:00+08:00")
    assert trade["exit_at"].startswith(str(days[1]))
    assert trade["exit_signal_at"].endswith("10:30:00+08:00")
    assert trade["exit_bar_at"].endswith("10:32:00+08:00")
    assert trade["exit_at"].endswith("10:33:00+08:00")
    assert trade["qty"] == 900
    assert trade["buy_fee"] == 5
    assert trade["net_return"] < trade["gross_return"]
    assert trade["net_return"] == pytest.approx(trade["net_pnl"] / trade["entry_cost"])
    assert result["by_group"]["S1F1"]["closed"] == 1
    assert len(repo.calls) == len(set(repo.calls))


def test_first_stock_day_signal_merges_patterns_and_keeps_negative_unknown_denominators(setup):
    repo, _ = setup
    first = sample()
    first["rows"][0]["candidates"].append({**first["rows"][0]["candidates"][0], "pattern": "trend"})
    later = sample(minute=5)
    negative = sample(flag=False)
    negative["rows"][0]["symbol"] = "600001.SH"
    unknown = sample(flag=None)
    unknown["rows"][0]["symbol"] = "600002.SH"
    result = run(repo, [first, later, negative, unknown])
    assert result["coverage"]["stock_days"] == 3
    assert result["coverage"]["eligible_stock_days"] == 1
    assert result["coverage"]["negative_stock_days"] == 1
    assert result["coverage"]["unknown_stock_days"] == 1
    assert result["trades"][0]["patterns"] == ["platform", "trend"]


@pytest.mark.parametrize("issue", ["missing", "unknown", "gap", "duplicate"])
def test_unknown_or_incomplete_entry_data_is_kept_as_unverifiable(setup, issue):
    repo, _ = setup
    frame = repo.frames[DAY]
    if issue == "missing":
        frame = pl.DataFrame()
    elif issue == "unknown":
        frame = frame.with_columns(pl.lit(None, dtype=pl.String).alias("minute_price_basis"))
    elif issue == "gap":
        frame = frame.filter(pl.col("datetime").dt.minute() != 1)
    else:
        frame = pl.concat([frame, frame.head(1)])
    repo.frames[DAY] = frame
    result = run(repo, [sample()])
    assert result["summary"]["unverifiable"] == 1
    assert result["trades"][0]["net_return"] is None
    assert result["trades"][0]["reasons"]


@pytest.mark.parametrize("price,volume", [(11., 10000.), (10., 0.), (10., 1.)])
def test_locked_or_insufficient_volume_entry_is_not_a_filled_trade(setup, price, volume):
    repo, _ = setup
    repo.frames[DAY] = bars(DAY, price).with_columns(pl.lit(volume).alias("volume"),
                                                    pl.lit(price * volume * 100).alias("amount"))
    result = run(repo, [sample()])
    assert result["summary"]["no_fill"] == 1
    assert result["trades"][0]["entry_at"] is None


def test_unknown_calendar_and_corporate_action_never_create_returns(setup, monkeypatch):
    repo, days = setup
    repo.daily = repo.daily.with_columns(pl.when(pl.col("date") == days[1]).then(5.).otherwise(pl.col("raw_close")).alias("raw_close"))
    assert run(repo, [sample()])["summary"]["unverifiable"] == 1
    monkeypatch.setattr(execution, "load_market_calendar", lambda *a, **kw: (_ for _ in ()).throw(ValueError("missing")))
    result = run(repo, [sample()])
    assert result["summary"]["unverifiable"] == 1
    assert "calendar_unavailable" in result["trades"][0]["reasons"]


def test_missing_next_day_does_not_skip_to_later_existing_data(setup):
    repo, days = setup
    del repo.frames[days[1]]
    result = run(repo, [sample()])
    assert result["trades"][0]["status"] == "unverifiable"
    assert result["trades"][0]["exit_at"] is None


@pytest.mark.parametrize("field", ["amount", "commission_pct", "stamp_tax_pct", "slippage_bps", "participation_rate"])
def test_options_reject_boolean_numbers(field):
    with pytest.raises(ValueError):
        execution.ExecutionOptions(**{field: True})


@pytest.mark.parametrize("field,value", [("exit_loss_pct", ".03"), ("exit_loss_pct", True), ("revision", True)])
def test_invalid_saved_exit_config_is_unverifiable_not_a_runtime_exception(setup, field, value):
    repo, _ = setup
    data = sample()
    data["metadata"]["config"][field] = value
    assert run(repo, [data])["trades"][0]["status"] == "unverifiable"


def test_entry_window_must_allow_a_completed_endpoint():
    with pytest.raises(ValueError):
        execution.ExecutionOptions(entry_window_minutes=2)


def test_bad_optional_context_is_unknown_and_bad_evidence_is_unverifiable(setup):
    repo, _ = setup
    data = sample()
    data["rows"][0]["attention_context"] = "bad"
    result = run(repo, [data])
    assert result["trades"][0]["group"] == "unknown"
    data["rows"][0]["candidates"][0]["evidence"] = "bad"
    assert run(repo, [data])["trades"][0]["status"] == "unverifiable"


def test_later_price_edits_do_not_change_earlier_entry(setup):
    repo, _ = setup
    before = run(repo, [sample()])["trades"][0]
    late = pl.col("datetime").dt.hour() >= 11
    repo.frames[DAY] = repo.frames[DAY].with_columns(
        [pl.when(late).then(10.7).otherwise(pl.col(c)).alias(c) for c in ("open", "high", "low", "close")]
        + [pl.when(late).then(10.7 * 1_000_000).otherwise(pl.col("amount")).alias("amount")])
    after = run(repo, [sample()])["trades"][0]
    assert {k: v for k, v in before.items() if k.startswith("entry") or k in {"qty", "buy_fee"}} == {
        k: v for k, v in after.items() if k.startswith("entry") or k in {"qty", "buy_fee"}}


def test_missing_entry_window_tail_does_not_masquerade_as_no_fill(setup):
    repo, _ = setup
    frame = bars(DAY, 11.)
    cutoff = datetime.combine(DAY, time(10, 2))
    repo.frames[DAY] = frame.filter(pl.col("datetime") <= cutoff)
    assert run(repo, [sample()])["trades"][0]["status"] == "unverifiable"


def test_locked_exit_remains_open_and_is_not_force_closed(setup):
    repo, days = setup
    repo.frames[days[1]] = bars(days[1], 9.)
    result = run(repo, [sample()], max_exit_sessions=1)
    trade = result["trades"][0]
    assert trade["status"] == "open_position" and trade["net_return"] is None
    assert "exit_window_exhausted" in trade["reasons"]


def test_stop_loss_only_triggers_t1_and_delays_execution_from_available_close(setup):
    repo, days = setup
    repo.frames[DAY] = repo.frames[DAY].with_columns(
        [pl.when(pl.col("datetime").dt.hour() >= 11).then(9.2).otherwise(pl.col(c)).alias(c)
         for c in ("open", "high", "low", "close")]
        + [pl.when(pl.col("datetime").dt.hour() >= 11).then(9_200_000.).otherwise(pl.col("amount")).alias("amount")])
    repo.frames[days[1]] = bars(days[1], 9.6)
    trade = run(repo, [sample()])["trades"][0]
    assert trade["exit_signal_at"] == f"{days[1]}T09:32:00+08:00"
    assert trade["exit_at"] == f"{days[1]}T09:35:00+08:00"
    assert trade["net_return"] < 0


def test_t1_uses_supplied_exchange_sessions_not_calendar_days(tmp_path, monkeypatch):
    monday = DAY + timedelta(days=7)
    friday = DAY + timedelta(days=4)
    sessions = [friday, monday]
    monkeypatch.setattr(execution, "load_market_calendar", lambda path, start, end: sessions)
    repo = Repo(tmp_path, {d: bars(d) for d in sessions})
    result = execution.run_execution_validation(repo, Store([sample(day=friday)]), start=friday, end=friday,
                                                revision=1, options=execution.ExecutionOptions(), through=monday)
    assert result["trades"][0]["exit_at"].startswith(str(monday))


def test_trade_details_limit_never_changes_summary_or_fingerprint(setup, monkeypatch):
    repo, _ = setup
    before = run(repo, [sample()])
    monkeypatch.setattr(execution, "MAX_DETAILS", 0)
    after = run(repo, [sample()])
    assert after["trades"] == [] and after["trades_truncated"]
    assert before["summary"] == after["summary"]
    assert before["fingerprint"] == after["fingerprint"]


def test_lunch_expiry_never_buys_at_afternoon_price(setup):
    repo, _ = setup
    data = sample()
    data["observed_at"] = f"{DAY}T11:28:17+08:00"
    trade = run(repo, [data])["trades"][0]
    assert trade["status"] == "no_fill" and trade["entry_at"] is None


def test_unknown_basis_and_current_provider_cannot_be_replaced_by_raw_marker(setup):
    repo, _ = setup
    mark_minute_basis_raw(repo.store.data_dir)
    repo.frames[DAY] = repo.frames[DAY].with_columns(pl.lit("forward").alias("minute_price_basis"))
    assert run(repo, [sample()])["summary"]["unverifiable"] == 1


def test_observation_reader_is_bounded_ordered_and_read_only(tmp_path):
    store = FirstBoardObservationStore(tmp_path)
    for minute in (5, 0):
        data = sample(minute=minute)
        store.record(data["rows"][0]["candidates"], now=datetime.fromisoformat(data["observed_at"]),
                     rule_revision=1, membership_version=None, metadata=data["metadata"])
    before = {p: p.read_bytes() for p in tmp_path.rglob("*.gz")}
    records = list(store.iter_samples(DAY, DAY, 1))
    assert len(records) == 2 and records[0]["anchor"] < records[1]["anchor"]
    assert {p: p.read_bytes() for p in before} == before
    with pytest.raises(ValueError):
        list(store.iter_samples(DAY, DAY + timedelta(days=92), 1))


def test_repository_raw_option_does_not_project_or_certify_unknown_rows(tmp_path):
    _write_minute_partition(bars(DAY), tmp_path / "kline_minute")
    root = tmp_path / "adj_factor"
    root.mkdir()
    pl.DataFrame({"symbol": [SYMBOL], "trade_date": [DAY + timedelta(days=1)], "ex_factor": [2.]}).write_parquet(root / "all.parquet")
    mark_minute_basis_raw(tmp_path)
    repo = KlineRepository(DataStore(tmp_path))
    assert repo.get_minute_by_dates([SYMBOL], [DAY])["close"][0] == 5.
    assert repo.get_minute_by_dates([SYMBOL], [DAY], raw=True)["close"][0] == 10.
    frame = bars(DAY).with_columns(pl.lit("unknown").alias("minute_price_basis"))
    _write_minute_partition(frame, tmp_path / "kline_minute")
    assert repo.get_minute_by_dates([SYMBOL], [DAY], raw=True)["minute_price_basis"].unique().to_list() == ["unknown"]


def test_execution_daily_reads_real_storage_with_identical_cold_and_warm_facts(tmp_path):
    from polars.testing import assert_frame_equal

    repo = KlineRepository(DataStore(tmp_path))
    days = [date(2026, 6, 1), date(2026, 6, 2), date(2026, 6, 3)]
    daily = pl.DataFrame({"symbol": [SYMBOL] * 3, "date": days,
                          "close": [10.] * 3, "raw_close": [10.] * 3})
    daily.write_parquet(tmp_path / "kline_daily_enriched" / "part.parquet")
    repo._instruments_cache = pl.DataFrame({"symbol": [SYMBOL], "name": ["当前ST名称"],
                                           "listing_date": [date(1990, 1, 1)]})
    history = pl.DataFrame({"symbol": [SYMBOL] * 2, "date": days[1:],
                            "name": ["历史普通股", "历史ST股"]})
    before = {p: p.read_bytes() for p in tmp_path.rglob("*.parquet")}
    cold = repo.get_execution_daily_batch([SYMBOL], days[1], days[-1], sessions=days,
                                          name_history=history)
    assert cold["raw_prev_close"].to_list() == [10., 10.]
    assert cold["limit_up_price"].to_list() == pytest.approx([10.995, 10.495])
    repo._enriched_history_cache = daily.with_columns(pl.lit(999.).alias("limit_up_price"))
    repo._enriched_history_start = days[0]
    warm = repo.get_execution_daily_batch([SYMBOL], days[1], days[-1], sessions=days,
                                          name_history=history)
    assert_frame_equal(cold, warm)
    assert {p: p.read_bytes() for p in before} == before


@pytest.mark.parametrize("issue", ["missing_name", "conflict", "missing_prior", "missing_listing"])
def test_execution_daily_never_guesses_missing_historical_limit_facts(tmp_path, issue):
    repo = KlineRepository(DataStore(tmp_path))
    days = [date(2026, 6, 1), date(2026, 6, 2)]
    daily = pl.DataFrame({"symbol": [SYMBOL] * 2, "date": days,
                          "close": [10.] * 2, "raw_close": [10.] * 2})
    if issue == "missing_prior":
        daily = daily.tail(1)
    daily.write_parquet(tmp_path / "kline_daily_enriched" / "part.parquet")
    repo._instruments_cache = pl.DataFrame({"symbol": [SYMBOL], "name": ["当前普通股"],
                                           "listing_date": [date(1990, 1, 1)]})
    if issue == "missing_listing":
        repo._instruments_cache = repo._instruments_cache.drop("listing_date")
    names = [] if issue == "missing_name" else ["普通股", "ST股"] if issue == "conflict" else ["普通股"]
    history = pl.DataFrame({"symbol": [SYMBOL] * len(names), "date": [days[-1]] * len(names), "name": names},
                           schema={"symbol": pl.String, "date": pl.Date, "name": pl.String})
    result = repo.get_execution_daily_batch([SYMBOL], days[-1], days[-1], sessions=days,
                                            name_history=history)
    assert result["limit_up_price"].to_list() == [None]
    assert result["limit_down_price"].to_list() == [None]
