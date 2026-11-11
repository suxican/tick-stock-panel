"""主线单一口径: 盘中补算与盘后回填同公式、表值优先、涨幅榜过滤与主线标注。"""
from __future__ import annotations

import types
from datetime import date

import polars as pl
import pytest

from app.services import market_mainline, preferences
from app.services.market_overview_builder import _rank_groups

D1, D2 = date(2026, 11, 10), date(2026, 11, 11)
FILTER = {"min_members": 4, "max_members": 600, "blacklist": []}
MAPPING = {
    "S1.SH": ["X"], "S2.SH": ["X"], "S3.SH": ["X"], "S4.SH": ["X", "Y"],
    "T1.SZ": ["Y"], "T2.SZ": ["Y"], "T3.SZ": ["Y"], "T4.SZ": ["Y"],
}
# D2: X 三家涨停(3,2,1板), Y 四家涨停(2,1,1,1板)
D2_LIMIT = [("S1.SH", 3, 6e8), ("S2.SH", 2, 2e8), ("S4.SH", 1, 4e8),
            ("T1.SZ", 2, 1e8), ("T2.SZ", 1, 1e8), ("T3.SZ", 1, 1e8), ("T4.SZ", 1, 1e8)]


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    path = tmp_path / "preferences.json"
    monkeypatch.setattr(preferences, "_path", lambda: path)
    preferences._invalidate_cache()
    monkeypatch.setattr(market_mainline, "_ST_SYMBOLS_CACHE", None)
    map_df = pl.DataFrame(
        {"_sym_up": [s for s, ms in MAPPING.items() for _ in ms],
         "concept": [m for _, ms in MAPPING.items() for m in ms]},
        schema={"_sym_up": pl.Utf8, "concept": pl.Utf8},
    )
    monkeypatch.setattr(market_mainline, "_load_concept_map_df",
                        lambda repo, k="concept": (map_df, 2) if k == "concept" else (pl.DataFrame(), 0))
    yield
    preferences._invalidate_cache()


def _repo(tmp_path):
    return types.SimpleNamespace(store=types.SimpleNamespace(data_dir=tmp_path))


def _quote_rows(spec):
    rows = [{"symbol": s, "name": s, "consecutive_limit_ups": c, "amount": a, "change_pct": 0.1} for s, c, a in spec]
    rows.append({"symbol": "S3.SH", "name": "S3", "consecutive_limit_ups": 0, "amount": 1e8, "change_pct": 0.01})
    return rows


def _write_enriched(root, d, spec):
    part = root / "kline_daily_enriched" / f"date={d.isoformat()}" / "part.parquet"
    part.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame([{"date": d, "symbol": s, "consecutive_limit_ups": c, "amount": a} for s, c, a in spec]).write_parquet(part)


def test_live_mainline_matches_after_close_backfill(tmp_path):
    """同一天: 盘中按行情行补算 == 盘后 compute_mainline_range 回填(同公式)。"""
    _write_enriched(tmp_path, D2, D2_LIMIT)
    backfill = market_mainline.compute_mainline_range(_repo(tmp_path), tmp_path, D2, D2, kind="concept", filter_cfg=FILTER)
    live, source = market_mainline.mainline_for_day(_repo(tmp_path), D2, _quote_rows(D2_LIMIT))

    assert source == "live"
    cols = ["member", "rank", "limit_up_count", "ge2_count", "max_boards", "leader_symbol"]
    assert [{k: r[k] for k in cols} for r in live] == backfill.sort("rank").select(cols).to_dicts()
    # X 高度/梯队占优(权重 0.65) > Y 涨停家数占优(0.35)
    assert [r["member"] for r in live] == ["X", "Y"]


def test_history_row_wins_over_live(tmp_path):
    p = market_mainline.mainline_path(tmp_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame([
        {"date": D2, "kind": "concept", "member": "历史主线", "rank": 1, "score": 90.0,
         "limit_up_count": 9, "ge2_count": 3, "max_boards": 5, "leader_symbol": "S1.SH"},
        {"date": D1, "kind": "concept", "member": "昨日主线", "rank": 1, "score": 80.0,
         "limit_up_count": 5, "ge2_count": 1, "max_boards": 2, "leader_symbol": None},
    ]).write_parquet(p)

    rows, source = market_mainline.mainline_for_day(_repo(tmp_path), D2, _quote_rows(D2_LIMIT))
    assert source == "history"
    assert [r["member"] for r in rows] == ["历史主线"]


def test_no_limit_up_rows_returns_empty(tmp_path):
    assert market_mainline.mainline_for_day(_repo(tmp_path), D2, []) == ([], None)


def test_sector_rank_applies_mainline_member_filter():
    items = [
        {"name": "小概念", "count": 2, "avg_pct": 0.09},
        {"name": "融资融券", "count": 3000, "avg_pct": 0.05},
        {"name": "黑名单", "count": 50, "avg_pct": 0.04},
        {"name": "CPO", "count": 40, "avg_pct": 0.03},
    ]
    ranked = _rank_groups(items, member_filter={"min_members": 4, "max_members": 600, "blacklist": ["黑名单"]})
    assert [it["name"] for it in ranked["leading"]] == ["CPO"]
    # 不传过滤时保持原行为
    assert _rank_groups(items)["leading"][0]["name"] == "小概念"


def test_recap_sector_block_tags_mainline():
    from app.services.market_recap import _build_sector_block

    block = _build_sector_block(
        {"leading": [{"name": "X", "avg_pct": 0.03, "leader": {"name": "龙"}},
                     {"name": "CPO", "avg_pct": 0.04, "leader": {"name": "光"}}], "lagging": []},
        "概念", {"X"},
    )
    assert "X【主线】(+3.00%" in block
    assert "CPO(+4.00%" in block
