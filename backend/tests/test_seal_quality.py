"""封板质量 (情绪周期新维度): 涨停池归档 + 日线收盘涨停复核 → 每日指标。"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import polars as pl
import pytest

from app.data_providers.schemas import LIMIT_EVENT_SCHEMA, ST_STATUS_SCHEMA
from app.services import market_reference
from app.services import seal_quality as sq

D1 = date(2020, 3, 2)
D0 = date(2019, 12, 2)   # 开板次数字段缺失期


def _enriched(data_dir: Path, day: date, sealed: dict[str, int], others: tuple[str, ...] = ("600000.SH",)):
    rows = [{"symbol": s, "date": day, "consecutive_limit_ups": n} for s, n in sealed.items()]
    rows += [{"symbol": s, "date": day, "consecutive_limit_ups": 0} for s in others]
    out = data_dir / "kline_daily_enriched" / f"date={day.isoformat()}" / "part.parquet"
    out.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(rows, schema={"symbol": pl.String, "date": pl.Date, "consecutive_limit_ups": pl.Int64}).write_parquet(out)


def _pool(data_dir: Path, day: date, rows: list[dict]):
    full = []
    for r in rows:
        base = {k: None for k in LIMIT_EVENT_SCHEMA}
        base.update({"date": day, "name": r["symbol"], "is_break": r["type"].endswith("b"), "source": "meoz"})
        base.update(r)
        full.append(base)
    out = market_reference.path_of(data_dir, "limit_events", day)
    out.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(full, schema=LIMIT_EVENT_SCHEMA).write_parquet(out)


def _st(data_dir: Path, day: date, symbols: list[str]):
    out = market_reference.path_of(data_dir, "st_status", day)
    out.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame([{"date": day, "symbol": s, "name": "ST", "st_type": "ST", "source": "meoz"} for s in symbols],
                 schema=ST_STATUS_SCHEMA).write_parquet(out)


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    market_reference.enable_reader(tmp_path)
    monkeypatch.setattr(sq, "_current_st_symbols", lambda data_dir: frozenset())
    sq.clear_cache()
    yield tmp_path
    market_reference.enable_reader(None)
    sq.clear_cache()


def _u(symbol, first="09:45:00", opens=0, fd=1e8, typ="u", name=None, amount=1e9):
    return {"symbol": symbol, "type": typ, "first_time": first, "last_time": first, "open_times": opens,
            "fd_amount": fd, "name": name or f"股票{symbol}", "amount": amount}


def _row(df: pl.DataFrame, day: date) -> dict:
    return df.filter(pl.col("date") == day).to_dicts()[0]


def test_tail_reseal_counts_as_sealed_and_break_rate(data_dir):
    _enriched(data_dir, D1, {"A.SZ": 1, "B.SZ": 2, "C.SZ": 1})
    _pool(data_dir, D1, [_u("A.SZ"), _u("B.SZ"), _u("C.SZ", typ="ub"),      # C: 猫爪标炸板但收盘涨停
                         _u("D.SZ", typ="ub")])                               # D: 真炸板
    row = _row(sq.compute_seal_quality(data_dir, exclude_st=False), D1)
    assert (row["touched"], row["sealed"]) == (4, 3)
    assert row["break_rate"] == pytest.approx(0.25)


def test_sealed_but_missing_from_pool_counts_without_detail(data_dir):
    _enriched(data_dir, D1, {"A.SZ": 1, "E.SZ": 1})
    _pool(data_dir, D1, [_u("A.SZ", first="09:30:00", opens=1, fd=2e8)])
    row = _row(sq.compute_seal_quality(data_dir, exclude_st=False), D1)
    assert (row["touched"], row["sealed"]) == (2, 2)
    assert row["pool_coverage"] == pytest.approx(0.5)
    assert row["reopen_rate"] == pytest.approx(1.0)       # 分母只含有涨停池记录的 A
    assert row["oneword_share"] == pytest.approx(1.0)
    assert row["fd_median"] == pytest.approx(2e8)


def test_time_buckets(data_dir):
    _enriched(data_dir, D1, {s: 1 for s in "ABCD"})
    _pool(data_dir, D1, [_u("A", first="09:25:00"), _u("B", first="09:59:59"),
                         _u("C", first="13:10:00"), _u("D", first="14:30:00")])
    row = _row(sq.compute_seal_quality(data_dir, exclude_st=False), D1)
    assert row["oneword_share"] == pytest.approx(0.25)
    assert row["early_share"] == pytest.approx(0.5)
    assert row["late_share"] == pytest.approx(0.25)


def test_reopen_rate_null_before_2020(data_dir):
    _enriched(data_dir, D0, {"A.SZ": 1})
    _pool(data_dir, D0, [_u("A.SZ", opens=0)])
    row = _row(sq.compute_seal_quality(data_dir, exclude_st=False), D0)
    assert row["reopen_rate"] is None
    assert row["early_share"] == pytest.approx(1.0)


def test_st_excluded_by_daily_list(data_dir):
    _enriched(data_dir, D1, {"A.SZ": 1, "S.SZ": 1})
    _pool(data_dir, D1, [_u("A.SZ"), _u("S.SZ", typ="ub")])
    _st(data_dir, D1, ["S.SZ"])
    row = _row(sq.compute_seal_quality(data_dir, exclude_st=True), D1)
    assert (row["touched"], row["sealed"]) == (1, 1)


def test_pool_name_st_excluded_without_daily_list(data_dir):
    # 2021 年前没有按日 ST 名单: 用涨停池当时的股票名 (含 ST) 剔除
    _enriched(data_dir, D0, {"A.SZ": 1})
    _pool(data_dir, D0, [_u("A.SZ"), _u("S.SZ", name="*ST某某"), _u("T.SZ", name="ST某某", typ="ub")])
    row = _row(sq.compute_seal_quality(data_dir, exclude_st=True), D0)
    assert (row["touched"], row["sealed"]) == (1, 1)
    keep = _row(sq.compute_seal_quality(data_dir, exclude_st=False), D0)
    assert keep["touched"] == 3


def test_new_listing_rows_excluded(data_dir):
    # 新股上市初期 (N/C 开头) 涨跌幅规则不同, 日线不按涨停计, 不能算作炸板
    _enriched(data_dir, D1, {"A.SZ": 1})
    _pool(data_dir, D1, [_u("A.SZ"), _u("N.SH", name="N新股"), _u("C.SZ", name="C次新", typ="ub")])
    row = _row(sq.compute_seal_quality(data_dir, exclude_st=False), D1)
    assert (row["touched"], row["sealed"], row["break_rate"]) == (1, 1, 0.0)


def test_fd_median_null_when_unit_suspect(data_dir):
    # 实测 2020-05~2023-11 猫爪封单额口径不同 (约小 600 倍): 封单额/成交额中位 < 0.5% 的日子不给封单额
    _enriched(data_dir, D1, {"A.SZ": 1, "B.SZ": 1})
    _pool(data_dir, D1, [_u("A.SZ", fd=5e4, amount=3e8), _u("B.SZ", fd=6e4, amount=2e8)])
    row = _row(sq.compute_seal_quality(data_dir, exclude_st=False), D1)
    assert row["fd_median"] is None
    assert row["early_share"] == pytest.approx(1.0)      # 其它指标不受影响


def test_day_without_pool_partition_has_no_row(data_dir):
    _enriched(data_dir, D1, {"A.SZ": 1})
    _enriched(data_dir, D1 + timedelta(days=1), {"A.SZ": 2})
    _pool(data_dir, D1, [_u("A.SZ")])
    df = sq.compute_seal_quality(data_dir, exclude_st=False)
    assert df["date"].to_list() == [D1]


def test_no_touch_day_has_null_rates(data_dir):
    _enriched(data_dir, D1, {})
    _pool(data_dir, D1, [{"symbol": "X.SZ", "type": "d", "first_time": None, "last_time": None,
                          "open_times": 0, "fd_amount": None}])
    row = _row(sq.compute_seal_quality(data_dir, exclude_st=False), D1)
    assert row["touched"] == 0 and row["break_rate"] is None and row["early_share"] is None


def _many_days(data_dir, n: int):
    days = [date(2021, 1, 4) + timedelta(days=i) for i in range(n)]
    for i, day in enumerate(days):
        sealed = {f"S{j}": 1 for j in range(10)}
        _enriched(data_dir, day, sealed)
        breaks = [_u(f"B{j}", typ="ub") for j in range(i % 10)]  # 炸板数随日递增
        _pool(data_dir, day, [_u(s) for s in sealed] + breaks)
    return days


def test_payload_latest_percentiles_and_range(data_dir):
    days = _many_days(data_dir, 70)
    out = sq.seal_quality_payload(data_dir, days[10], days[-1])
    assert out["rows"][0]["date"] == days[10].isoformat() and len(out["rows"]) == 60
    assert out["latest"]["date"] == days[-1].isoformat()
    assert 0 <= out["latest"]["pct"]["break_rate"] <= 100
    assert out["reopen_from"] == "2020-01-02"


def test_percentile_null_with_short_history(data_dir):
    days = _many_days(data_dir, 30)
    out = sq.seal_quality_payload(data_dir, days[0], days[-1])
    assert out["latest"]["pct"]["break_rate"] is None


def test_empty_payload(data_dir):
    out = sq.seal_quality_payload(data_dir, None, None)
    assert out["rows"] == [] and out["latest"] is None


def test_cache_invalidates_on_new_partition(data_dir):
    _enriched(data_dir, D1, {"A.SZ": 1})
    _pool(data_dir, D1, [_u("A.SZ")])
    assert sq.load_seal_quality(data_dir).height == 1
    d2 = D1 + timedelta(days=1)
    _enriched(data_dir, d2, {"A.SZ": 2})
    _pool(data_dir, d2, [_u("A.SZ")])
    assert sq.load_seal_quality(data_dir).height == 2
