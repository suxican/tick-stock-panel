import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.custom.etf_observation import router
from app.custom.etf_observation_data import read_model
from app.custom.etf_observation_data.ledger import RESOURCES, TABLE


@pytest.fixture
def page(tmp_path, monkeypatch):
    monkeypatch.setattr(read_model, "cn_today", lambda: date(2026, 11, 6))
    app = FastAPI()
    app.include_router(router)
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    return TestClient(app)


def _partition(root: Path, day: str, prev: str, *, omit=(), amount=2):
    scope = json.loads((RESOURCES / "scope.json").read_text(encoding="utf-8"))
    table = root / "ext_data" / TABLE
    table.mkdir(parents=True, exist_ok=True)
    (table / "config.json").write_text(
        json.dumps(
            {
                "id": TABLE,
                "label": "宽基 ETF 日度份额与净申购估算",
                "mode": "timeseries",
                "fields": [],
            }
        ),
        encoding="utf-8",
    )
    dest = table / "timeseries" / f"date={day}"
    dest.mkdir(parents=True, exist_ok=True)
    data = []
    for item in scope:
        if item["symbol"] in omit:
            continue
        whole = item["scope"] == "fund_total_verified"
        data.append(
            dict(
                symbol=item["symbol"],
                scope=item["scope"],
                date=day,
                previous_date=prev,
                listed_units=100 + amount,
                previous_listed_units=100,
                listed_change_units=amount,
                unit_nav_cny=2.0,
                listed_change_value_cny=2.0 * amount,
                fund_net_units=amount if whole else None,
                fund_net_estimate_cny=2.0 * amount if whole else None,
                status="ok",
                shares_source="https://www.sse.com.cn/",
                previous_shares_source="https://www.sse.com.cn/",
                nav_source="https://www.chinaamc.com/",
            )
        )
    pl.DataFrame(data).write_parquet(dest / "part.parquet")


def test_latest_complete_and_missing_fund_are_separate(page, tmp_path):
    _partition(tmp_path, "2026-11-02", "2026-10-30")
    _partition(tmp_path, "2026-11-03", "2026-11-02", omit={"510310.SH"})
    snapshot = page.get("/api/etf-observation").json()
    assert snapshot["as_of"] == "2026-11-02"
    assert snapshot["latest_observed_date"] == "2026-11-03"
    assert snapshot["totals"] == {"listed_change_value_cny": 40.0, "fund_net_estimate_cny": 28.0}
    partial = page.get("/api/etf-observation", params={"as_of": "2026-11-03"}).json()
    assert partial["totals"] == {"listed_change_value_cny": None, "fund_net_estimate_cny": 28.0}
    absent = next(r for r in partial["funds"] if r["symbol"] == "510310.SH")
    assert absent["latest"] is None and absent["days_covered"] == 0
    _partition(tmp_path, "2026-11-03", "2026-11-02", amount=0)
    refreshed = page.get("/api/etf-observation").json()
    assert refreshed["as_of"] == "2026-11-03"
    assert refreshed["totals"] == {"listed_change_value_cny": 0.0, "fund_net_estimate_cny": 0.0}


def test_missing_session_inside_window_never_collapses_period(page, tmp_path):
    _partition(tmp_path, "2026-09-24", "2026-09-23")
    _partition(tmp_path, "2026-09-29", "2026-09-28")
    response = page.get("/api/etf-observation?window=20").json()
    assert response["dates"] == ["2026-09-24", "2026-09-28", "2026-09-29"]
    assert response["totals"]["fund_net_estimate_cny"] is None
    assert response["trend"][1]["listed_change_value_cny"] is None
    rows = page.get("/api/etf-observation/detail?symbol=510330.SH&window=20").json()["rows"]
    assert [r["status"] for r in rows] == ["ok", "missing", "ok"]
    assert rows[1]["listed_change_units"] is None and rows[-1]["fund_net_estimate_cny"] is None
    assert rows[-1]["listed_units"] == 102 and rows[-1]["unit_nav_cny"] == 2.0
    assert rows[-1]["nav_source"] == "https://www.chinaamc.com/"
    assert page.get("/api/etf-observation?as_of=2026-09-25").status_code == 404


def test_no_release_read_only_and_invalid_parameters(page, tmp_path):
    response = page.get("/api/etf-observation")
    assert response.status_code == 200 and response.json()["as_of"] is None
    assert response.json()["totals"]["listed_change_value_cny"] is None
    assert not (tmp_path / "ext_data").exists()
    assert page.post("/api/etf-observation").status_code == 405
    assert page.get("/api/etf-observation?window=22").status_code == 422
    assert page.get("/api/etf-observation/detail?symbol=SH").status_code == 422


def test_expired_scope_and_malformed_partitions_do_not_leak_to_totals(page, tmp_path):
    _partition(tmp_path, "2026-11-04", "2026-11-03")
    _partition(tmp_path, "2026-11-05", "2026-11-04")
    _partition(tmp_path, "2026-11-00", "2026-10-30")
    _partition(tmp_path, "1900-11-03", "1900-11-02")
    response = page.get("/api/etf-observation").json()
    assert response["dates"] == ["2026-11-04", "2026-11-05"]
    assert response["as_of"] == "2026-11-04"
    partial = page.get("/api/etf-observation?as_of=2026-11-05").json()
    assert partial["totals"] == {"listed_change_value_cny": 40.0, "fund_net_estimate_cny": None}
    assert {r["scope"] for r in partial["funds"]} == {"unverified"}


def test_disclosure_failure_is_isolated_from_financial_observations(page, tmp_path, monkeypatch):
    _partition(tmp_path, "2026-11-04", "2026-11-03")
    (tmp_path / "scope.json").write_bytes((RESOURCES / "scope.json").read_bytes())
    monkeypatch.setattr(read_model, "RESOURCES", tmp_path)
    source = tmp_path / "huijin_disclosures.json"
    reference = json.loads((RESOURCES / source.name).read_text(encoding="utf-8"))
    only = reference["funds"][0].copy()
    only["holders"] = []
    reviewed = {"reviewed_through": reference["reviewed_through"], "funds": [only]}
    source.write_text(json.dumps(reviewed), encoding="utf-8")
    assert page.get("/api/etf-observation").json()["disclosures"] == reviewed
    invalid_rows = dict(reviewed, funds=[dict(only, holders={})])
    for malformed in (None, [], {"funds": [0]}, invalid_rows):
        source.write_text(json.dumps(malformed), encoding="utf-8")
        response = page.get("/api/etf-observation").json()
        assert response["disclosures"] == {"reviewed_through": None, "funds": []}
        assert response["totals"]["fund_net_estimate_cny"] == 28
