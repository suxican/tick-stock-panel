from datetime import date

import polars as pl

from app.services.first_board_verification import apply_verification, close_results, paper_results

DAY = date(2026, 10, 15)


def daily(*rows):
    return pl.DataFrame([{"symbol": s, "date": DAY, "raw_close": c, "raw_high": h, "raw_prev_close": 10.0,
                          "limit_up_price": 10.995} for s, c, h in rows],
                        schema={"symbol": pl.String, "date": pl.Date, "raw_close": pl.Float64, "raw_high": pl.Float64,
                                "raw_prev_close": pl.Float64, "limit_up_price": pl.Float64})


def test_close_results_sealed_not_sealed_and_missing_row():
    result = close_results(daily(("600001.SH", 11.0, 11.0), ("600002.SH", 10.6, 11.0)), ["600001.SH", "600002.SH", "600003.SH"], DAY, closed=True)
    assert result["600001.SH"] == {"status": "sealed", "close_price": 11.0, "limit_price": 11.0, "reason": None}
    assert result["600002.SH"]["status"] == "not_sealed" and result["600002.SH"]["close_price"] == 10.6
    assert result["600003.SH"]["status"] == "unverified" and result["600003.SH"]["reason"]


def test_close_results_inconsistent_prices_are_unverified_not_unsealed():
    result = close_results(daily(("600001.SH", 11.5, 11.5), ("600002.SH", 10.9, 10.5)), ["600001.SH", "600002.SH"], DAY, closed=True)
    assert result["600001.SH"]["status"] == "unverified" and result["600002.SH"]["status"] == "unverified"


def test_close_results_missing_price_columns_and_empty_frame_are_unverified():
    broken = daily(("600001.SH", 11.0, 11.0)).with_columns(pl.lit(None, dtype=pl.Float64).alias("raw_close"))
    assert close_results(broken, ["600001.SH"], DAY, closed=True)["600001.SH"]["status"] == "unverified"
    assert close_results(pl.DataFrame(), ["600001.SH"], DAY, closed=True)["600001.SH"]["status"] == "unverified"


def test_close_results_before_close_is_unverified_even_with_a_row():
    result = close_results(daily(("600001.SH", 11.0, 11.0)), ["600001.SH"], DAY, closed=False)
    assert result["600001.SH"]["status"] == "unverified" and "尚未收盘" in result["600001.SH"]["reason"]


def trade(symbol, status, signal_id="e", orders=(), net=None):
    return {"signal_id": signal_id, "symbol": symbol, "status": status, "reason": f"理由-{status}",
            "orders": [{"side": side} for side in orders], "closed_net_return": net}


def audit(*trades, status="ready", truncated=False):
    return {"status": status, "reason": "审计说明", "coverage": {"truncated": truncated}, "trades": list(trades)}


def test_paper_results_uses_the_single_ordered_row_and_carries_closed_return():
    result = paper_results(audit(trade("A", "closed", "e1", ["buy", "sell"], .031), trade("A", "not_ordered", "e2")), ["A"], has_account=True)
    assert result["A"] == {"status": "closed", "reason": "理由-closed", "net_return": .031}


def test_paper_results_two_buy_orders_across_events_is_ambiguous():
    result = paper_results(audit(trade("A", "open", "e1", ["buy"]), trade("A", "pending", "e2", ["buy"])), ["A"], has_account=True)
    assert result["A"]["status"] == "ambiguous" and result["A"]["net_return"] is None


def test_paper_results_not_ordered_evidence_missing_and_no_signal():
    result = paper_results(audit(trade("A", "not_ordered", "e1"), trade("A", "not_ordered", "e2"), trade("B", "not_ordered", "e3"),
                                 trade("B", "evidence_missing", "e4")), ["A", "B", "C"], has_account=True)
    assert result["A"]["status"] == "not_ordered"
    assert result["B"]["status"] == "evidence_missing"
    assert result["C"]["status"] == "no_signal"


def test_paper_results_no_account_unavailable_and_truncated():
    assert paper_results(None, ["A"], has_account=False)["A"]["status"] == "no_account"
    for bad in (audit(trade("A", "open", "e", ["buy"]), status="unavailable"), audit(trade("A", "open", "e", ["buy"]), truncated=True), None):
        result = paper_results(bad, ["A"], has_account=True)
        assert result["A"]["status"] == "evidence_missing" and result["A"]["reason"]


def report(*rows):
    return {"day": "2026-10-15", "revision": 4, "rows": list(rows),
            "summary": {"stock_count": len(rows), "alerted_count": sum(r["alerted"] for r in rows)}}


def test_apply_verification_attaches_rows_counts_and_denominators():
    rows = [{"symbol": "A", "alerted": True}, {"symbol": "B", "alerted": True}, {"symbol": "C", "alerted": False}]
    close = {"A": {"status": "sealed"}, "B": {"status": "not_sealed"}, "C": {"status": "unverified"}}
    paper = {"A": {"status": "closed"}, "B": {"status": "ambiguous"}, "C": {"status": "no_signal"}}
    result = apply_verification(report(*rows), close, paper)
    assert result["verified"] is True
    assert [row["close"]["status"] for row in result["rows"]] == ["sealed", "not_sealed", "unverified"]
    assert [row["paper"]["status"] for row in result["rows"]] == ["closed", "ambiguous", "no_signal"]
    assert {key: result["summary"][key] for key in ("close_sealed_count", "close_checked_count", "paper_filled_count", "paper_checked_count")} == {
        "close_sealed_count": 1, "close_checked_count": 2, "paper_filled_count": 1, "paper_checked_count": 1}
