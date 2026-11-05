import json
from datetime import date
from decimal import Decimal

import httpx
import pytest

from app.custom.etf_observation_data import nav


def test_split_day_uses_unit_nav_not_cumulative(tmp_path):
    day = "2024-09-20"

    def respond(request):
        if str(request.url).startswith(nav.EFUNDS):
            return httpx.Response(
                200,
                json={
                    "status": 1,
                    "data": {
                        "total": 1,
                        "data": [{"navDate": day, "netValue": "3.2021", "totalNetValue": "1.6003"}],
                    },
                },
            )
        return httpx.Response(
            200,
            json={
                "iTotalDisplayRecords": 1,
                "aaData": [
                    {
                        "code": "510310",
                        "valuationDate": day,
                        "shareNetValue": "3.2021",
                        "totalNetValue": "1.6003",
                        "fund": {"idStr": 123},
                    }
                ],
            },
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        source = nav.NavSource(client, tmp_path, refresh=False)
        for read in (source.manager, source.regulatory_reports):
            assert read("510310", {day})[day][0] == Decimal("3.2021")


def test_pcf_attributes_previous_trading_day_and_uses_nav_per_share():
    fields = {
        "TRADE_CODE": "510300",
        "TRADING_DAY": "20261105",
        "PRE_TRADING_DAY": "20261104",
        "NAV": "￥4.1785",
        "NAVPERCU": "￥3760682.55",
    }
    body = json.dumps({"result": [fields]}).encode()
    assert nav._pcf_observation(body, "510300") == ("2026-11-04", Decimal("4.1785"))
    with pytest.raises(ValueError):
        nav._pcf_observation(body, "510310")
    fields["PRE_TRADING_DAY"] = "20261105"
    with pytest.raises(ValueError):
        nav._pcf_observation(json.dumps({"result": [fields]}).encode(), "510300")


@pytest.mark.parametrize(
    "manager,regulator,vendor,expected",
    [
        (None, None, None, "missing"),
        ("3.2021", None, "3.2021", "missing"),
        (None, "3.2021", None, "missing"),
        ("1.6003", "3.2021", "3.2021", "conflict"),
    ],
)
def test_incomplete_refresh_preserves_published_file(
    tmp_path, monkeypatch, manager, regulator, vendor, expected
):
    day = "2026-11-04"
    path = tmp_path / "nav.csv"
    path.write_bytes(b"previous complete publication")
    monkeypatch.setattr(nav, "SSE", ("510050",))
    monkeypatch.setattr(nav, "SZSE", ())
    monkeypatch.setattr(nav, "QUARTER", ())

    def record(value):
        return {day: (Decimal(value), "", "")} if value else {}

    monkeypatch.setattr(nav.NavSource, "manager", lambda *args: record(manager))
    monkeypatch.setattr(nav.NavSource, "regulatory_reports", lambda *args: record(regulator))
    monkeypatch.setattr(nav.NavSource, "eastmoney", lambda *args: record(vendor))
    start = end = date.fromisoformat(day)
    if expected == "conflict":
        with pytest.raises(ValueError, match="conflicting unit NAVs"):
            nav.collect_nav(tmp_path, start, end, refresh=True)
    else:
        assert nav.collect_nav(tmp_path, start, end, refresh=True) == [("510050", day)]
    assert path.read_bytes() == b"previous complete publication"
