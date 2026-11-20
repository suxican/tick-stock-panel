"""监管名单与严重异动预测读取: 留档 → 源中立契约, 区分「无」与「不可用」。"""
from datetime import date

import polars as pl
import pytest

from app.services import regulatory_reference as reg

FRI, MON = date(2026, 11, 20), date(2026, 11, 23)


def _write(data_dir, dataset, day, rows):
    path = data_dir / "meoz_archive" / "v1" / dataset / f"date={day.isoformat()}.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    frame = pl.DataFrame(rows, schema={k: pl.String for k in rows[0]}) if rows else pl.DataFrame(
        schema={"fetched_at": pl.String})
    frame.write_parquet(path)


MONITOR = {"type": "交易所风险提示", "symbol": "605255", "name": "天普股份", "startdate": "20261119",
           "enddate": "20261202", "reason": "股价累计涨幅较大", "fetched_at": "x"}
ANOMALY = {"tradedate": "20261120", "symbol": "688613", "name": "奥精医疗", "pct_chg": "-2.8571",
           "rule": "30日偏离值-70%", "elapsed_days": "12", "deviation_pct": "-68.18",
           "required_pct_chg": "-4.90787315", "trigger_price": "5.99", "status": "reachable", "fetched_at": "x"}


def test_missing_archives_are_unknown(tmp_path):
    ref = reg.regulatory_reference(MON, tmp_path)
    assert ref == {"monitor": None, "anomaly": None, "monitor_as_of": None, "anomaly_as_of": None}


def test_monday_uses_friday_archives_and_period_filter(tmp_path):
    _write(tmp_path, "point_monitor", FRI, [MONITOR, {**MONITOR, "symbol": "600825", "enddate": "20261120"}])
    _write(tmp_path, "anomaly_forecast", FRI, [ANOMALY])
    ref = reg.regulatory_reference(MON, tmp_path)
    assert set(ref["monitor"]) == {"605255"}  # 600825 监控期已在周五结束
    assert ref["monitor"]["605255"] == [{"label": "交易所风险提示", "start": "2026-11-19", "end": "2026-12-02",
                                         "reason": "股价累计涨幅较大", "name": "天普股份"}]
    assert ref["anomaly"]["688613"] == {"rule": "30日偏离值-70%", "trigger_price": 5.99,
                                        "required_pct": -0.0490787315, "status": "reachable", "name": "奥精医疗",
                                        "elapsed_days": 12, "deviation_pct": pytest.approx(-0.6818)}
    assert (ref["monitor_as_of"], ref["anomaly_as_of"]) == ("2026-11-20", "2026-11-20")


def test_empty_archive_means_none_not_unknown(tmp_path):
    _write(tmp_path, "point_monitor", FRI, [])
    _write(tmp_path, "anomaly_forecast", FRI, [])
    ref = reg.regulatory_reference(MON, tmp_path)
    assert ref["monitor"] == {} and ref["anomaly"] == {}


def test_anomaly_forecast_only_from_previous_session(tmp_path):
    """周四的预测作用于周五, 不能用于周一; 当天自己的留档 (盘后) 也不能用于当天。"""
    _write(tmp_path, "anomaly_forecast", date(2026, 11, 19), [ANOMALY])
    _write(tmp_path, "anomaly_forecast", MON, [ANOMALY])
    assert reg.regulatory_reference(MON, tmp_path)["anomaly"] is None


def test_monitor_archive_older_than_previous_session_is_unknown(tmp_path):
    _write(tmp_path, "point_monitor", date(2026, 11, 18), [MONITOR])
    assert reg.regulatory_reference(MON, tmp_path)["monitor"] is None
    assert reg.regulatory_reference(date(2026, 11, 19), tmp_path)["monitor"] == {"605255": [
        {"label": "交易所风险提示", "start": "2026-11-19", "end": "2026-12-02", "reason": "股价累计涨幅较大",
         "name": "天普股份"}]}


def test_same_day_monitor_archive_is_used_after_close(tmp_path):
    """盘后分析当天: 当天 15:40 的留档最新, 优先使用。"""
    _write(tmp_path, "point_monitor", FRI, [MONITOR])
    _write(tmp_path, "point_monitor", date(2026, 11, 19), [])
    assert set(reg.regulatory_reference(FRI, tmp_path)["monitor"]) == {"605255"}


def test_listing_resolves_suffix_and_flattens_for_display(tmp_path):
    """异动监控页「交易所口径」: 目标交易日适用的名单, 代码补交易所后缀 (按证券目录, 不猜)。"""
    _write(tmp_path, "point_monitor", FRI, [MONITOR])
    _write(tmp_path, "anomaly_forecast", FRI, [ANOMALY])
    view = reg.regulatory_listing(MON, tmp_path, {"605255": "605255.SH", "688613": "688613.SH"})
    assert view["target_day"] == "2026-11-23"
    assert (view["monitor_as_of"], view["anomaly_as_of"]) == ("2026-11-20", "2026-11-20")
    assert view["anomaly"] == [{"symbol": "688613.SH", "code": "688613", "name": "奥精医疗", "rule": "30日偏离值-70%",
                                "trigger_price": 5.99, "required_pct": -0.0490787315, "status": "reachable",
                                "elapsed_days": 12, "deviation_pct": pytest.approx(-0.6818)}]
    assert view["monitor"] == [{"symbol": "605255.SH", "code": "605255", "name": "天普股份", "label": "交易所风险提示",
                                "start": "2026-11-19", "end": "2026-12-02", "reason": "股价累计涨幅较大"}]
    unknown = reg.regulatory_listing(MON, tmp_path / "none", {})
    assert unknown["anomaly"] is None and unknown["monitor"] is None  # 留档缺失: 不可用, 不显示为空名单


def test_abnormal_regulatory_api_uses_next_session_and_catalog(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import abnormal as abnormal_api
    from app.services import holding_analysis

    _write(tmp_path, "anomaly_forecast", FRI, [ANOMALY])
    monkeypatch.setattr(holding_analysis, "trade_day", lambda now: MON)
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path),
                           get_instruments=lambda: pl.DataFrame({"symbol": ["688613.SH", "000001.SZ", "000001.SH"]}))
    app = FastAPI()
    app.include_router(abnormal_api.router)
    app.state.repo = repo
    body = TestClient(app).get("/api/abnormal/regulatory").json()
    assert body["target_day"] == "2026-11-23" and body["monitor"] is None
    assert body["anomaly"][0]["symbol"] == "688613.SH"
