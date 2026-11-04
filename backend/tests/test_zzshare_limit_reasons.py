from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import Mock

import duckdb
import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.ext_data import router as ext_data_router
from app.custom import zzshare as extension
from app.extensions.registry import BackendExtensionRegistrar
from app.market_time import CN_TZ
from app.services import zzshare_limit_reasons as service
from app.services.ext_data import ExtConfigStore

DAY = "2026-10-21"
URL = "/api/zzshare/limit-reasons/sync"


def row(code="000001", reason="资产重组", day=DAY):
    return {"date": day, "stock_code": code, "stock_name": "样本", "reason": reason}


@pytest.fixture
def env(tmp_path, monkeypatch):
    app = FastAPI()
    registrar = BackendExtensionRegistrar(extension.EXTENSION_ID, api_version=1)
    extension.setup(registrar)
    app.include_router(registrar.routers[0])
    app.include_router(ext_data_router)
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path, db=Mock()), clear_cache=Mock())
    app.state.repo = repo
    provider = Mock()
    monkeypatch.setattr(service.custom_sources, "get_provider", lambda _: provider)
    monkeypatch.setattr(service, "cn_now", lambda: datetime(2026, 10, 22, 10, tzinfo=CN_TZ))
    return SimpleNamespace(
        client=TestClient(app),
        provider=provider,
        repo=repo,
        root=tmp_path / "ext_data" / service.TABLE_ID,
        partition=tmp_path
        / "ext_data"
        / service.TABLE_ID
        / "timeseries"
        / f"date={DAY}"
        / "part.parquet",
    )


def test_sync_aggregates_in_one_daily_ext_partition_and_reuses_it(env):
    env.provider.get_limit_reasons.return_value = [
        row(),
        row(reason="回购"),
        row("000001.SZ"),
        row("920001"),
        row("600001"),
    ]
    result = env.client.post(URL, json={"date": DAY})
    assert result.status_code == 200
    assert result.json() == {
        "state": "synced",
        "date": DAY,
        "raw_rows": 5,
        "stocks": 3,
        "table_id": service.TABLE_ID,
    }
    frame = pl.read_parquet(env.partition)
    assert set(frame["symbol"]) == {"000001.SZ", "920001.BJ", "600001.SH"}
    stock = frame.filter(pl.col("symbol") == "000001.SZ").row(0, named=True)
    assert stock["reason"].count("资产重组") == 1 and "回购" in stock["reason"]
    assert stock["source"] == "zzshare" and stock["trade_date"] == DAY
    assert stock["fetched_at"].endswith("+08:00") and stock["source_rows"] == 3
    assert ExtConfigStore(env.repo.store.data_dir).get(service.TABLE_ID).mode == "timeseries"
    env.repo.clear_cache.assert_called_once()
    first = env.partition.read_bytes()
    assert env.client.post(URL, json={"date": DAY}).json() == {**result.json(), "state": "cached"}
    env.provider.get_limit_reasons.assert_called_once_with(DAY)
    assert env.partition.read_bytes() == first


@pytest.mark.parametrize(
    "day", ["2026-02-30", "20261021", "2026-10-22", "2026-10-23", "../2026-10-21"]
)
def test_refuses_bad_dates_and_intraday_snapshots_without_request(env, day):
    assert env.client.post(URL, json={"date": day}).status_code in {400, 422}
    env.provider.get_limit_reasons.assert_not_called()
    assert not env.root.exists()


@pytest.mark.parametrize(
    "invalid", [row(day="2026-10-20"), row("junk"), row("000001.SH"), row(reason=" "), None]
)
def test_bad_rows_reject_the_entire_batch(env, invalid):
    env.provider.get_limit_reasons.return_value = [row(), invalid]
    assert env.client.post(URL, json={"date": DAY}).status_code == 502
    assert not env.root.exists()


def test_empty_and_upstream_failure_can_be_retried(env):
    env.provider.get_limit_reasons.side_effect = [[], RuntimeError("private detail"), [row()]]
    assert env.client.post(URL, json={"date": DAY}).json()["state"] == "no_data"
    error = env.client.post(URL, json={"date": DAY})
    assert error.status_code == 502 and "private" not in error.text
    assert not env.root.exists()
    assert env.client.post(URL, json={"date": DAY}).json()["stocks"] == 1


def test_source_without_research_method_is_unavailable(env, monkeypatch):
    monkeypatch.setattr(service.custom_sources, "get_provider", lambda _: SimpleNamespace())
    assert env.client.post(URL, json={"date": DAY}).status_code == 503
    assert not env.root.exists()


def test_user_table_is_untouched_and_unreadable_existing_day_is_preserved(env):
    env.provider.get_limit_reasons.return_value = [row()]
    env.root.mkdir(parents=True)
    config_path = env.root / "config.json"
    config_path.write_text("{}", encoding="utf8")
    assert env.client.post(URL, json={"date": DAY}).status_code == 409
    assert config_path.read_text(encoding="utf8") == "{}"
    env.provider.get_limit_reasons.assert_not_called()
    config_path.unlink()
    env.root.rmdir()
    assert env.client.post(URL, json={"date": DAY}).status_code == 200
    env.partition.write_bytes(b"corrupt file")
    assert env.client.post(URL, json={"date": DAY}).status_code == 409
    assert env.partition.read_bytes() == b"corrupt file"


def test_dates_stay_separate_and_only_the_effective_date_is_visible(env):
    earlier = "2026-10-20"
    env.provider.get_limit_reasons.side_effect = [
        [row(reason="复牌", day=earlier)],
        [row(reason="资产重组")],
    ]
    assert env.client.post(URL, json={"date": earlier}).json()["state"] == "synced"
    assert env.client.post(URL, json={"date": DAY}).json()["state"] == "synced"
    old = env.root / "timeseries" / f"date={earlier}" / "part.parquet"
    assert pl.read_parquet(old)["reason"].to_list() == ["复牌"]
    assert pl.read_parquet(env.partition)["reason"].to_list() == ["资产重组"]
    rows = env.client.get(f"/api/ext-data/{service.TABLE_ID}/rows", params={"date": earlier})
    assert rows.status_code == 200 and rows.json()["rows"][0]["reason"] == "复牌"


def test_normal_sync_registers_queryable_ext_view(env):
    with duckdb.connect(":memory:") as db:
        env.repo.store.db = db
        env.provider.get_limit_reasons.return_value = [row()]
        assert env.client.post(URL, json={"date": DAY}).json()["state"] == "synced"
        assert db.execute("SELECT symbol, reason FROM ext_zzshare_limit_reasons").fetchall() == [
            ("000001.SZ", "资产重组")
        ]


def test_unavailable_provider_fails_without_writing(env, monkeypatch):
    monkeypatch.setattr(service.custom_sources, "get_provider", Mock(side_effect=ValueError()))
    assert env.client.post(URL, json={"date": DAY}).status_code == 503
    assert not env.root.exists()


def test_concurrent_retries_publish_only_one_partition(env):
    both_fetching = Barrier(2)

    def fetch(_day):
        both_fetching.wait(timeout=5)
        return [row()]

    env.provider.get_limit_reasons.side_effect = fetch
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(
                service.sync_limit_reasons, env.repo.store.data_dir, date.fromisoformat(DAY)
            )
            for _ in range(2)
        ]
        assert sorted(future.result(timeout=10)["state"] for future in futures) == [
            "cached",
            "synced",
        ]
    assert pl.read_parquet(env.partition)["source_rows"].to_list() == [1]
