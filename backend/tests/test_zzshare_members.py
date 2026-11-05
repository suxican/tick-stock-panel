import hashlib
import json
from datetime import date, datetime
from types import SimpleNamespace

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.custom import zzshare
from app.services import zzshare_radar as radar

DAY = date(2026, 11, 3)
CODE = "801159"
URL = f"/api/zzshare/themes/{CODE}"


def archive(root, day=DAY, code=CODE, members=None):
    members = {"600001": "甲", "920001": "乙", "110077": "转债"} if members is None else members
    name = hashlib.sha256(code.encode()).hexdigest()
    file = root / "zzshare_radar" / "v1" / str(day) / "members" / f"{name}.json"
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(json.dumps({"date": str(day), "code": code, "count": len(members), "members": members}), encoding="utf-8")
    return file


@pytest.fixture
def rig(tmp_path, monkeypatch):
    app = FastAPI()
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    app.state.repo = repo
    app.include_router(zzshare.router)
    monkeypatch.setattr(radar, "cn_today", lambda: date(2026, 11, 4))
    return TestClient(app), repo


def test_day_quotes_are_left_joined_and_use_host_units(rig, monkeypatch, tmp_path):
    client, _ = rig
    archive(tmp_path)
    calls = []
    def quotes(_repo, day):
        calls.append(day)
        return pl.DataFrame([
            {"date": day, "symbol": "600001.SH", "close": 3.5, "raw_close": 10., "change_pct": 0.025,
             "turnover_rate": 4.3, "volume": 500, "amount": 501000., "consecutive_limit_ups": 0, "consecutive_limit_downs": 0},
            {"date": date(2026, 11, 4), "symbol": "920001.BJ", "close": 9., "raw_close": 20., "change_pct": .07,
             "turnover_rate": 1.5, "volume": 100, "amount": 300000., "consecutive_limit_ups": 0, "consecutive_limit_downs": 0},
        ])
    monkeypatch.setattr(radar, "_quote_frame", quotes)
    res = client.get(URL + "/members", params={"date": DAY})
    assert res.status_code == 200
    data = res.json()
    assert (data["date"], data["plate_code"], data["total"], data["quote_count"]) == (str(DAY), CODE, 3, 1)
    rows = {r["symbol"]:r for r in data["rows"]}
    assert set(rows) == {"600001.SH", "920001.BJ", "110077"}
    assert rows["600001.SH"]["close"] == 10 and rows["600001.SH"]["change_pct"] == .025
    assert rows["600001.SH"]["turnover_rate"] == 4.3 and rows["600001.SH"]["amount"] == 501000
    assert rows["920001.BJ"]["name"] == "乙" and rows["920001.BJ"]["close"] is None
    assert rows["110077"]["change_pct"] is None and calls == [DAY]


def test_missing_corrupt_or_wrong_date_extents_never_fetch_or_overwrite(rig, tmp_path):
    client, _ = rig
    assert client.get(URL + "/members", params={"date": DAY}).status_code == 404
    path = archive(tmp_path)
    assert client.get(URL + "/members", params={"date":"2026-11-02"}).status_code == 404
    value = json.loads(path.read_text(encoding="utf-8"))
    for override in ({"date":"2026-11-04"}, {"code":"1"}, {"count":5}, {"members":[]}, {"members":{"..":"X"}}):
        bad = json.dumps({**value, **override})
        path.write_text(bad, encoding="utf-8")
        assert client.get(URL + "/members", params={"date": DAY}).status_code == 409
        assert path.read_text(encoding="utf-8") == bad
    assert client.get(URL + "/members", params={"date":"2026-11-05"}).status_code == 422
    assert client.get("/api/zzshare/themes/not!valid/members", params={"date":DAY}).status_code == 422


def test_intraday_uses_the_same_exact_date_members_and_never_substitutes_a_later_day(rig, tmp_path):
    client, _ = rig
    archive(tmp_path, members={"600001": "甲"})
    archive(tmp_path, day=date(2026, 11, 4), members={"600002":"乙"})
    prior = tmp_path / "kline_daily" / "date=2026-11-02"
    prior.mkdir(parents=True)
    pl.DataFrame({"symbol": ["600001.SH","600002.SH"], "close":[10.,10.]}).write_parquet(prior / "part.parquet")
    minute = tmp_path / "kline_minute" / f"date={DAY}"
    minute.mkdir(parents=True)
    pl.DataFrame({
        "symbol":["600001.SH", "600002.SH"],
        "datetime":[datetime(2026, 11, 3, 9, 31)] * 2,
        "close":[10.5, 9.5],
    }).write_parquet(minute / "part.parquet")
    actual = client.get(URL + "/intraday", params={"date": DAY})
    assert actual.status_code == 200
    assert actual.json()["points"] == [{"time":"09:31", "sector": .05, "market":0.}]
    assert actual.json()["member_count"] == actual.json()["members_with_minute"] == 1
    assert client.get(URL + "/intraday", params={"date":"2026-11-02"}).status_code == 404
    missing = client.get(URL + "/intraday", params={"date":"2026-11-04"}).json()
    assert (missing["status"],missing["date"],missing["points"]) == ("no_data","2026-11-04",[])
