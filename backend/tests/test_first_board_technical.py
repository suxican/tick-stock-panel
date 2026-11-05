from datetime import date, datetime, time, timedelta
from types import SimpleNamespace

import polars as pl
import pytest

from app.indicators.pipeline import compute_indicators
from app.market_time import CN_TZ
from app.services import first_board as fb
from app.services import first_board_technical as tech
from app.tickflow.repository import KlineRepository

AS_OF = date(2026, 11, 5)
SYMBOL = "600000.SH"


@pytest.fixture
def days(monkeypatch):
    monkeypatch.setattr(tech.data_integrity, "_CAL", (0, None))
    return tech._sessions(AS_OF)


def series(days):
    prices = [20 + i * .1 + (i % 7) * .04 for i in range(len(days))]
    return pl.DataFrame({"symbol": [SYMBOL] * len(days), "date": days, "close": prices,
                         "raw_close": prices, "quote_ts": [None] * len(days)},
                        schema_overrides={"quote_ts": pl.Int64})


def seed(day):
    return pl.DataFrame({"symbol": [SYMBOL] * 3, "reference_date": [day] * 3,
                         "pattern": ["platform", "trend", "oversold"]})


def test_shadow_uses_exact_prior_date_and_ref_price_basis_without_live_bar(days):
    raw = series(days)
    assert len(days) == 120 and days[-1] == date(2026, 11, 4)
    # Rebase on a synthetic future corporate action. The shadow values must
    # remain on the reference session's raw price scale.
    scaled = raw.with_columns((pl.col("close") / 2).alias("close"))
    future = scaled.tail(1).with_columns(pl.lit(AS_OF).alias("date"), pl.lit(1.e5).alias("close"))
    a = tech._values(pl.concat([future, scaled.reverse()]), days)[SYMBOL]
    b = tech._values(raw, days)[SYMBOL]
    expected = compute_indicators(raw, needed=tech._INDICATORS).tail(2).to_dicts()
    for key in tech._INDICATORS:
        assert a[key] == pytest.approx(b[key]) and a[key] == pytest.approx(expected[-1][key])
    assert a["prev_macd_hist"] == pytest.approx(expected[-2]["macd_hist"])
    assert a["close"] == raw["raw_close"][-1]


@pytest.mark.parametrize("issue,position,ma_ok", [
    ("missing", -2, False), ("missing", -22, True), ("duplicate", -10, False),
    ("midday", -1, False), ("wrong_day", -2, False), ("midday", -70, True),
    ("nan", -3, False), ("zero", -21, False), ("null", -50, True),
])
def test_contiguous_warmup_completion_and_invalid_inputs(days, issue, position, ma_ok):
    frame = series(days)
    d = days[position]
    if issue == "missing":
        frame = frame.filter(pl.col("date") != d)
    elif issue == "duplicate":
        frame = pl.concat([frame, frame.filter(pl.col("date") == d)])
    elif issue in {"wrong_day", "midday"}:
        ts = datetime.combine(d + timedelta(days=issue == "wrong_day"),
                              time(15) if issue == "wrong_day" else time(14, 56), CN_TZ)
        frame = frame.with_columns(pl.when(pl.col("date") == d).then(int(ts.timestamp() * 1000))
                                   .otherwise(pl.col("quote_ts")).alias("quote_ts"))
    else:
        value = {"nan": float("nan"), "zero": 0., "null": None}[issue]
        frame = frame.with_columns(pl.when(pl.col("date") == d).then(value).otherwise(pl.col("close")).alias("close"))
    v = tech._values(frame, days).get(SYMBOL, {})
    obs = tech._evidence("platform", days[-1], v, "")
    assert (obs["ma_support"] is not None) == ma_ok
    assert obs["macd_support"] is None and obs["macd_hist"] is None
    assert obs["status"] == ("partial" if ma_ok else "unavailable")


@pytest.mark.parametrize("pattern,ma,macd", [
    ("platform", True, False), ("trend", False, False), ("oversold", True, True),
])
def test_patterns_have_independent_unrounded_definitions(pattern, ma, macd):
    v = dict(close=10., ma5=10., ma10=9.5, ma20=9.5, prev_close=9.7, prev_ma5=9.8,
             prev_ma20=9.5, macd_dif=-1e-9, macd_dea=0., macd_hist=-2e-9,
             prev_macd_hist=-2.1e-9, _ma_bars=21, _macd_bars=120)
    obs = tech._evidence(pattern, AS_OF, v, "")
    assert obs["status"] == "ready" and obs["ma_support"] is ma and obs["macd_support"] is macd
    if pattern == "oversold":
        assert not tech._evidence(pattern, AS_OF, v | {"macd_hist": 0.}, "")["macd_support"]
    if pattern == "trend":
        stronger = v | {"ma10": 9.6, "macd_dif": 1.e-9, "macd_hist": 2.e-9}
        assert tech._evidence(pattern, AS_OF, stronger, "")["macd_support"]
        assert tech._evidence(pattern, AS_OF, stronger, "")["ma_support"]


def test_cold_warm_partition_identity_and_repair_without_live_cache(tmp_path, days, monkeypatch):
    repo = KlineRepository(SimpleNamespace(data_dir=tmp_path, db=None))
    adapter = tech.FirstBoardTechnical(repo)
    calls = []
    original = tech._values
    monkeypatch.setattr(tech, "_values", lambda f, s: (calls.append(len(f)), original(f, s))[1])

    def write(day, frame):
        path = tmp_path / "kline_daily_enriched" / f"date={day}" / "part.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        frame.write_parquet(path)

    # Legacy partition without quote_ts is a historical day bar. New explicit
    # incomplete bars must remain detectable even if a hot cache drops quote_ts.
    for d, frame in series(days).partition_by("date", as_dict=True).items():
        write(d[0], frame.drop("quote_ts"))
    bad = tmp_path / "kline_daily_enriched" / f"date={AS_OF}" / "part.parquet"
    bad.parent.mkdir(parents=True)
    bad.write_bytes(b"future partition must never be read")
    monkeypatch.setattr(repo, "get_daily_batch", lambda *_a, **_k: pytest.fail("used chart cache"))

    def attach(ref):
        return adapter.attach(seed(ref), as_of=AS_OF, reference=ref)["technical_shadow"].to_list()

    first = attach(days[-1])
    assert all(x["status"] == "ready" for x in first) and attach(days[-1]) == first
    assert calls == [120]
    assert all(x["reference_date"] is None and x["ma_support"] is None for x in attach(days[-2]))
    assert calls == [120]
    ts = int(datetime.combine(days[-1], time(14, 56), CN_TZ).timestamp() * 1000)
    write(days[-1], series(days).tail(1).with_columns(pl.lit(ts).cast(pl.Int64).alias("quote_ts")))
    assert all(x["status"] == "unavailable" for x in attach(days[-1]))
    assert calls == [120, 120]
    monkeypatch.setattr(repo, "get_first_board_technical_closes", lambda *_: (_ for _ in ()).throw(OSError()))
    assert "读取失败" in attach(days[-1])[0]["ma_reason"]


def test_service_prepares_same_candidate_seed_when_technical_read_fails(tmp_path, days, monkeypatch):
    prices = series(days).with_columns(
        (10 + pl.int_range(pl.len()) * .002).alias("close"),
        (10 + pl.int_range(pl.len()) * .002).alias("raw_close"),
    ).with_columns(pl.lit("测试股").alias("name"), (pl.col("close") + .01).alias("high"),
                   (pl.col("close") - .01).alias("low"))
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path),
                           get_index_daily=lambda *_a, **_k: pl.DataFrame({"date": days}),
                           get_first_board_technical_closes=lambda *_: (prices, ("v1",)))
    service = fb.FirstBoardService(repo, history_loader=lambda _d, n: prices.tail(n))
    monkeypatch.setattr(fb, "load_environment", lambda *_a, **_k: {"allowed": True})
    monkeypatch.setattr(fb, "load_sector_members", lambda *_: {})
    monkeypatch.setattr(service._attention_data, "prepare", lambda **_: None)
    service._prepare(AS_OF, service._config)
    before = service._candidates
    assert before.height and before["technical_shadow"].to_list()[0]["status"] == "ready"

    def failure(*_):
        raise OSError("optional history read failed")

    repo.get_first_board_technical_closes = failure
    service._prepared_key = None
    service._prepare(AS_OF, service._config)
    after = service._candidates
    assert after.drop("technical_shadow").equals(before.drop("technical_shadow"))
    assert service._environment["allowed"] and after["technical_shadow"].to_list()[0]["status"] == "unavailable"
