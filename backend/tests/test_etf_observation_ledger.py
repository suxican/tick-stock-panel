from decimal import Decimal

import polars as pl
import pytest

from app.api.ext_data import _read_ext_dataframe
from app.custom.etf_observation_data.ledger import (
    TABLE,
    daily_rows,
    observations,
    publish,
    reconcile_quarters,
    split_check,
)
from app.services.ext_data import ExtConfigStore

URL = "https://example.com/source"


def test_missing_previous_session_and_unknown_scope_are_never_zero_flows():
    days = ["2026-09-24", "2026-09-28", "2026-09-29", "2026-09-30"]
    symbol = "159919.SZ"
    shares = {(symbol, d): (100, URL) for d in (days[0], days[1], days[3])}
    navs = {(symbol, d): (Decimal("4.6001"), URL) for d in days}
    rows = daily_rows(days, [symbol], shares, navs, {symbol: "unverified"})
    assert [r["status"] for r in rows] == ["ok", "missing_shares", "missing_previous_shares"]
    assert [r["listed_change_value_cny"] for r in rows] == [0, None, None]
    assert all(r["fund_net_estimate_cny"] is None for r in rows)
    assert rows[0]["previous_date"] == days[0]


def test_listed_only_and_whole_fund_columns_preserve_direction():
    days = ["2026-11-03", "2026-11-04"]
    scopes = {"510310.SH": "listed_only", "510300.SH": "fund_total_verified"}
    shares = {
        (symbol, day): (v, URL)
        for symbol in scopes
        for day, v in zip(days, [2_000, 100], strict=True)
    }
    navs = {(symbol, days[-1]): (Decimal("4.0041"), URL) for symbol in scopes}
    listed, whole = daily_rows(days, scopes, shares, navs, scopes)
    assert (
        listed["listed_change_value_cny"] == whole["fund_net_estimate_cny"] == Decimal("-7607.7900")
    )
    assert listed["fund_net_estimate_cny"] is None
    assert daily_rows(days, scopes, shares, {}, scopes)[1]["fund_net_estimate_cny"] is None


def test_quarter_tolerance_applies_after_subtracting_declared_off_market_units():
    key = ("159915.SZ", "2026-09-30")
    nav = {key: (Decimal("3.1551"), URL)}
    previous = ("159915.SZ", "2026-06-30")
    listed = 20_615_471_290 - 6_016_354
    for error, state in ((-36, "pass"), (-100, "check")):
        checks = reconcile_quarters(
            {previous: (10_746_454_900, URL), key: (listed + error, URL)}, nav
        )
        row = next(r for r in checks if (r["symbol"], r["date"]) == key)
        assert row["shares_residual"] == error
        assert row["listed_minus_fund"] == error - 6_016_354
        assert row["disclosed_otc_change_from_prior_quarter"] == 2_447_731
        assert row["quarterly_change_residual"] == error + 36
        assert row["status"] == state


def test_conversion_removes_actual_per_holder_rounding_but_keeps_real_flows():
    symbol = "510310.SH"
    before, day, after = "2024-09-19", "2024-09-20", "2024-09-23"
    shares = {
        (symbol, d): (v, URL)
        for d, v in zip(
            [before, day, after], [121_864_307_400, 62_347_244_000, 62_403_944_000], strict=True
        )
    }
    nav = {(symbol, day): (Decimal("3.2021"), URL), (symbol, after): (Decimal("3.2138"), URL)}
    result = split_check(shares, nav)
    assert result["status"] == "pass"
    assert result["rounding_units"] == Decimal("48100.89477565")
    assert result["naive_change_units"] < 0
    assert result["event_listed_change_value_cny"] == Decimal("4618465824.0045")
    assert result["next_change_units"] == 56_700_000
    nav[symbol, day] = Decimal("1.6003"), URL
    assert split_check(shares, nav)["event_listed_change_value_cny"] is None


def test_duplicate_source_rows_stop_before_calculation(tmp_path):
    path = tmp_path / "shares.csv"
    row = f"510300.SH,2026-11-04,100,{URL}\n"
    path.write_text("symbol,date,shares,source_url\n" + row * 2, encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate"):
        observations(path, "shares")


def test_native_release_can_be_read_and_repeated_without_replacing_user_data(tmp_path):
    part = pl.DataFrame(
        {"symbol": ["159919.SZ"], "date": ["2026-11-04"], "amount": [Decimal("8.3512")]}
    )
    publish(part, tmp_path)
    config = ExtConfigStore(tmp_path).get(TABLE)
    actual, day = _read_ext_dataframe(config, tmp_path, "2026-11-04")
    assert day == "2026-11-04"
    assert actual.row(0, named=True)["amount"] == 8.3512
    publish(part, tmp_path)
    with pytest.raises(ValueError, match="differs"):
        publish(part.with_columns(pl.lit(Decimal("2.0000")).alias("amount")), tmp_path)


def test_rows_after_scope_review_are_listed_only():
    days = ["2026-11-03", "2026-11-04", "2026-11-05"]
    symbol = "159919.SZ"
    shares = {(symbol, d): (100 + i, URL) for i, d in enumerate(days)}
    navs = {(symbol, d): (Decimal("2"), URL) for d in days}
    scopes = {symbol: "fund_total_verified"}
    reviewed, later = daily_rows(days, [symbol], shares, navs, scopes, {symbol: "2026-11-04"})
    assert reviewed["scope"] == "fund_total_verified" and reviewed["fund_net_estimate_cny"] == 2
    assert later["scope"] == "unverified" and later["fund_net_estimate_cny"] is None
    assert later["listed_change_value_cny"] == 2


def test_release_only_grows_after_its_last_day(tmp_path):
    def day(d, amount="1.5"):
        return pl.DataFrame({"symbol": ["159919.SZ"], "date": [d], "amount": [Decimal(amount)]})

    assert publish(day("2026-11-04"), tmp_path) == ["2026-11-04"]
    grown = pl.concat([day("2026-11-04"), day("2026-11-05"), day("2026-11-06")])
    assert publish(grown, tmp_path) == ["2026-11-05", "2026-11-06"]
    assert publish(grown, tmp_path) == []
    folder = tmp_path / "ext_data" / TABLE / "timeseries"
    assert sorted(p.name for p in folder.iterdir()) == [
        "date=2026-11-04", "date=2026-11-05", "date=2026-11-06",
    ]
    with pytest.raises(ValueError, match="differs"):
        publish(pl.concat([day("2026-11-04"), day("2026-11-05", "9")]), tmp_path)
    with pytest.raises(ValueError, match="backfill"):
        publish(pl.concat([day("2026-11-03"), day("2026-11-04")]), tmp_path)
    assert not (folder / "date=2026-11-03").exists()


def test_recited_source_query_is_the_same_observation_but_another_host_is_not(tmp_path):
    def day(source, amount="1.5"):
        return pl.DataFrame({
            "symbol": ["159919.SZ"], "date": ["2026-11-04"], "amount": [Decimal(amount)], "nav_source": [source],
        })

    publish(day("https://www.szse.cn/api?txtEnd=2026-11-04"), tmp_path)
    assert publish(day("https://www.szse.cn/api?txtEnd=2026-11-10"), tmp_path) == []
    with pytest.raises(ValueError, match="differs"):
        publish(day("https://elsewhere.example/api"), tmp_path)
    with pytest.raises(ValueError, match="differs"):
        publish(day("https://www.szse.cn/api?txtEnd=2026-11-10", "1.6"), tmp_path)
