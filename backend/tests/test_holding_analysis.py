"""Read-only holding research keeps reminder, inventory and price bases separate."""
from datetime import date, datetime, time, timedelta
from types import SimpleNamespace

import polars as pl
import pytest

from app.market_time import CN_TZ
from app.services.holding_analysis import analyze_holdings, read_inputs
from app.services.huichun_calendar import BUNDLED_CALENDAR_PATH, load_market_calendar
from app.strategy import lots

NOW = datetime(2026, 11, 5, 10, 30, tzinfo=CN_TZ)
SESSIONS = load_market_calendar(
    BUNDLED_CALENDAR_PATH, start=date(2026, 6, 1), end=date(2026, 11, 5),
)
HISTORY_DAYS = [d for d in SESSIONS if d < NOW.date()][-65:]
SYMBOL = "600000.SH"


class Repo:
    def __init__(self, data_dir, history=None):
        self.store = SimpleNamespace(data_dir=data_dir)
        self.calls = []
        self.history = history if history is not None else history_frame()

    def get_instruments(self):
        return pl.DataFrame({"symbol": [SYMBOL, "600001.SH", "600002.SH", "510300.SH"],
                             "name": ["样本甲", "样本乙", "样本丙", "样本基金"],
                             "asset_type": ["stock", "stock", "stock", "etf"]})

    def resolve_asset_type(self, symbol):
        return "etf" if symbol == "510300.SH" else "stock"

    def get_daily_batch(self, symbols, start, end, columns=None):
        self.calls.append((list(symbols), start, end))
        return self.history.filter(pl.col("symbol").is_in(symbols)
                                   & pl.col("date").is_between(start, end))


def history_frame():
    rows = []
    for symbol in (SYMBOL, "600001.SH", "600002.SH"):
        for i, day in enumerate(HISTORY_DAYS):
            raw = 10 + i * .05
            # Adjusted prices deliberately differ from executable raw prices.
            rows.append({"symbol": symbol, "date": day, "open": raw / 2,
                         "close": raw / 2, "high": (raw + .2) / 2,
                         "low": (raw - .2) / 2, "raw_close": raw,
                         "raw_high": raw + .2, "raw_low": raw - .2,
                         "change_pct": .004, "quote_ts": None})
    return pl.DataFrame(rows)


def quote(price=14, *, stamp=NOW, change=14 / 13.2 - 1):
    return {"symbol": SYMBOL, "last_price": price, "prev_close": price / (1 + change),
            "change_pct": change, "timestamp": stamp.timestamp() * 1000, "source": "fixture"}


def market(records=None):
    records = {SYMBOL: quote()} if records is None else records
    return SimpleNamespace(get_market_records=lambda: records,
                           status=lambda: {"enabled": True, "paused": False,
                                           "realtime_allowed": True})


def sectors(groups=None):
    if groups is None:
        groups = [{"key": "industry:a", "name": "同名板块", "kind": "industry",
                   "source_id": "source-a", "field": "industry", "members": [SYMBOL]},
                  {"key": "industry:b", "name": "同名板块", "kind": "industry",
                   "source_id": "source-b", "field": "industry", "members": [SYMBOL]}]
    return SimpleNamespace(membership_snapshot=lambda: groups,
                           build_snapshots=lambda *a, **k: pytest.fail("must not mutate sector history"))


def save_lot(repo, identifier="lot_a", **fields):
    lots.save_one(repo.store.data_dir, {"id": identifier, "symbol": SYMBOL, "qty": 100,
                                      "cost_price": 10, "buy_date": "2026-11-02", **fields})


def analyze(repo, **kwargs):
    key = next(p["key"] for p in read_inputs(repo, None, "lots")["positions"]
               if p["symbol"] == SYMBOL)
    profiles = {key: {"invalidation": "跌破研究支撑后重新评估", "thesis": "趋势观察"}}
    return analyze_holdings(repo, kwargs.pop("quote_service", market()),
                            kwargs.pop("sector_service", sectors()), None, "lots",
                            profiles=kwargs.pop("profiles", profiles), now=NOW, **kwargs)


def test_inputs_merge_reference_cost_and_keep_dates_without_mixing_sources(tmp_path):
    repo = Repo(tmp_path)
    save_lot(repo, qty=100, cost_price=10)
    save_lot(repo, "lot_b", qty=300, cost_price=20, buy_date="2026-11-04")
    first = read_inputs(repo, None, "lots")
    row = first["positions"][0]
    assert row["qty"] == 400 and row["cost_price"] == 17.5
    assert row["opened_on"] == "2026-11-02" and row["latest_added_on"] == "2026-11-04"
    assert row["cost_method"] == "manual_reference"
    assert first["input_version"] == read_inputs(repo, None, "lots")["input_version"]
    save_lot(repo, "lot_b", qty=200)
    assert first["input_version"] != read_inputs(repo, None, "lots")["input_version"]


@pytest.mark.parametrize("fields", [{"qty": 0}, {"qty": 1.5}, {"cost_price": 0},
                                         {"cost_price": float("nan")}])
def test_bad_batch_invalidates_entire_symbol_not_only_bad_row(tmp_path, fields):
    repo = Repo(tmp_path)
    save_lot(repo)
    save_lot(repo, "lot_bad", **fields)
    row = read_inputs(repo, None, "lots")["positions"][0]
    assert row["qty"] is None and row["cost_price"] is None
    result = analyze(repo)["rows"][0]
    assert result["pnl_ratio"] is None and not result["plan"]["can_adopt"]
    assert result["warnings"]


def position_service(*, active=False):
    pos = {"symbol": SYMBOL, "name": "样本甲", "qty": 100, "cost_price": 10,
           "cost_method": "manual_reference", "snapshot_at": NOW.isoformat(),
           "version": 2, "sellable_qty": 100, "effective_sellable": 100,
           "reconciled": True, "trade_date": NOW.date().isoformat()}
    plans = [{"id": "plan_a", "symbol": SYMBOL, "state": "exposure", "version": 1,
              "unmatched_qty": 100, "exposure_qty": 100}] if active else []
    store = SimpleNamespace(list_accounts=lambda: [{"id": "a", "name": "账户甲"}],
                            get_account=lambda aid: {"id": aid, "version": 3, "reconciled": True,
                                                     "snapshot_at": NOW.isoformat()},
                            list_positions=lambda aid: [dict(pos)],
                            list_plans=lambda aid: plans)
    return SimpleNamespace(store=store), pos, plans


def test_account_requires_explicit_choice_and_snapshot_is_not_open_date(tmp_path):
    repo = Repo(tmp_path)
    service, _, plans = position_service(active=True)
    empty = read_inputs(repo, service, "position_t")
    assert empty["accounts"] == [{"id": "a", "name": "账户甲"}]
    assert empty["positions"] == []
    result = read_inputs(repo, service, "position_t", "a")
    assert result["positions"][0]["opened_on"] is None
    assert result["positions"][0]["active_t_plan"] is True
    plans[0]["exposure_qty"] = 200
    assert read_inputs(repo, service, "position_t", "a")["input_version"] != result["input_version"]
    with pytest.raises(ValueError):
        read_inputs(repo, service, "position_t", "other")


def test_analysis_uses_raw_cost_and_converted_technical_prices_in_one_batch(tmp_path):
    repo = Repo(tmp_path)
    save_lot(repo)
    result = analyze(repo)
    row = result["rows"][0]
    assert row["pnl_ratio"] == pytest.approx(.4)
    assert row["technicals"]["ma20"] == pytest.approx(sum(10 + i * .05 for i in range(45, 65)) / 20)
    assert row["technicals"]["support"] > 10
    assert row["trend"] == "up"
    assert row["age_state"] == "new" and row["age_days"] == 3
    assert row["pnl_state"] == "profit" and row["plan"]["priority"] == "protect"
    assert row["plan"]["can_adopt"] is True
    assert row["constraints"]["can_size"] is False
    assert result["as_of_date"] == "2026-11-04"
    assert len(repo.calls) <= 2
    assert {s["source_id"] for s in row["sectors"]} == {"source-a", "source-b"}


def test_incomplete_day_missing_quote_time_and_old_daily_do_not_create_triggers(tmp_path):
    repo = Repo(tmp_path)
    save_lot(repo)
    # Inject current incomplete candle; it must not enter indicator windows.
    current = repo.history.tail(1).with_columns(pl.lit(SYMBOL).alias("symbol"),
                                               pl.lit(NOW.date()).alias("date"),
                                               pl.lit(10000.).alias("raw_close"))
    repo.history = pl.concat([repo.history, current], how="vertical_relaxed")
    bad_quote = quote()
    bad_quote.pop("timestamp")
    row = analyze(repo, quote_service=market({SYMBOL: bad_quote}))["rows"][0]
    assert row["price"]["source"] == "daily" and row["price"]["date"] == "2026-11-04"
    assert row["price"]["value"] == pytest.approx(13.2)
    repo.history = repo.history.filter(pl.col("date") < HISTORY_DAYS[-1])
    row = analyze(repo, quote_service=market({}))["rows"][0]
    assert not row["plan"]["can_adopt"]
    assert all(s["price_trigger"] is None for s in row["plan"]["scenarios"])


def test_missing_adjustment_evidence_never_compares_adjusted_level_to_raw_price(tmp_path):
    repo = Repo(tmp_path, history_frame().drop("raw_close", "raw_high", "raw_low"))
    save_lot(repo)
    row = analyze(repo)["rows"][0]
    assert row["price"]["source"] == "quote" and row["pnl_ratio"] == pytest.approx(.4)
    assert row["technicals"]["support"] is None and not row["plan"]["can_adopt"]
    assert all(s["price_trigger"] is None for s in row["plan"]["scenarios"])


def test_sector_complete_membership_preserves_unknowns_and_does_not_take_hot_list(tmp_path):
    repo = Repo(tmp_path)
    save_lot(repo)
    groups = [{"key": f"concept:{i}", "name": f"题材{i}", "kind": "concept",
               "source_id": "source", "members": [SYMBOL, "600999.SH"]}
              for i in range(35)]
    row = analyze(repo, sector_service=sectors(groups))["rows"][0]
    assert len(row["sectors"]) == 35
    assert all(s["member_count"] == 2 and s["coverage_ratio"] == .5
               and not s["valid"] and s["change_ratio"] is None for s in row["sectors"])
    assert row["relative_strength"] == "unknown"


def test_missing_profile_and_t_conflict_are_explicit_adoption_blocks(tmp_path):
    repo = Repo(tmp_path)
    save_lot(repo, buy_date=None)
    row = analyze(repo, profiles={})["rows"][0]
    assert row["age_state"] == "unknown" and row["age_days"] is None
    assert not row["plan"]["can_adopt"]
    assert any("失效条件" in s for s in row["warnings"])
    service, _, _ = position_service(active=True)
    key = read_inputs(repo, service, "position_t", "a")["positions"][0]["key"]
    profiles = {key: {"opened_on": "2026-11-02", "invalidation": "逻辑失效"}}
    row = analyze_holdings(repo, market(), sectors(), service, "position_t", "a",
                           profiles=profiles, now=NOW)["rows"][0]
    assert not row["plan"]["can_adopt"] and row["constraints"]["active_t_plan"]


def test_account_changes_while_reading_market_invalidate_result(tmp_path):
    repo = Repo(tmp_path)
    service, pos, _ = position_service()
    key = read_inputs(repo, service, "position_t", "a")["positions"][0]["key"]

    def records():
        pos["version"] += 1
        return {SYMBOL: quote()}

    quotes = SimpleNamespace(get_market_records=records,
                             status=lambda: {"enabled": True, "realtime_allowed": True})
    result = analyze_holdings(repo, quotes, sectors(), service, "position_t", "a",
                              profiles={key: {"opened_on": "2026-11-02", "invalidation": "失效"}},
                              now=NOW)
    assert not result["rows"][0]["plan"]["can_adopt"]
    assert any("变化" in message for message in result["warnings"])


def test_deep_loss_is_risk_first_and_never_auto_averages_down(tmp_path):
    repo = Repo(tmp_path)
    save_lot(repo, cost_price=30, buy_date="2026-10-01")
    row = analyze(repo)["rows"][0]
    assert row["pnl_state"] == "deep_loss" and row["plan"]["priority"] == "risk"
    assert all("补仓" not in s["action"] for s in row["plan"]["scenarios"])


def test_bad_single_symbol_and_calendar_out_of_coverage_are_isolated(tmp_path):
    repo = Repo(tmp_path)
    save_lot(repo)
    save_lot(repo, "lot_b", symbol="510300.SH")
    result = analyze(repo)
    assert len(result["rows"]) == 2
    assert next(r for r in result["rows"] if r["symbol"] == SYMBOL)["plan"]["can_adopt"]
    assert not next(r for r in result["rows"] if r["symbol"] == "510300.SH")["plan"]["can_adopt"]
    future = datetime.combine(date(2027, 1, 4), time(10), CN_TZ)
    result = analyze_holdings(repo, market(), sectors(), None, "lots", now=future)
    assert result["as_of_date"] is None
    assert all(not r["plan"]["can_adopt"] for r in result["rows"])


def test_future_quote_and_history_gap_fail_closed(tmp_path):
    repo = Repo(tmp_path)
    save_lot(repo)
    repo.history = repo.history.filter(pl.col("date") != HISTORY_DAYS[-3])
    row = analyze(repo, quote_service=market({SYMBOL: quote(stamp=NOW + timedelta(minutes=1))}))["rows"][0]
    assert row["price"]["source"] == "daily"
    assert not row["plan"]["can_adopt"]


def test_old_holdings_count_real_sessions_without_using_snapshot_date(tmp_path):
    repo = Repo(tmp_path)
    opened = date(2025, 11, 3)
    save_lot(repo, buy_date=opened.isoformat())
    row = analyze(repo)["rows"][0]
    expected = load_market_calendar(BUNDLED_CALENDAR_PATH, start=opened, end=NOW.date())
    assert row["age_days"] == len(expected) - 1
    assert row["age_state"] == "overdue"


def test_quote_price_basis_change_cannot_keep_old_raw_triggers(tmp_path):
    repo = Repo(tmp_path)
    save_lot(repo)
    changed = quote()
    changed["prev_close"] = 6.6
    row = analyze(repo, quote_service=market({SYMBOL: changed}))["rows"][0]
    assert not row["plan"]["can_adopt"]
    assert all(s["price_trigger"] is None for s in row["plan"]["scenarios"])
    assert any("基准" in s for s in row["warnings"])


def test_monitor_bookkeeping_does_not_change_input_revision(tmp_path):
    repo = Repo(tmp_path)
    service, _, plans = position_service(active=True)
    before = read_inputs(repo, service, "position_t", "a")["input_version"]
    plans[0]["monitor"] = {"last_checked_at": NOW.isoformat(), "quote_price": 99}
    assert read_inputs(repo, service, "position_t", "a")["input_version"] == before


def test_empty_lots_read_does_not_create_runtime_folders(tmp_path):
    repo = Repo(tmp_path)
    result = read_inputs(repo, None, "lots")
    assert result["positions"] == []
    assert not (tmp_path / "user_data").exists()


def test_sector_fallback_uses_completed_day_not_stale_realtime(tmp_path):
    repo = Repo(tmp_path)
    save_lot(repo)
    stale = market({SYMBOL: quote(stamp=NOW - timedelta(hours=1), change=.5)})
    row = analyze(repo, quote_service=stale)["rows"][0]
    assert row["sectors"][0]["basis"] == "daily"
    assert row["sectors"][0]["change_ratio"] == pytest.approx(.004)
    assert row["relative_strength"] == "inline"


def test_calendar_near_coverage_end_still_recognizes_completed_sessions(tmp_path):
    repo = Repo(tmp_path)
    save_lot(repo)
    near_end = datetime(2026, 12, 30, 10, tzinfo=CN_TZ)
    result = analyze_holdings(repo, market({}), sectors(), None, "lots", now=near_end)
    assert result["as_of_date"] == "2026-12-29"


def test_lots_plan_blocks_other_account_t_exposure_without_adding_inventory(tmp_path):
    repo = Repo(tmp_path)
    save_lot(repo)
    service, _, plans = position_service(active=True)
    before = read_inputs(repo, service, "lots")
    row = before["positions"][0]
    assert row["qty"] == 100 and row["active_t_plan"]
    key = row["key"]
    result = analyze_holdings(repo, market(), sectors(), service, "lots",
                              profiles={key: {"invalidation": "失效"}}, now=NOW)
    assert not result["rows"][0]["plan"]["can_adopt"]
    plans[0]["unmatched_qty"] = 50
    assert read_inputs(repo, service, "lots")["input_version"] != before["input_version"]


def test_price_references_and_research_horizon_are_explicit(tmp_path):
    repo = Repo(tmp_path)
    save_lot(repo)
    row = analyze(repo)["rows"][0]
    assert row["price"]["reference_close"] == pytest.approx(13.2)
    assert row["price"]["reference_date"] == "2026-11-04"
    assert row["plan"]["expires_on"] == "2026-11-11"
    key = read_inputs(repo, None, "lots")["positions"][0]["key"]
    row = analyze(repo, profiles={key: {"horizon": "swing", "invalidation": "失效"}})["rows"][0]
    assert row["plan"]["expires_on"] == "2026-12-02"


@pytest.mark.parametrize("patch", [{"snapshot_at": (NOW - timedelta(hours=1)).isoformat()},
                                    {"reserved_qty": 100}, {"effective_sellable": None}])
def test_precision_sizing_requires_fresh_unreserved_inventory(tmp_path, patch):
    repo = Repo(tmp_path)
    service, pos, _ = position_service()
    key = read_inputs(repo, service, "position_t", "a")["positions"][0]["key"]
    profiles = {key: {"opened_on": "2026-11-02", "invalidation": "失效"}}
    row = analyze_holdings(repo, market(), sectors(), service, "position_t", "a",
                           profiles=profiles, now=NOW)["rows"][0]
    assert row["constraints"]["can_size"]
    pos.update(patch)
    row = analyze_holdings(repo, market(), sectors(), service, "position_t", "a",
                           profiles=profiles, now=NOW)["rows"][0]
    assert not row["constraints"]["can_size"]


def test_unrelated_t_plan_does_not_invalidate_lots_research(tmp_path):
    repo = Repo(tmp_path)
    save_lot(repo)
    service, _, plans = position_service(active=True)
    plans[0]["symbol"] = "600001.SH"
    before = read_inputs(repo, service, "lots")["input_version"]
    plans[0]["unmatched_qty"] = 50
    assert read_inputs(repo, service, "lots")["input_version"] == before


def test_explicit_risk_limit_can_tighten_bear_trigger(tmp_path):
    repo = Repo(tmp_path)
    save_lot(repo, cost_price=15)
    key = read_inputs(repo, None, "lots")["positions"][0]["key"]
    row = analyze(repo, profiles={key: {"max_loss_pct": 5, "invalidation": "风险预算耗尽"}})["rows"][0]
    bear = next(s for s in row["plan"]["scenarios"] if s["key"] == "bear")
    assert bear["price_trigger"]["value"] >= 14.25


def test_t_exposure_read_failure_is_explicit_adoption_block(tmp_path):
    repo = Repo(tmp_path)
    save_lot(repo)
    service, _, _ = position_service()

    def fail():
        raise RuntimeError("fixture outage")

    service.store.list_accounts = fail
    key = read_inputs(repo, None, "lots")["positions"][0]["key"]
    result = analyze_holdings(repo, market(), sectors(), service, "lots",
                              profiles={key: {"invalidation": "失效"}}, now=NOW)
    assert not result["rows"][0]["plan"]["can_adopt"]
    assert any("敞口" in note for note in result["rows"][0]["warnings"])


def test_corrupt_lot_cannot_leave_silent_partial_inventory(tmp_path):
    repo = Repo(tmp_path)
    save_lot(repo)
    broken = tmp_path / "user_data" / "lots" / "lot_broken.json"
    broken.write_text("{bad-json", encoding="utf-8")
    inputs = read_inputs(repo, None, "lots")
    assert inputs["positions"][0]["qty"] is None
    row = analyze(repo)["rows"][0]
    assert not row["plan"]["can_adopt"]
    assert any("损坏" in note or "完整" in note for note in row["warnings"])


@pytest.mark.parametrize("max_days", [5, 250])
def test_unknown_future_calendar_expiry_blocks_otherwise_complete_report(tmp_path, max_days):
    near_end = datetime(2026, 12, 30, 10, tzinfo=CN_TZ)
    days = load_market_calendar(BUNDLED_CALENDAR_PATH, start=date(2026, 8, 1),
                                end=date(2026, 12, 29))[-65:]
    mapping = dict(zip(HISTORY_DAYS, days, strict=True))
    history = history_frame().with_columns(pl.col("date").replace_strict(mapping).alias("date"))
    repo = Repo(tmp_path, history)
    save_lot(repo)
    key = read_inputs(repo, None, "lots")["positions"][0]["key"]
    row = analyze_holdings(repo, market({SYMBOL: quote(stamp=near_end)}), sectors(), None,
                           "lots", profiles={key: {"invalidation": "失效", "max_holding_days": max_days}},
                           now=near_end)["rows"][0]
    assert row["price"]["fresh"] and row["technicals"]["support"] is not None
    assert row["plan"]["expires_on"] is None and not row["plan"]["can_adopt"]
    assert all(s["price_trigger"] is None for s in row["plan"]["scenarios"])
    assert any("日历" in s and "期限" in s for s in row["warnings"])


def test_evidence_explains_observed_stock_facts_and_matching_sector_basis(tmp_path):
    repo = Repo(tmp_path)
    save_lot(repo)
    row = analyze(repo)["rows"][0]
    text = "\n".join(row["evidence"])
    assert "已持有3个交易日" in text and "价差+40.00%" in text
    assert "14.0000元" in text and f"MA20 {row['technicals']['ma20']:.4f}元" in text
    assert f"支撑{row['technicals']['support']:.4f}元" in text and "支撑位 S1" in text
    assert "source-a" in text and "个股+6.06% / 板块+6.06%" in text
    assert "数量100" not in text and "成本10" not in text


def test_evidence_states_unknown_values_without_manufacturing_explanations(tmp_path):
    repo = Repo(tmp_path, history_frame().drop("raw_close", "raw_high", "raw_low"))
    save_lot(repo, buy_date=None)
    row = analyze(repo, sector_service=sectors([]))["rows"][0]
    text = "\n".join(row["evidence"])
    assert "持仓天数未知" in text and "MA20未知" in text
    assert "支撑未知" in text and "同口径涨跌对比未知" in text
    assert "高于MA20" not in text and "低于MA20" not in text
