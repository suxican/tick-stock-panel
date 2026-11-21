from datetime import datetime

import pytest

from app.market_time import CN_TZ
from app.services.first_board_temperature import first_board_temperature

DAY = "2026-10-16"
AT = datetime(2026, 10, 16, 10, 30, tzinfo=CN_TZ)


def ladder(seal=.8, up=8, broken=2, as_of=DAY):
    return {"as_of": as_of, "observed_at": AT.isoformat(), "seal_rate": seal, "limit_up": up, "broken": broken}


def feedback(mean=.03, *, mode="live", **summary):
    base = {"sample_count": 12, "coverage": 1., "mean_change_pct": mean, "up_ratio": .6, "small_sample": False, **summary}
    return {"status": "ready", "quote_mode": mode, "as_of": DAY, "groups": {"all": base}}


def temp(lad=None, fb=None, *, status="ready", now=AT, as_of=DAY):
    return first_board_temperature(ladder() if lad is None else lad, feedback() if fb is None else fb,
                                   status=status, as_of=as_of, now=now)


@pytest.mark.parametrize(("seal", "mean", "tier"), [
    (.70, .02, "attack"), (.80, .03, "attack"), (.6999, .02, "probe"), (.70, .0199, "probe"),
    (.70, -.01, "probe"), (.60, 0., "probe"), (.60, -.0001, "rest"), (.5999, .05, "rest"),
])
def test_tier_boundaries(seal, mean, tier):
    result = temp(ladder(seal=seal), feedback(mean))
    assert result["tier"] == tier and result["version"] == "temperature-v1"
    assert result["label"] == {"attack": "进攻", "probe": "试错", "rest": "休息"}[tier]


@pytest.mark.parametrize(("kwargs", "reason"), [
    ({"status": "stale"}, "行情已过期"), ({"status": "waiting"}, "等待有效行情"),
    ({"status": "disabled"}, "自动盯盘已暂停"), ({"status": "error"}, "首板数据暂不可用"),
    ({"lad": {}}, "缺少当日涨停梯队"), ({"lad": ladder(as_of="2026-10-15")}, "缺少当日涨停梯队"),
    ({"lad": ladder(seal=None, broken=None)}, "封板率暂不可用（缺少炸板数据）"),
    ({"lad": ladder(up=7, broken=2)}, "触板仅 9 家，封板率尚不稳定"),
    ({"fb": feedback(small_sample=True)}, "昨日首板样本少于 10 只"),
    ({"fb": feedback(coverage=.79)}, "昨日首板行情覆盖不足 80%"),
    ({"fb": feedback(mode="snapshot")}, "昨日首板反馈不是盘中实测"),
])
def test_unknown_cases(kwargs, reason):
    result = temp(**kwargs)
    assert result["tier"] == "unknown" and result["label"] == "待确认" and result["reason"] == reason


def test_unknown_keeps_available_numbers_and_nulls_missing_ones():
    result = temp(ladder(seal=None, broken=None))
    assert result["seal_rate"] is None and result["touch_count"] is None
    assert result["feedback_mean"] == .03 and result["feedback_sample"] == 12


def test_ten_touches_is_enough():
    assert temp(ladder(up=8, broken=2))["tier"] == "attack"


def test_early_and_session_stage():
    assert temp(now=AT.replace(hour=9, minute=59, second=59))["stage"] == "early"
    assert temp(now=AT.replace(hour=10, minute=0))["stage"] == "session"


def test_closed_after_session_is_close_and_accepts_snapshot_feedback():
    result = temp(fb=feedback(mode="snapshot"), status="closed", now=AT.replace(hour=15, minute=30))
    assert result["stage"] == "close" and result["tier"] == "attack"


def test_lunch_break_keeps_session_reading():
    result = temp(fb=feedback(mode="snapshot"), status="closed", now=AT.replace(hour=12))
    assert result["stage"] == "session" and result["tier"] == "attack"


def test_next_morning_shows_previous_close():
    result = temp(fb=feedback(mode="snapshot"), status="closed", now=datetime(2026, 10, 19, 9, 0, tzinfo=CN_TZ))
    assert result["stage"] == "close" and result["as_of"] == DAY and result["tier"] == "attack"


def test_preopen_auction_is_not_a_reading():
    result = temp(fb=feedback(mode="snapshot"), status="closed", now=AT.replace(hour=9, minute=26))
    assert result["tier"] == "unknown" and result["reason"] == "集合竞价阶段，待开盘"


def test_closing_auction_is_not_yet_close():
    assert temp(fb=feedback(mode="snapshot"), status="closed", now=AT.replace(hour=14, minute=58))["stage"] == "session"
    assert temp(fb=feedback(mode="snapshot"), status="closed", now=AT.replace(hour=15, minute=0))["stage"] == "close"


def test_naive_now_is_treated_as_beijing_time():
    result = temp(status="ready", now=AT.replace(tzinfo=None, hour=9, minute=40))
    assert result["stage"] == "early"


def test_empty_snapshot_and_zero_touches_get_the_right_reason():
    assert temp(ladder(), status="closed", now=AT.replace(hour=20), as_of=None)["reason"] == "缺少当日涨停梯队"
    assert temp(ladder(seal=None, up=0, broken=0))["reason"] == "触板仅 0 家，封板率尚不稳定"
