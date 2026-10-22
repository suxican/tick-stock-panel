"""Market reports must not be mistaken for stock membership catalogs."""
from types import SimpleNamespace

import polars as pl
import pytest

from app.services.ext_data import ExtConfig, ExtConfigStore, ExtField
from app.services.first_board_attention_data import AttentionData
from app.services.sector_monitor import SectorMonitorService


def _source(tmp_path, identifier, *, market_level=False):
    config = ExtConfig(
        id=identifier, label=identifier, mode="snapshot", market_level=market_level,
        fields=[ExtField("symbol", "string", "代码"), ExtField("concept", "string", "所属概念")],
    )
    ExtConfigStore(tmp_path).upsert(config)
    return tmp_path / "ext_data" / identifier / "part.parquet"


def _repo(tmp_path):
    return SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        get_instruments=lambda: pl.DataFrame({"symbol": ["000001.SZ"], "name": ["普通股票"]}),
    )


@pytest.mark.parametrize("report", ["no_stock_key", "has_stock_key", "corrupt"])
def test_market_reports_do_not_block_or_pollute_stock_membership(tmp_path, report):
    members = _source(tmp_path, "members")
    pl.DataFrame({"symbol": ["000001.SZ"], "concept": ["人工智能"]}).write_parquet(members)
    market = _source(tmp_path, "market_report", market_level=True)
    if report == "corrupt":
        market.write_bytes(b"invalid market report")
    elif report == "has_stock_key":
        # Event-selected rows still cannot stand in for the full membership denominator.
        pl.DataFrame({"symbol": ["000001.SZ"], "concept": ["涨停事件概念"]}).write_parquet(market)
    else:
        pl.DataFrame({"record_id": ["event"], "concept": ["涨停事件概念"]}).write_parquet(market)

    adapter = AttentionData(_repo(tmp_path))
    adapter.prepare(universe="hs_a_non_st")
    assert adapter.reason is None
    assert adapter.version is not None
    assert [(row["source_id"], row["name"], row["members"]) for row in adapter.groups] == [
        ("members", "人工智能", ["000001.SZ"]),
    ]


def test_market_report_updates_do_not_reset_membership_cache(tmp_path):
    members = _source(tmp_path, "members")
    pl.DataFrame({"symbol": ["000001.SZ"], "concept": ["人工智能"]}).write_parquet(members)
    catalog = SectorMonitorService(_repo(tmp_path))
    expected = catalog.membership_snapshot()
    catalog._history["sentinel"] = "existing observations"

    market = _source(tmp_path, "market_report", market_level=True)
    market.write_bytes(b"irrelevant market data")
    assert catalog.membership_snapshot() == expected
    assert catalog._history["sentinel"] == "existing observations"


@pytest.mark.parametrize("problem", ["missing_key", "corrupt"])
def test_actual_membership_source_still_fails_closed(tmp_path, problem):
    members = _source(tmp_path, "members")
    if problem == "corrupt":
        members.write_bytes(b"invalid membership")
    else:
        pl.DataFrame({"concept": ["人工智能"]}).write_parquet(members)
    catalog = SectorMonitorService(_repo(tmp_path))
    with pytest.raises(RuntimeError, match="板块成员数据读取失败"):
        catalog.membership_snapshot()
