"""Local attention inputs preserve complete denominators and never fetch remotely."""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import polars as pl
import pytest

from app.market_time import CN_TZ
from app.services import first_board_attention_data as mod
from app.services.ext_data import ExtConfig, ExtConfigStore, ExtField, write_ext_parquet
from app.services.sector_monitor import SectorMonitorService

NOW = datetime(2026, 10, 21, 10, 0, tzinfo=CN_TZ)


def target(key="concept:a", *, name="人工智能", kind="concept", members=None, **kwargs):
    return {"key": key, "kind": kind, "name": name, "source_id": "source", "field": kind,
            "value": name, "level": None if kind == "concept" else 2,
            "members": ["000001", "000002.SZ"] if members is None else members, **kwargs}


@pytest.fixture()
def data(tmp_path, monkeypatch):
    instruments = pl.DataFrame({"symbol": ["000001.SZ", "000002.SZ", "300001.SZ", "600001.SH"],
                                "name": ["普通一", "普通二", "创业板", "*ST股票"]})
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path),
                           get_instruments=Mock(return_value=instruments),
                           get_minute_by_dates=Mock(return_value=pl.DataFrame({
                               "symbol": ["000001.SZ"] * 3,
                               "datetime": [NOW.replace(tzinfo=None) - timedelta(minutes=1),
                                            NOW.replace(tzinfo=None),
                                            NOW.replace(tzinfo=None) - timedelta(days=1)],
                               "close": [10., 100., 5.],
                           })))
    monkeypatch.setattr(mod, "capability_gate", lambda kind: (kind == "minute", kind))
    monkeypatch.setattr(mod, "minute_basis_is_raw", lambda path: True)
    adapter = mod.AttentionData(repo)
    monkeypatch.setattr(adapter._catalog, "membership_snapshot", lambda: [target()])
    return adapter, repo


def test_resolves_codes_keeps_missing_full_symbols_and_excludes_st_universe(data):
    adapter, _ = data
    adapter._catalog.membership_snapshot = lambda: [target(members=[
        "000001", "000001.SZ", "000002.SZ", "000099.SZ", "300001.SZ", "600001.SH", "430001.BJ",
    ])]
    adapter.prepare(universe="main_board_non_st")
    assert adapter.groups[0]["members"] == ["000001.SZ", "000002.SZ", "000099.SZ"]
    first_version = adapter.version
    assert len(first_version) == 64
    adapter.prepare(universe="hs_a_non_st")
    assert "300001.SZ" in adapter.groups[0]["members"]
    assert adapter.version != first_version


def test_unknown_or_ambiguous_bare_code_discards_whole_group(data):
    adapter, repo = data
    adapter._catalog.membership_snapshot = lambda: [target(members=["000001", "123456"])]
    adapter.prepare(universe="hs_a_non_st")
    assert adapter.groups == [] and "无法唯一映射" in adapter.reason
    repo.get_instruments.return_value = pl.DataFrame({"symbol": ["000001.SZ", "000001.SH"],
                                                     "name": ["甲", "乙"]})
    adapter._catalog.membership_snapshot = lambda: [target(members=["000001"])]
    adapter.prepare(universe="hs_a_non_st")
    assert adapter.groups == []


def test_industry_uses_level_two_or_standalone_level_one_and_keeps_sources(data):
    adapter, _ = data
    targets = [target("a", kind="industry", name="科技", value="科技", level=1),
               target("b", kind="industry", name="科技 / 软件", value="科技-软件", level=2),
               target("c", kind="industry", name="科技 / 软件 / 应用", value="科技-软件-应用", level=3),
               target("d", kind="industry", name="银行", value="银行", level=1),
               target("e", source_id="second")]
    adapter._catalog.membership_snapshot = lambda: targets
    adapter.prepare(universe="hs_a_non_st")
    assert {group["id"] for group in adapter.groups} == {"b", "d", "e"}
    assert next(group for group in adapter.groups if group["id"] == "e")["source_id"] == "second"


def test_attribute_concepts_and_broad_groups_are_excluded(data):
    adapter, _ = data
    adapter._catalog.membership_snapshot = lambda: [
        target("attr", name="融资融券"), target("broad", members=[f"{i:06d}.SZ" for i in range(301)]),
        target("normal"),
    ]
    adapter.prepare(universe="hs_a_non_st")
    assert [group["id"] for group in adapter.groups] == ["normal"]


def test_membership_hash_is_order_independent_and_output_is_detached(data):
    adapter, _ = data
    adapter.prepare(universe="hs_a_non_st")
    version = adapter.version
    returned = adapter.groups
    returned[0]["members"].clear()
    assert len(adapter.groups[0]["members"]) == 2
    adapter._catalog.membership_snapshot = lambda: [target(members=["000002.SZ", "000001"])]
    adapter.prepare(universe="hs_a_non_st")
    assert adapter.version == version
    adapter._catalog.membership_snapshot = lambda: [target(source_id="another")]
    adapter.prepare(universe="hs_a_non_st")
    assert adapter.version != version


def test_prepare_failure_clears_previous_membership(data):
    adapter, repo = data
    adapter.prepare(universe="hs_a_non_st")
    repo.get_instruments.side_effect = RuntimeError("private filesystem detail")
    adapter.prepare(universe="hs_a_non_st")
    assert adapter.groups == [] and adapter.version is None
    assert adapter.reason and "private" not in adapter.reason


def test_minutes_batch_exact_today_and_cutoff_cached_then_next_minute_refresh(data):
    adapter, repo = data
    first, raw, reason = adapter.minutes({"000001.SZ", "000002.SZ"}, now=NOW)
    assert raw and reason is None and first["close"].to_list() == [10.]
    repo.get_minute_by_dates.assert_called_once_with(
        ["000001.SZ", "000002.SZ"], [NOW.date()], asset_type="stock")
    cached, _, _ = adapter.minutes({"000002.SZ", "000001.SZ"}, now=NOW + timedelta(seconds=40))
    assert cached is first  # immutable batch identity is the downstream feature cache key
    assert repo.get_minute_by_dates.call_count == 1
    adapter.minutes({"000001.SZ", "000002.SZ"}, now=NOW + timedelta(minutes=1))
    assert repo.get_minute_by_dates.call_count == 2


def test_changed_symbols_membership_and_partition_batch_invalidate_cache(data, tmp_path):
    adapter, repo = data
    adapter.prepare(universe="hs_a_non_st")
    adapter.minutes({"000001.SZ"}, now=NOW)
    adapter.minutes({"000002.SZ"}, now=NOW)
    adapter._catalog.membership_snapshot = lambda: [target(members=["000001.SZ"])]
    adapter.prepare(universe="hs_a_non_st")
    adapter.minutes({"000002.SZ"}, now=NOW)
    partition = tmp_path / "kline_minute" / f"date={NOW.date()}" / "part.parquet"
    partition.parent.mkdir(parents=True)
    partition.write_bytes(b"new generation")
    adapter.minutes({"000002.SZ"}, now=NOW)
    assert repo.get_minute_by_dates.call_count == 4


@pytest.mark.parametrize("blocked", ["basis", "capability", "exception"])
def test_minutes_fail_closed_without_read_or_remote_work(data, monkeypatch, blocked):
    adapter, repo = data
    if blocked == "basis":
        monkeypatch.setattr(mod, "minute_basis_is_raw", lambda path: False)
    elif blocked == "capability":
        monkeypatch.setattr(mod, "capability_gate", lambda kind: (False, "no permission"))
    else:
        repo.get_minute_by_dates.side_effect = OSError("private path")
    frame, raw, reason = adapter.minutes({"000001.SZ"}, now=NOW)
    assert frame.is_empty() and reason and "private" not in reason
    if blocked != "exception":
        repo.get_minute_by_dates.assert_not_called()
    if blocked == "basis":
        assert raw is False


def test_full_minute_only_capability_and_beijing_timezone_are_supported(data, monkeypatch):
    adapter, repo = data
    monkeypatch.setattr(mod, "capability_gate", lambda kind: (kind == "full_minute", kind))
    utc = NOW.astimezone(UTC)
    frame, raw, reason = adapter.minutes({"000001.SZ"}, now=utc)
    assert raw and reason is None and frame.height == 1
    assert repo.get_minute_by_dates.call_args.args[1] == [NOW.date()]


def test_sector_membership_snapshot_is_detached_and_excludes_indices(tmp_path):
    config = ExtConfig(id="members", label="成员", mode="snapshot", fields=[
        ExtField("symbol", "string", "代码"), ExtField("concept", "string", "概念"),
    ])
    ExtConfigStore(tmp_path).upsert(config)
    write_ext_parquet(pl.DataFrame({"symbol": ["000001.SZ"], "concept": ["科技"]}), config, tmp_path)
    catalog = SectorMonitorService(SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)))
    snapshot = catalog.membership_snapshot()
    assert len(snapshot) == 1 and snapshot[0]["kind"] == "concept"
    snapshot[0]["members"].clear()
    snapshot[0]["name"] = "changed"
    again = catalog.membership_snapshot()
    assert again[0]["name"] == "科技" and again[0]["members"] == ["000001.SZ"]


def test_corrupt_source_does_not_publish_incomplete_rank_universe(tmp_path, monkeypatch):
    config = ExtConfig(id="members", label="成员", mode="snapshot", fields=[
        ExtField("symbol", "string", "代码"), ExtField("concept", "string", "概念"),
    ])
    ExtConfigStore(tmp_path).upsert(config)
    write_ext_parquet(pl.DataFrame({"symbol": ["000001.SZ"], "concept": ["科技"]}), config, tmp_path)
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path), get_instruments=lambda: pl.DataFrame(
        {"symbol": ["000001.SZ"], "name": ["普通股票"]}))
    adapter = mod.AttentionData(repo)
    adapter.prepare(universe="hs_a_non_st")
    assert len(adapter.groups) == 1
    original = pl.read_parquet
    monkeypatch.setattr(pl, "read_parquet", Mock(side_effect=OSError("temporarily unreadable")))
    # Force a normal catalog refresh without mutating existing user data.
    adapter._catalog._catalog_signature = None
    adapter.prepare(universe="hs_a_non_st")
    assert adapter.groups == [] and adapter.version is None
    monkeypatch.setattr(pl, "read_parquet", original)
    adapter.prepare(universe="hs_a_non_st")
    assert len(adapter.groups) == 1


def test_corrupt_non_dimension_source_does_not_disable_membership(tmp_path):
    members = ExtConfig(id="members", label="成员", mode="snapshot", fields=[
        ExtField("symbol", "string", "代码"), ExtField("concept", "string", "概念"),
    ])
    financial = ExtConfig(id="finance", label="财务", mode="snapshot", fields=[
        ExtField("symbol", "string", "代码"), ExtField("profit", "float", "利润"),
    ])
    for config in (members, financial):
        ExtConfigStore(tmp_path).upsert(config)
    write_ext_parquet(pl.DataFrame({"symbol": ["000001.SZ"], "concept": ["科技"]}), members, tmp_path)
    (tmp_path / "ext_data" / "finance" / "part.parquet").write_bytes(b"invalid parquet")
    catalog = SectorMonitorService(SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)))
    snapshot = catalog.membership_snapshot()
    assert len(snapshot) == 1 and snapshot[0]["name"] == "科技"


@pytest.mark.parametrize("missing", ["symbol", "concept"])
def test_dimension_source_missing_required_column_is_not_complete(tmp_path, missing):
    config = ExtConfig(id="members", label="成员", mode="snapshot", fields=[
        ExtField("symbol", "string", "代码"), ExtField("concept", "string", "概念"),
    ])
    ExtConfigStore(tmp_path).upsert(config)
    frame = pl.DataFrame({"symbol": ["000001.SZ"], "concept": ["科技"]}).drop(missing)
    frame.write_parquet(tmp_path / "ext_data" / "members" / "part.parquet")
    catalog = SectorMonitorService(SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)))
    with pytest.raises(RuntimeError, match="板块成员数据读取失败"):
        catalog.membership_snapshot()
