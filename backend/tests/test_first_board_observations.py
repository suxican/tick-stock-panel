import gzip
import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path, PureWindowsPath

import pytest

from app.market_time import CN_TZ
from app.services import first_board_observations as observations

NOW = datetime(2026, 10, 21, 10, 0, 17, tzinfo=CN_TZ)
VERSION = "a" * 64
GROUPS = [{
    "id": "concept:sample", "kind": "concept", "name": "测试概念",
    "source_id": "provider-a", "field": "concept", "members": ["600000.SH", "000001.SZ"],
}]


def row(symbol="600000.SH", pattern="platform", *, strong=None, can_buy=False):
    return {
        "symbol": symbol, "name": "测试股", "pattern": pattern, "state": "watch",
        "can_buy": can_buy, "price": 10.0, "change_pct": -.01,
        "attention_context": {"strong_sector": strong, "leader_follow": None,
                              "groups": [{"id": "concept:sample", "coverage": .8}]},
        "reasons": ["涨幅未达到实验门槛"],
    }


def record(store, rows=None, *, now=NOW, revision=1, version=None):
    return store.record(
        [row()] if rows is None else rows, now=now, rule_revision=revision,
        membership_version=version,
        metadata={"rule_version": "initial", "minute_price_basis": "raw", "coverage": None},
    )


def read(path):
    return json.loads(gzip.decompress(path.read_bytes()))


def test_sample_keeps_negative_unknown_and_deduplicates_symbols(tmp_path):
    store = observations.FirstBoardObservationStore(tmp_path)
    store.write_membership(GROUPS, VERSION, NOW - timedelta(minutes=1))
    rows = [row(strong=False), row(pattern="oversold", strong=False, can_buy=True),
            row("000001.SZ", strong=None, can_buy=None)]
    before = json.loads(json.dumps(rows))
    result = record(store, rows, version=VERSION)
    assert result == {"status": "recorded", "last_sample_at": NOW.isoformat(), "sample_count": 1}
    sample = read(tmp_path / "2026-10-21" / "1" / "1000.json.gz")
    assert sample["observed_at"] == NOW.isoformat()
    assert sample["anchor"] == NOW.replace(second=0).isoformat()
    assert sample["candidate_count"] == 3
    assert sample["symbol_count"] == 2
    indexed = {item["symbol"]: item for item in sample["rows"]}
    first = indexed["600000.SH"]
    assert first["patterns"] == ["platform", "oversold"]
    assert first["states"] == ["watch"]
    assert first["can_buy"] is True
    assert first["attention_context"]["strong_sector"] is False
    assert first["candidates"] == rows[:2]
    assert indexed["000001.SZ"]["attention_context"]["strong_sector"] is None
    assert indexed["000001.SZ"]["can_buy"] is None
    assert sample["metadata"]["coverage"] is None
    assert rows == before


def test_restart_and_same_anchor_cannot_rewrite_observations(tmp_path):
    first = observations.FirstBoardObservationStore(tmp_path)
    record(first)
    path = tmp_path / "2026-10-21" / "1" / "1000.json.gz"
    original = path.read_bytes()
    reopened = observations.FirstBoardObservationStore(tmp_path)
    result = record(reopened, [row(strong=True)], now=NOW.replace(second=59))
    assert result == {"status": "already_recorded", "last_sample_at": NOW.isoformat(), "sample_count": 1}
    assert path.read_bytes() == original
    assert record(reopened, now=NOW + timedelta(minutes=5))["sample_count"] == 2


@pytest.mark.parametrize("hour,minute", [(9, 25), (9, 31), (11, 30), (12, 0), (14, 59), (15, 0)])
def test_only_current_five_minute_window_is_sampled_without_backfill(tmp_path, hour, minute):
    result = record(observations.FirstBoardObservationStore(tmp_path), now=NOW.replace(hour=hour, minute=minute))
    assert result == {"status": "not_due", "last_sample_at": None, "sample_count": 0}
    assert not list(tmp_path.rglob("*.gz"))


def test_revision_day_and_beijing_clock_have_independent_slots(tmp_path):
    store = observations.FirstBoardObservationStore(tmp_path)
    record(store, now=NOW.astimezone(UTC))
    assert record(store, now=NOW.replace(tzinfo=None))["status"] == "already_recorded"
    assert record(store, revision=2)["sample_count"] == 1
    assert record(store, now=NOW + timedelta(days=1))["sample_count"] == 1
    assert record(store, now=NOW + timedelta(days=3))["status"] == "not_due"
    assert len(list(tmp_path.rglob("*.gz"))) == 3


def test_empty_pool_is_a_real_zero_denominator_sample(tmp_path):
    assert record(observations.FirstBoardObservationStore(tmp_path), [])["status"] == "recorded"
    sample = read(tmp_path / "2026-10-21" / "1" / "1000.json.gz")
    assert sample["candidate_count"] == sample["symbol_count"] == 0
    assert sample["rows"] == []


def test_membership_is_immutable_and_complete(tmp_path):
    store = observations.FirstBoardObservationStore(tmp_path)
    store.write_membership(GROUPS, VERSION, NOW)
    path = tmp_path / "memberships" / f"{VERSION}.json.gz"
    initial = path.read_bytes()
    store.write_membership(GROUPS, VERSION, NOW + timedelta(minutes=1))
    assert path.read_bytes() == initial
    assert read(path)["groups"] == GROUPS
    changed = [{**GROUPS[0], "members": ["600000.SH"]}]
    with pytest.raises(ValueError, match="成分快照版本冲突"):
        store.write_membership(changed, VERSION, NOW)
    assert path.read_bytes() == initial


@pytest.mark.parametrize("version", ["../outside", "A" * 64, "", "a/b", "a" * 129])
def test_membership_version_cannot_escape_directory(tmp_path, version):
    store = observations.FirstBoardObservationStore(tmp_path)
    with pytest.raises(ValueError, match="成分版本"):
        store.write_membership(GROUPS, version, NOW)
    with pytest.raises(ValueError, match="成分版本"):
        record(store, version=version)


@pytest.mark.parametrize("revision", [-1, "../escape", 1.5, True])
def test_revision_must_be_a_nonnegative_integer(tmp_path, revision):
    with pytest.raises(ValueError, match="规则版本"):
        record(observations.FirstBoardObservationStore(tmp_path), revision=revision)


def test_missing_or_future_membership_cannot_be_used(tmp_path):
    store = observations.FirstBoardObservationStore(tmp_path)
    with pytest.raises(ValueError, match="成分快照不存在"):
        record(store, version=VERSION)
    store.write_membership(GROUPS, VERSION, NOW + timedelta(seconds=1))
    with pytest.raises(ValueError, match="晚于采样"):
        record(store, version=VERSION)
    assert not (tmp_path / "2026-10-21").exists()


@pytest.mark.parametrize("kind", ["membership", "sample"])
def test_corrupt_existing_file_is_reported_and_preserved(tmp_path, kind):
    store = observations.FirstBoardObservationStore(tmp_path)
    if kind == "membership":
        store.write_membership(GROUPS, VERSION, NOW)
        path = tmp_path / "memberships" / f"{VERSION}.json.gz"
    else:
        record(store)
        path = tmp_path / "2026-10-21" / "1" / "1000.json.gz"
    path.write_bytes(b"broken gzip")
    reopened = observations.FirstBoardObservationStore(tmp_path)
    with pytest.raises(ValueError, match="损坏"):
        if kind == "membership":
            reopened.write_membership(GROUPS, VERSION, NOW)
        else:
            record(reopened)
    assert path.read_bytes() == b"broken gzip"


def test_valid_gzip_with_invalid_sample_is_not_accepted(tmp_path):
    store = observations.FirstBoardObservationStore(tmp_path)
    record(store)
    path = tmp_path / "2026-10-21" / "1" / "1000.json.gz"
    path.write_bytes(gzip.compress(b'{}'))
    with pytest.raises(ValueError, match="损坏"):
        record(store)


def test_existing_sample_does_not_hide_corrupt_membership(tmp_path):
    store = observations.FirstBoardObservationStore(tmp_path)
    store.write_membership(GROUPS, VERSION, NOW)
    record(store, version=VERSION)
    member_path = tmp_path / "memberships" / f"{VERSION}.json.gz"
    member_path.write_bytes(b"corrupt membership")
    with pytest.raises(ValueError, match="损坏"):
        record(store, version=VERSION)
    assert member_path.read_bytes() == b"corrupt membership"


def test_earlier_sample_stays_immutable_when_membership_changes_in_same_window(tmp_path):
    store = observations.FirstBoardObservationStore(tmp_path)
    store.write_membership(GROUPS, VERSION, NOW)
    record(store, version=VERSION)
    later_version = "b" * 64
    later_groups = [{**GROUPS[0], "members": ["000001.SZ"]}]
    store.write_membership(later_groups, later_version, NOW + timedelta(seconds=5))
    result = record(store, now=NOW + timedelta(seconds=10), version=later_version)
    assert result["status"] == "already_recorded"
    assert read(tmp_path / "2026-10-21" / "1" / "1000.json.gz")["membership_version"] == VERSION
    assert record(store, now=NOW + timedelta(minutes=5), version=later_version)["sample_count"] == 2


def test_disagreeing_contexts_are_kept_per_pattern_without_inventing_a_summary(tmp_path):
    store = observations.FirstBoardObservationStore(tmp_path)
    rows = [row(strong=True), row(pattern="oversold", strong=None)]
    record(store, rows)
    sample_row = read(tmp_path / "2026-10-21" / "1" / "1000.json.gz")["rows"][0]
    assert sample_row["attention_context"] is None
    assert sample_row["candidates"] == rows


def test_failed_publication_does_not_advance_status_and_can_retry(tmp_path, monkeypatch):
    store = observations.FirstBoardObservationStore(tmp_path)
    real_link = observations.os.link

    def fail(*args):
        raise OSError("disk full")

    monkeypatch.setattr(observations.os, "link", fail)
    with pytest.raises(OSError, match="disk full"):
        record(store)
    assert not list(tmp_path.rglob("*.gz"))
    assert not list(tmp_path.rglob("*.tmp"))
    monkeypatch.setattr(observations.os, "link", real_link)
    assert record(store)["sample_count"] == 1


def test_multiple_store_instances_publish_only_one_complete_sample(tmp_path):
    def publish(index):
        return record(observations.FirstBoardObservationStore(tmp_path), [row(strong=bool(index % 2))])

    with ThreadPoolExecutor(max_workers=6) as executor:
        results = list(executor.map(publish, range(12)))
    assert sum(result["status"] == "recorded" for result in results) == 1
    assert all(result["sample_count"] == 1 for result in results)
    assert len(list(tmp_path.rglob("*.gz"))) == 1
    assert len(read(tmp_path / "2026-10-21" / "1" / "1000.json.gz")["rows"]) == 1


@pytest.mark.skipif(os.name != "nt", reason="Windows extended path namespace")
def test_windows_extended_parent_resolution_still_publishes_inside_root(tmp_path, monkeypatch):
    store = observations.FirstBoardObservationStore(tmp_path)
    original_resolve = Path.resolve

    def extended_parent(path, *args, **kwargs):
        result = original_resolve(path, *args, **kwargs)
        # Python 3.12 can retain this prefix when a concurrent mkdir changes
        # the error from its second GetFinalPathNameByHandle lookup.
        if path.name == "1" and not str(result).startswith("\\\\?\\"):
            return Path("\\\\?\\" + str(result))
        return result

    monkeypatch.setattr(Path, "resolve", extended_parent)
    result = record(store)
    assert result["status"] == "recorded"
    assert read(tmp_path / "2026-10-21" / "1" / "1000.json.gz")["symbol_count"] == 1


@pytest.mark.parametrize("root,parent,inside", [
    (r"D:\root", r"\\?\D:\root\day\1", True),
    (r"\\?\D:\root", r"D:\root\day\1", True),
    (r"\\server\share\root", r"\\?\UNC\server\share\root\day", True),
    (r"\\?\UNC\server\share\root", r"\\server\share\root\day", True),
    (r"D:\root", r"\\?\D:\root-elsewhere\day", False),
    (r"D:\root", r"\\?\D:\root.\day", False),
    (r"\\server\share\root", r"\\?\UNC\server\other\root\day", False),
])
def test_windows_path_identity_keeps_real_directory_boundaries(root, parent, inside):
    root_identity = observations._path_identity(PureWindowsPath(root))
    parent_identity = observations._path_identity(PureWindowsPath(parent))
    assert parent_identity.is_relative_to(root_identity) is inside


def test_resolved_parent_outside_root_is_rejected_with_distinct_reason(tmp_path, monkeypatch):
    store = observations.FirstBoardObservationStore(tmp_path / "root")
    original_resolve = Path.resolve

    def escaped_parent(path, *args, **kwargs):
        if path.name == "1":
            return tmp_path / "outside"
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", escaped_parent)
    with pytest.raises(ValueError, match="路径超出存储目录"):
        record(store)
    assert not list(tmp_path.rglob("*.gz"))


def test_leaf_symlink_is_rejected_even_when_its_parent_is_inside_root(tmp_path, monkeypatch):
    store = observations.FirstBoardObservationStore(tmp_path)
    original_is_symlink = Path.is_symlink

    def leaf_symlink(path):
        return path.name == "1000.json.gz" or original_is_symlink(path)

    monkeypatch.setattr(Path, "is_symlink", leaf_symlink)
    with pytest.raises(ValueError, match="路径指向符号链接"):
        record(store)
    assert not list(tmp_path.rglob("*.gz"))


def test_nonfinite_values_are_rejected_before_any_file_is_published(tmp_path):
    with pytest.raises(ValueError):
        record(observations.FirstBoardObservationStore(tmp_path), [{**row(), "price": float("nan")}])
    assert not list(tmp_path.rglob("*.gz"))


def test_symlink_directory_escape_fails_closed(tmp_path):
    root, outside = tmp_path / "root", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    try:
        (root / "memberships").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlink requires OS privilege")
    with pytest.raises(ValueError, match="目录"):
        observations.FirstBoardObservationStore(root).write_membership(GROUPS, VERSION, NOW)
    assert not list(outside.iterdir())
