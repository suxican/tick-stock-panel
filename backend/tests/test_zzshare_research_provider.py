import httpx
import pytest

from app.plugins.zzshare import provider as zz

D = "2026-11-03"


def source(monkeypatch, data, *, status=200):
    calls = []
    monkeypatch.setattr(zz, "_INTERVAL", 0)

    def handle(req):
        calls.append((req.url.path, dict(req.url.params)))
        return httpx.Response(status, json=data)

    return zz.ZzshareProvider(transport=httpx.MockTransport(handle)), calls


def test_rank_and_membership_keep_vendor_score_and_date_boundaries(monkeypatch):
    rank = {"date1": D, "time": D + " 18:01:08", "plate_type": 17, "plate_code": "801004",
            "plate_name": "锂电池", "score": 5568, "rate": -0.8, "money_leader": 1071050000,
            "trade_money": 240655000000}
    p, calls = source(monkeypatch, {"code": 200, "data": [rank]})
    assert p.research("plates_rank", D)[0] == {
        "trade_date": D, "plate_type": 17, "plate_code": "801004", "plate_name": "锂电池",
        "source_score": 5568, "change_pct": -0.8, "net_flow_yuan": 1071050000,
        "amount_yuan": 240655000000, "updated_at": D + "T18:01:08+08:00",
    }
    assert calls[0][0].endswith("/17/rank")
    member = {"plate_type": 17, "plate_code": "801004", "stock_name": "示例", "stock_code": "000008",
              "time_in": "2025-11-11", "time_out": None}
    rows = [dict(member, stock_code=f"{i:06}") for i in range(21)]
    p, calls = source(monkeypatch, {"code": 20000, "data": rows})
    got = p.research("plates_stocks", D, code="801004", limit=1)
    assert len(got) == 21 and got[8]["symbol"] == "000008.SZ"
    assert got[0]["trade_date"] == D
    assert calls[0][0].endswith("/17/801004/stocks") and "date" in calls[0][1]
    rows[0]["time_in"] = "2026-11-04"
    with pytest.raises(zz.ZzshareError):
        p.research("plates_stocks", D, code="801004")


def test_directory_has_no_request_date_and_no_historical_claim(monkeypatch):
    p, calls = source(monkeypatch, {"code": 20000, "data": [{
        "plate_code": "801001", "plate_type": 17, "plate_name": "芯片", "date_in": None, "date_out": None,
    }]})
    row = p.research("plates_list", D)[0]
    assert calls == [("/v3/market/plates/17", {})]
    assert "trade_date" not in row and row["date_in"] is None


def test_sentiment_range_and_intraday_positional_series(monkeypatch):
    raw = {"modal_id": 0, "date": "20261102", "p_open": 10, "p_high": 12, "p_low": 8,
           "p_close": 9, "p_close_pre1d": 8}
    p, _ = source(monkeypatch, {"code": 200, "data": [raw]})
    assert p.research("market_sentiment", D, start="2026-11-02") == [{
        "trade_date": "2026-11-02", "open": 10, "high": 12, "low": 8, "close": 9, "previous_close": 8,
    }]
    with pytest.raises(zz.ZzshareError):
        p.research("market_sentiment", D)
    p, calls = source(monkeypatch, {"code": 20000, "data": [["09:30:00", 4.29, 0]]})
    assert p.research("sentiment_trend", D)[0] == {"trade_date": D, "time": "09:30:00",
                                                   "value": 4.29, "aux_value": 0}
    assert calls[0][0].endswith("/trend/1")


def test_ths_hot_validates_date_and_percent(monkeypatch):
    row = {"symbol_code": "300308", "symbol_name": "中际旭创", "collect_date": D,
           "rank": 1, "rank_diff": 3, "last_pct": -12.69, "last_price": 480.2,
           "update_time": D + " 23:31:39"}
    p, _ = source(monkeypatch, {"code": 20000, "data": [row]})
    got = p.research("ths_hot_top", D)[0]
    assert got["symbol"] == "300308.SZ" and got["change_pct"] == -12.69
    assert got["updated_at"].endswith("+08:00")
    row["update_time"] = "2026-11-04 00:01:00"
    got = p.research("ths_hot_top", D)[0]
    assert got["rank"] == 1 and got["updated_at"].startswith("2026-11-04")
    assert got["change_pct"] is None and got["price_yuan"] is None
    row["collect_date"] = "2026-11-04"
    with pytest.raises(zz.ZzshareError):
        p.research("ths_hot_top", D)


def test_lhb_iso_dates_seat_rows_and_history_cutoff(monkeypatch):
    listing = {"stock_code": "002580", "stock_name": "圣阳股份", "quote_change": -10,
               "up_reason": "偏离值", "buy_in": -25550600, "turnover": 7710430000}
    p, calls = source(monkeypatch, {"code": 20000, "data": [listing]})
    assert p.research("lhb_list", D)[0]["net_buy_yuan"] == -25550600
    assert calls[0][1]["date1"] == D
    seat = {"trader_name": "机构专用", "type": 1, "rank": 1, "buy_amount": 70313000, "sell_amount": 39656100}
    p, calls = source(monkeypatch, {"code": 20000, "data": {"detail": listing,
                                                            "traders": [seat, dict(seat, type=2)]}})
    rows = p.research("lhb_detail", D, code="002580.SZ")
    assert [r["side"] for r in rows] == ["buy", "sell"]
    assert rows[0]["seat_name"] == "机构专用" and rows[0]["sell_yuan"] == 39656100
    assert calls[0][1] == {"date1": D, "stock_code": "002580"}
    hist = {"date": "2025-02-24", "buy_in": 76485400, "quote_change": 10.01}
    p, _ = source(monkeypatch, {"code": 20000, "data": [hist, dict(hist, date="2026-11-05"),
                                                         dict(hist, date="2026-10-20")]})
    rows = p.research("lhb_stock_history", D, code="002580", limit=1)
    assert len(rows) == 1 and rows[0]["trade_date"] == "2026-10-20"


def test_null_detail_only_valid_on_success_and_unsuitable_shapes_fail(monkeypatch):
    p, _ = source(monkeypatch, {"code": 20000, "data": None})
    assert p.research("lhb_detail", D, code="002580") == []
    with pytest.raises(zz.ZzshareError):
        p.research("lhb_list", D)
    p, _ = source(monkeypatch, {"code": 403, "data": None})
    with pytest.raises(zz.ZzshareError) as error:
        p.research("lhb_detail", D, code="002580")
    assert error.value.status_code == 403
    p, _ = source(monkeypatch, {"code": 20000}, status=429)
    with pytest.raises(zz.ZzshareError) as error:
        p.research("plates_list", D)
    assert error.value.status_code == 429


def test_rank_does_not_expose_inferred_midnight_or_other_day_quote(monkeypatch):
    row = {"date1": D, "time": D, "plate_type": 17, "plate_code": "801004", "plate_name": "锂电池",
           "score": 5568, "rate": -0.8, "money_leader": 100, "trade_money": 200}
    p, _ = source(monkeypatch, {"code": 200, "data": [row]})
    for stamp in (D, "2026-11-04 00:00:11"):
        row["time"] = stamp
        with pytest.raises(zz.ZzshareError):
            p.research("plates_rank", D)


@pytest.mark.parametrize(("kind", "kwargs"), [
    ("bogus", {}), ("plates_rank", {"limit": 1001}), ("plates_list", {"plate_type": 99}),
    ("plates_stocks", {"code": "../rank"}), ("lhb_detail", {"code": "600000.SZ"}),
    ("sentiment_trend", {"model": -1}), ("market_sentiment", {"start": "2027-01-01"}),
])
def test_invalid_request_does_not_reach_upstream(monkeypatch, kind, kwargs):
    p, calls = source(monkeypatch, {"code": 20000, "data": []})
    with pytest.raises(ValueError):
        p.research(kind, D, **kwargs)
    assert not calls
