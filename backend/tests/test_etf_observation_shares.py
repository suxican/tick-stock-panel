import csv
import gzip
import json
from datetime import date

import httpx
import pytest

from app.custom.etf_observation_data import shares


@pytest.mark.parametrize("value", ["0", "-1", "0.00001", "NaN", "Infinity"])
def test_invalid_exchange_units_are_rejected(value):
    with pytest.raises(ValueError):
        shares.shares_from_wan(value)


def test_sse_requires_complete_page_and_unique_fund_date():
    key = ("510310", "2024-09-20")
    query = shares.Query(shares.SSE_URL, "sse_split", frozenset([key]))
    row = {"SEC_CODE": key[0], "STAT_DATE": key[1], "TOT_VOL": "6234724.40"}

    def parse(total, rows):
        return shares._rows(query, json.dumps({"result": rows, "pageHelp": {"total": total}}))

    assert parse(1, [row]) == {key: 62_347_244_000}
    assert parse(1, [{**row, "STAT_DATE": "2024-09-19"}]) == {}
    with pytest.raises(ValueError, match="Incomplete SSE"):
        parse(2, [row])
    with pytest.raises(ValueError, match="Duplicate"):
        parse(2, [row, row])


def test_szse_offline_replay_preserves_report_code_date_units_and_url(tmp_path, monkeypatch):
    start = date(2026, 6, 29)
    end = date(2026, 6, 30)
    query = shares.Query(
        shares.SZSE_URL + "?txtDm=159919",
        "szse_fixture",
        frozenset({("159919", start.isoformat()), ("159919", end.isoformat())}),
    )
    metadata = {"recordcount": 2}
    rows = [
        {"fund_code": "159919", "size_date": end.isoformat(), "current_size": "693,921.66"},
        {"fund_code": "159919", "size_date": start.isoformat(), "current_size": "699,681.66"},
    ]
    payload = [{"data": rows, "metadata": metadata, "error": None}]
    raw = tmp_path / "raw" / "shares"
    raw.mkdir(parents=True)
    (raw / "szse_fixture.json.gz").write_bytes(gzip.compress(json.dumps(payload).encode()))
    monkeypatch.setattr(shares, "_queries", lambda _sessions: [query])
    assert not shares.collect_shares(tmp_path, start, end)
    with (tmp_path / "shares.csv").open(encoding="utf-8", newline="") as f:
        assert list(csv.reader(f)) == [
            ["symbol", "date", "shares", "source_url"],
            ["159919.SZ", "2026-06-29", "6996816600", query.url],
            ["159919.SZ", "2026-06-30", "6939216600", query.url],
        ]
    metadata["recordcount"] = 3
    with pytest.raises(ValueError, match="Incomplete SZSE"):
        shares._rows(query, json.dumps(payload))


def test_incomplete_refresh_preserves_published_snapshot_and_evidence(tmp_path, monkeypatch):
    day = date(2026, 9, 30)
    key = ("510300", day.isoformat())
    query = shares.Query(shares.SSE_URL, "sse_fixture", frozenset([key]))
    monkeypatch.setattr(shares, "_queries", lambda _sessions: [query])
    raw = tmp_path / "raw" / "shares" / "sse_fixture.json.gz"
    raw.parent.mkdir(parents=True)
    raw.write_bytes(b"damaged cache")
    row = {"SEC_CODE": key[0], "STAT_DATE": key[1], "TOT_VOL": "2411958.77"}
    rows = [row]

    def get(_client, url, *, headers):
        return httpx.Response(
            200,
            request=httpx.Request("GET", url, headers=headers),
            json={"result": rows, "pageHelp": {"total": len(rows)}},
        )

    monkeypatch.setattr(httpx.Client, "get", get)
    assert shares.collect_shares(tmp_path, day, day) == []
    raw_good = raw.read_bytes()
    assert json.loads(gzip.decompress(raw_good))["result"] == [row]
    csv_path = tmp_path / "shares.csv"
    normalized_good = csv_path.read_bytes()

    rows.clear()
    assert shares.collect_shares(tmp_path, day, day, refresh=True) == [key]
    assert raw.read_bytes() == raw_good
    assert csv_path.read_bytes() == normalized_good
