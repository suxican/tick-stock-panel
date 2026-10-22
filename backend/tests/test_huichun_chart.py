"""The research chart must reproduce A0, including its price basis and gaps."""

import json
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import polars as pl
import pytest

from app.market_time import CN_TZ
from app.services import huichun_chart as module
from app.services.huichun_audit import FACTOR_SCHEMA
from app.services.huichun_chart import HuichunChartRequest, build_huichun_chart
from app.services.huichun_mode import HuichunModeService, HuichunRules


@pytest.fixture
def chart_env(tmp_path, monkeypatch):
    origin = date(2023, 1, 2)
    days = [origin + timedelta(days=i) for i in range(600)
            if (origin + timedelta(days=i)).weekday() < 5][:400]
    values = [10 + i * .02 + (i % 17) * .03 for i in range(len(days))]
    raw = pl.DataFrame({
        "symbol": ["600001.SH"] * len(days), "date": days,
        "open": values, "high": [v + .1 for v in values],
        "low": [v - .1 for v in values], "close": values,
        "volume": [1000.0] * len(days), "amount": [10000.0] * len(days),
    })
    env = SimpleNamespace(
        days=days, values=values, raw=raw, factors=pl.DataFrame(schema=FACTOR_SCHEMA),
        generation=0,
    )
    calendar_path = tmp_path / "calendar.json"
    calendar_path.write_text(json.dumps({
        "version": 1, "coverage_start": str(days[0]), "coverage_end": str(days[-1]),
        "weekends_closed": True, "closures": [], "sources": [{
            "id": "fixture", "url": "https://www.sse.com.cn/notice",
            "published_on": "2022-12-01", "title": "Synthetic test calendar",
        }],
    }), encoding="utf-8")
    def execute_all(query, params=None):
        if query == "DESCRIBE instruments":
            return [(name,) for name in ("symbol", "name", "listing_date")]
        if "FROM instruments" in query:
            return [("测试股票", days[0])]
        return [(d,) for d in days if d <= params[0]]

    repo = SimpleNamespace(execute_one=lambda query, params: (1,), execute_all=execute_all)
    service = HuichunModeService.__new__(HuichunModeService)
    service.repo = repo
    service._calendar_path = calendar_path
    service._calendar_source = "sentinel_scan_source"
    service._latest_observed_date = date(2020, 1, 1)
    service._input_signature = lambda: [env.generation]
    service._raw = lambda symbols, end: env.raw.filter(pl.col("date") <= end)
    service._factors = lambda symbols, end: env.factors.filter(
        pl.col("trade_date").is_null() | (pl.col("trade_date") <= end)
    )
    env.service = service
    monkeypatch.setattr(module, "_now", lambda: datetime(2026, 10, 22, 16, tzinfo=CN_TZ))
    return env


def request(env, **updates):
    return HuichunChartRequest(
        **{"symbol": "600001.SH", "as_of": env.days[-1], "rules": HuichunRules(),
           "rule_revision": 7, **updates}
    )


def ema(values, span):
    result = [values[0]]
    alpha = 2 / (span + 1)
    for value in values[1:]:
        result.append(alpha * value + (1 - alpha) * result[-1])
    return result


def test_chart_uses_full_history_a0_macd_not_generic_12_26(chart_env):
    env = chart_env
    result = build_huichun_chart(env.service, request(env))
    dif = [a - b for a, b in zip(ema(env.values, 10), ema(env.values, 20), strict=True)]
    dea = ema(dif, 9)
    row = result["rows"][-1]
    assert row["macd_dif"] == pytest.approx(dif[-1], abs=1e-12)
    assert row["macd_dea"] == pytest.approx(dea[-1], abs=1e-12)
    assert row["macd_hist"] == pytest.approx(2 * (dif[-1] - dea[-1]), abs=1e-12)
    assert row["ma60"] == pytest.approx(sum(env.values[-60:]) / 60)
    generic_dif = ema(env.values, 12)[-1] - ema(env.values, 26)[-1]
    assert abs(row["macd_dif"] - generic_dif) > .01
    assert result["huichun"]["macd"] == [10, 20, 9]
    assert result["huichun"]["rule_revision"] == 7
    assert result["huichun"]["data_status"] == "end_reached"
    assert result["huichun"]["missing_observed_days"] == 0
    assert result["huichun"]["segment_count"] == 1
    assert env.service._calendar_source == "sentinel_scan_source"
    assert env.service._latest_observed_date == date(2020, 1, 1)


def test_adjustment_event_scales_ohlc_and_indicators_together(chart_env):
    env = chart_env
    base = build_huichun_chart(env.service, request(env))
    event = env.days[310]
    env.raw = env.raw.with_columns([
        pl.when(pl.col("date") >= event).then(pl.col(c) / 2).otherwise(pl.col(c)).alias(c)
        for c in ("open", "high", "low", "close")
    ])
    env.factors = pl.DataFrame({"symbol": ["600001.SH"], "trade_date": [event],
                                "ex_factor": [2.0]}, schema=FACTOR_SCHEMA)
    adjusted = build_huichun_chart(env.service, request(env))
    for old, new in zip(base["rows"], adjusted["rows"], strict=True):
        for col in ("open", "high", "low", "close", "ma60", "macd_dif", "macd_dea", "macd_hist"):
            assert new[col] == (pytest.approx(old[col] / 2) if old[col] is not None else None)
    assert adjusted["rows"][-1]["close"] == env.raw["close"][-1]
    assert adjusted["huichun"]["price_basis_date"] == str(env.days[-1])


def test_display_window_does_not_restart_indicators(chart_env):
    env = chart_env
    full = build_huichun_chart(env.service, request(env))
    cropped = build_huichun_chart(env.service, request(
        env, start_date=env.days[300], end_date=env.days[350],
    ))
    assert cropped["rows"] == full["rows"][300:351]


def test_future_prices_and_factors_do_not_change_historical_chart(chart_env):
    env = chart_env
    body = request(env, as_of=env.days[330])
    before = build_huichun_chart(env.service, body)
    env.raw = env.raw.with_columns(
        pl.when(pl.col("date") > body.as_of).then(1e6).otherwise(pl.col("close")).alias("close")
    )
    env.factors = pl.DataFrame({"symbol": ["600001.SH"], "trade_date": [env.days[350]],
                                "ex_factor": [-1.0]}, schema=FACTOR_SCHEMA)
    assert build_huichun_chart(env.service, body) == before


def test_unknown_daily_gap_restarts_a0_indicator_segment(chart_env):
    env = chart_env
    env.raw = env.raw.filter(pl.col("date") != env.days[300])
    result = build_huichun_chart(env.service, request(env))
    row = next(r for r in result["rows"] if r["date"] == str(env.days[301]))
    assert row["macd_dif"] == row["macd_dea"] == row["macd_hist"] == 0
    assert row["ma60"] is None
    assert row["segment_id"] == "1"
    # Reaching the cutoff does not certify completeness of the preceding path.
    assert result["huichun"]["data_status"] == "end_reached"
    assert result["huichun"]["missing_observed_days"] == 1
    assert result["huichun"]["segment_count"] == 2


def test_bad_factor_preserves_safe_chart_prefix_and_reports_truncation(chart_env):
    env = chart_env
    prefix = build_huichun_chart(env.service, request(env, as_of=env.days[329]))
    env.factors = pl.DataFrame({"symbol": ["600001.SH"], "trade_date": [env.days[330]],
                                "ex_factor": [-1.0]}, schema=FACTOR_SCHEMA)
    result = build_huichun_chart(env.service, request(env))
    assert result["rows"] == prefix["rows"]
    assert result["huichun"]["invalid_from"] == str(env.days[330])
    assert result["huichun"]["latest_valid_date"] == str(env.days[329])
    assert result["huichun"]["as_of"] == str(env.days[-1])
    assert result["huichun"]["factor_status"] == "quarantined"
    assert result["huichun"]["data_status"] == "truncated"
    assert result["huichun"]["missing_observed_days"] == 70


def test_undated_factor_returns_empty_chart_with_explicit_reason(chart_env):
    env = chart_env
    env.factors = pl.DataFrame({"symbol": ["600001.SH"], "trade_date": [None],
                                "ex_factor": [2.0]}, schema=FACTOR_SCHEMA)
    result = build_huichun_chart(env.service, request(env))
    assert result["rows"] == []
    assert result["huichun"]["factor_status"] == "invalid_undated"
    assert result["huichun"]["latest_valid_date"] is None
    assert result["huichun"]["missing_observed_days"] is None
    assert result["huichun"]["segment_count"] is None


def test_no_local_stock_data_never_falls_back_to_live(chart_env):
    env = chart_env
    env.raw = env.raw.head(0)
    result = build_huichun_chart(env.service, request(env))
    assert result["rows"] == []
    assert result["source"] == "huichun_a0"
    assert result["huichun"]["data_status"] == "no_valid_data"


def test_chart_rejects_mixed_input_generations(chart_env):
    env = chart_env
    read_raw = env.service._raw

    def changed(symbols, end):
        env.generation += 1
        return read_raw(symbols, end)

    env.service._raw = changed
    with pytest.raises(ValueError, match="行情已更新"):
        build_huichun_chart(env.service, request(env))


@pytest.mark.parametrize("offset,hour", [(0, 14), (1, 16)])
def test_future_or_unclosed_as_of_is_rejected(chart_env, monkeypatch, offset, hour):
    env = chart_env
    today = env.days[-1]
    monkeypatch.setattr(module, "_now", lambda: datetime.combine(
        today, datetime.min.time(), CN_TZ,
    ).replace(hour=hour))
    with pytest.raises(ValueError, match="已收盘"):
        build_huichun_chart(env.service, request(env, as_of=today + timedelta(days=offset)))


@pytest.mark.parametrize("updates", [
    {"symbol": "000001.SH"}, {"symbol": "600001.SH;SELECT"}, {"symbol": "430001.BJ"},
    {"rule_revision": -1}, {"end_date": date(2026, 10, 23)},
    {"start_date": date(2026, 10, 23)},
])
def test_chart_request_rejects_incompatible_context(chart_env, updates):
    with pytest.raises(ValueError):
        request(chart_env, **updates)
