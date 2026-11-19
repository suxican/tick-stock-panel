"""GET /api/regime/seal-quality: 区间过滤、日期校验、空数据。"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import regime as regime_api
from app.services import seal_quality as sq


@pytest.fixture
def client(tmp_path, monkeypatch):
    calls = []

    def payload(data_dir, start, end):
        calls.append((data_dir, start, end))
        return {"rows": [{"date": "2026-11-18"}], "latest": None, "first_date": "2018-09-12",
                "reopen_from": "2020-01-02"}

    monkeypatch.setattr(sq, "seal_quality_payload", payload)
    app = FastAPI()
    app.include_router(regime_api.router)
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    return TestClient(app), calls, tmp_path


def test_passes_range_and_data_dir(client):
    http, calls, data_dir = client
    body = http.get("/api/regime/seal-quality", params={"start": "2026-01-02", "end": "2026-11-18"}).json()
    assert body["rows"] == [{"date": "2026-11-18"}]
    assert calls == [(data_dir, date(2026, 1, 2), date(2026, 11, 18))]


def test_range_optional(client):
    http, calls, _ = client
    assert http.get("/api/regime/seal-quality").status_code == 200
    assert calls[0][1:] == (None, None)


def test_bad_date_is_422(client):
    http, _, _ = client
    assert http.get("/api/regime/seal-quality", params={"start": "2026-13-01"}).status_code == 422


def test_real_payload_empty_when_no_archive(tmp_path):
    sq.clear_cache()
    out = sq.seal_quality_payload(tmp_path, None, None)
    assert out == {"rows": [], "latest": None, "first_date": None, "reopen_from": "2020-01-02"}
