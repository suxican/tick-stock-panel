from datetime import date
from types import SimpleNamespace

import pytest

from app.services.first_board_seal import MAX_AGE_SECONDS, load_sealed, seal_quality

DAY = date(2026, 10, 16)
SYMBOL = "600000.SH"


def depth(entries, *, ready=True, age=5.0):
    return SimpleNamespace(is_sealed_ready=lambda _day: ready, get_sealed_age=lambda _day: age,
                           get_sealed_map=lambda _day, is_down: {} if is_down else entries)


def test_real_seal_reports_lots_amount_and_ratio_in_documented_units():
    sealed = load_sealed(depth({SYMBOL: {"sealed": True, "vol": 12_000, "ready": True, "age": 5.0}}), DAY)
    quality = seal_quality(sealed, SYMBOL, price=11.0, amount=66_000_000.0, live=True)
    assert quality["status"] == "real"
    assert quality["vol_lots"] == 12_000
    # 12000 手 × 100 股 × 11 元。
    assert quality["sealed_amount"] == pytest.approx(13_200_000)
    assert quality["seal_ratio"] == pytest.approx(.2)
    assert quality["age_seconds"] == 5.0


@pytest.mark.parametrize("flag,status", [(False, "fake"), (None, "pending")])
def test_fake_and_undetermined_seals_are_not_reported_as_real(flag, status):
    sealed = load_sealed(depth({SYMBOL: {"sealed": flag, "vol": 300}}), DAY)
    assert seal_quality(sealed, SYMBOL, price=11.0, amount=1e8, live=True)["status"] == status


def test_missing_inputs_stay_unknown_instead_of_zero():
    sealed = load_sealed(depth({SYMBOL: {"sealed": True, "vol": None}}), DAY)
    quality = seal_quality(sealed, SYMBOL, price=11.0, amount=1e8, live=True)
    assert quality["status"] == "real" and quality["vol_lots"] is None and quality["sealed_amount"] is None
    sealed = load_sealed(depth({SYMBOL: {"sealed": True, "vol": 100}}), DAY)
    for amount in (None, 0.0, float("nan")):
        quality = seal_quality(sealed, SYMBOL, price=11.0, amount=amount, live=True)
        assert quality["sealed_amount"] == pytest.approx(110_000) and quality["seal_ratio"] is None
    assert seal_quality(sealed, SYMBOL, price=None, amount=1e8, live=True)["sealed_amount"] is None
    assert seal_quality(sealed, "600001.SH", price=11.0, amount=1e8, live=True)["status"] == "pending"


def test_unready_stale_or_failing_depth_is_unavailable():
    assert seal_quality(load_sealed(None, DAY), SYMBOL, price=11.0, amount=1e8, live=True)["status"] == "unavailable"
    entries = {SYMBOL: {"sealed": True, "vol": 100}}
    unready = load_sealed(depth(entries, ready=False), DAY)
    assert seal_quality(unready, SYMBOL, price=11.0, amount=1e8, live=True)["status"] == "unavailable"
    stale = load_sealed(depth(entries, age=MAX_AGE_SECONDS + 1), DAY)
    quality = seal_quality(stale, SYMBOL, price=11.0, amount=1e8, live=True)
    assert quality["status"] == "unavailable" and "未更新" in quality["reason"]
    # 盘后定版没有时效要求: 同一份数据在非盯盘时段仍可查看。
    assert seal_quality(stale, SYMBOL, price=11.0, amount=1e8, live=False)["status"] == "real"

    def broken(_day):
        raise RuntimeError("depth down")
    failing = SimpleNamespace(is_sealed_ready=broken)
    assert load_sealed(failing, DAY)["ready"] is False


def test_polling_status_reports_actionable_reasons():
    from app.services.first_board_seal import polling_status
    assert polling_status(None) == {"running": False, "reason": "五档封单服务未启动"}
    running = SimpleNamespace(polling_status=lambda: (True, None))
    assert polling_status(running) == {"running": True, "reason": None}
    off = SimpleNamespace(polling_status=lambda: (False, "未开启连板梯队监控"))
    assert polling_status(off)["reason"] == "未开启连板梯队监控"

    def broken():
        raise RuntimeError("boom")
    assert polling_status(SimpleNamespace(polling_status=broken))["running"] is False


@pytest.mark.parametrize("capable,ladder,realtime,reason", [
    (False, True, True, "五档盘口能力"), (True, False, True, "连板梯队监控"), (True, True, False, "实时行情"),
])
def test_depth_service_polling_status_follows_start_gates(monkeypatch, capable, ladder, realtime, reason):
    from app.services import preferences
    from app.services.depth_service import DepthService
    service = DepthService()
    monkeypatch.setattr(service, "_has_capability", lambda: capable)
    monkeypatch.setattr(preferences, "get_limit_ladder_monitor_enabled", lambda: ladder)
    monkeypatch.setattr(preferences, "get_realtime_quotes_enabled", lambda: realtime)
    running, message = service.polling_status()
    assert running is False and reason in message
    service._running = True
    assert service.polling_status() == (True, None)
