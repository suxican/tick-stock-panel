import json
from datetime import date, datetime, timedelta
from unittest.mock import Mock

import pytest

from app.market_time import CN_TZ
from app.plugins.kaipanla.client import KaipanlaClient, KaipanlaError
from app.services import kaipanla_intraday as svc
from app.services.kaipanla_intraday import KaipanlaUnavailableError, capture, read_day, read_intraday

NOW = datetime(2026, 11, 17, 10, 30, tzinfo=CN_TZ)
CLOSE_1505 = datetime(2026, 11, 17, 15, 5, tzinfo=CN_TZ)


@pytest.fixture(autouse=True)
def _reset():
    svc._reset_for_tests()
    yield
    svc._reset_for_tests()


def _stock(symbol, boards, theme, name):
    return {"symbol": symbol, "name": symbol, "boards": boards, "first_limit_at": "2026-11-17T09:30:00+08:00",
            "reason": name, "theme_code": theme, "plates": [name], "seal_amount": 1.0, "max_seal": 2.0,
            "reseal": False, "label": None, "price": 1.0, "change_pct": 0.1}


def fake_client(*, empty=False, fail=(), limit_up=41):
    c = Mock(spec=KaipanlaClient)

    def boom(*_a, **_k):
        raise KaipanlaError("down")

    stocks = {
        1: [_stock("000001.SZ", 1, "801001", "芯片"), _stock("000002.SZ", 1, "801001", "芯片"),
            _stock("600001.SH", 1, "801807", "算力")],
        2: [_stock("000003.SZ", 2, "801807", "算力")], 3: [], 4: [], 5: [],
    }
    if empty:
        stocks = {p: [] for p in range(1, 6)}
    c.limit_up_stocks.side_effect = lambda pid: ("2026-11-17", stocks[pid])
    c.limit_counts.return_value = [0] * 5 if empty else [35, 4, 1, 1, 0]
    c.first_board_broken.return_value = [] if empty else [{"symbol": f"30000{i}.SZ"} for i in range(10)]
    c.sentiment.return_value = {"day": "2026-11-17", "kpl_strong": 36, "limit_up": 0 if empty else limit_up,
                                "max_boards": 0 if empty else 4, "sharp_drawdown": 5}
    c.limit_totals.return_value = {"limit_up": 0 if empty else limit_up, "limit_down": 44}
    c.expression.return_value = None
    for name in fail:
        getattr(c, name).side_effect = boom
    return c


def _day_dir(tmp_path, day="2026-11-17"):
    return tmp_path / "kaipanla" / "v1" / day


def test_snapshot_aggregates_themes_and_tiers(tmp_path):
    snap = read_intraday(tmp_path, now=NOW, client=fake_client())
    assert snap["date"] == "2026-11-17"
    assert snap["summary"]["limit_up"] == 41 and snap["summary"]["limit_down"] == 44
    assert snap["summary"]["kpl_strong"] == 36 and snap["summary"]["max_boards"] == 4
    assert snap["tiers"][0] == {"boards": 1, "count": 35} and len(snap["tiers"]) == 5
    assert len(snap["stocks"]) == 4
    assert snap["themes"][0] == {"code": "801001", "name": "芯片", "count": 2, "symbols": ["000001.SZ", "000002.SZ"]}
    assert snap["themes"][1]["code"] == "801807" and snap["themes"][1]["count"] == 2
    assert snap["missing"] == [] and snap["stale_seconds"] == 0
    assert snap["summary"]["first_board_broken"] == 10
    assert snap["summary"]["broken_rate"] == pytest.approx(10 / (41 + 10))


def test_empty_lists_are_a_valid_snapshot(tmp_path):
    snap = read_intraday(tmp_path, now=NOW, client=fake_client(empty=True))
    assert snap["summary"]["limit_up"] == 0 and snap["themes"] == [] and snap["stocks"] == []
    assert snap["stale_seconds"] == 0 and snap["summary"]["broken_rate"] is None


def test_secondary_failure_goes_to_missing(tmp_path):
    snap = read_intraday(tmp_path, now=NOW, client=fake_client(fail={"sentiment"}))
    assert "kpl_strong" in snap["missing"] and snap["summary"]["kpl_strong"] is None
    assert snap["summary"]["limit_up"] == 41


def test_core_failure_returns_previous_snapshot_as_stale(tmp_path):
    read_intraday(tmp_path, now=NOW, client=fake_client())
    later = NOW + timedelta(seconds=95)
    snap = read_intraday(tmp_path, now=later, client=fake_client(fail={"limit_up_stocks"}))
    assert snap["stale_seconds"] == 95 and snap["summary"]["limit_up"] == 41
    assert svc.status()["last_error"]


def test_never_succeeded_raises_unavailable(tmp_path):
    with pytest.raises(KaipanlaUnavailableError):
        read_intraday(tmp_path, now=NOW, client=fake_client(fail={"limit_up_stocks"}))


def test_concurrent_reads_within_ttl_share_one_round(tmp_path):
    client = fake_client()
    for s in (0, 10, 29):
        read_intraday(tmp_path, now=NOW + timedelta(seconds=s), client=client)
    assert client.limit_up_stocks.call_count == 5
    read_intraday(tmp_path, now=NOW + timedelta(seconds=31), client=client)
    assert client.limit_up_stocks.call_count == 10


def test_threads_within_ttl_share_one_round(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    client = fake_client()
    with ThreadPoolExecutor(4) as pool:
        list(pool.map(lambda _: read_intraday(tmp_path, now=NOW, client=client), range(8)))
    assert client.limit_up_stocks.call_count == 5


def test_capture_appends_observation_in_session_and_freezes_close(tmp_path):
    capture(tmp_path, NOW, client=fake_client())
    lines = (_day_dir(tmp_path) / "observations.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["summary"]["limit_up"] == 41
    capture(tmp_path, CLOSE_1505, client=fake_client())
    first = (_day_dir(tmp_path) / "close.json").read_text(encoding="utf-8")
    svc._reset_for_tests()
    capture(tmp_path, CLOSE_1505 + timedelta(minutes=1), client=fake_client(limit_up=99))
    assert (_day_dir(tmp_path) / "close.json").read_text(encoding="utf-8") == first


def test_capture_backfills_expression_once(tmp_path):
    prev = _day_dir(tmp_path, "2026-11-16")
    prev.mkdir(parents=True)
    (prev / "close.json").write_text("{}", encoding="utf-8")
    client = fake_client()
    capture(tmp_path, NOW, client=client)
    assert not (prev / "expression.json").exists()
    client.expression.return_value = {"kpl_promo_2": 0.266667}
    capture(tmp_path, NOW + timedelta(minutes=5), client=client)
    assert json.loads((prev / "expression.json").read_text(encoding="utf-8")) == {"kpl_promo_2": 0.266667}
    calls = client.expression.call_count
    capture(tmp_path, NOW + timedelta(minutes=10), client=client)
    assert client.expression.call_count == calls
    assert client.expression.call_args.args[0] == date(2026, 11, 16)


def test_capture_skips_lunch_and_non_session(tmp_path):
    client = fake_client()
    for hour in (12, 8):
        capture(tmp_path, NOW.replace(hour=hour), client=client)
    client.limit_up_stocks.assert_not_called()
    assert not (_day_dir(tmp_path) / "observations.jsonl").exists()


def test_snapshot_carries_previous_expression(tmp_path):
    prev = _day_dir(tmp_path, "2026-11-16")
    prev.mkdir(parents=True)
    (prev / "expression.json").write_text(json.dumps({"kpl_promo_2": 0.27}), encoding="utf-8")
    snap = read_intraday(tmp_path, now=NOW, client=fake_client())
    assert snap["prev_expression"] == {"date": "2026-11-16", "kpl_promo_2": 0.27}


def test_read_day_returns_archive_or_none(tmp_path):
    assert read_day(tmp_path, date(2026, 11, 13)) is None
    capture(tmp_path, CLOSE_1505, client=fake_client())
    day = read_day(tmp_path, date(2026, 11, 17))
    assert day["summary"]["limit_up"] == 41 and day["expression"] is None


def test_device_id_is_persisted(tmp_path):
    first = svc._device_id(tmp_path)
    assert first and svc._device_id(tmp_path) == first
    assert (tmp_path / "kaipanla" / "device_id").read_text(encoding="utf-8").strip() == first


def test_unexpected_exception_does_not_wedge_later_reads(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    bad = fake_client()
    bad.limit_up_stocks.side_effect = KeyError("x")
    with pytest.raises(KaipanlaUnavailableError):
        read_intraday(tmp_path, now=NOW, client=bad)
    svc._retry_after.clear()
    with ThreadPoolExecutor(1) as pool:
        snap = pool.submit(read_intraday, tmp_path, now=NOW, client=fake_client()).result(timeout=5)
    assert snap["summary"]["limit_up"] == 41


def test_previous_day_snapshot_is_not_served_as_today(tmp_path):
    read_intraday(tmp_path, now=NOW, client=fake_client())
    next_day = NOW + timedelta(days=1)
    with pytest.raises(KaipanlaUnavailableError):
        read_intraday(tmp_path, now=next_day, client=fake_client(fail={"limit_up_stocks"}))


def test_retry_backoff_counts_from_end_of_slow_failed_round(tmp_path, monkeypatch):
    read_intraday(tmp_path, now=NOW, client=fake_client())
    clock = {"t": 1000.0}
    monkeypatch.setattr(svc.time, "monotonic", lambda: clock["t"])
    slow = fake_client()

    def slow_fail(_pid):
        clock["t"] += 40
        raise KaipanlaError("timeout")

    slow.limit_up_stocks.side_effect = slow_fail
    start = NOW + timedelta(seconds=60)
    read_intraday(tmp_path, now=start, client=slow)
    assert slow.limit_up_stocks.call_count == 1
    snap = read_intraday(tmp_path, now=start + timedelta(seconds=45), client=slow)
    assert slow.limit_up_stocks.call_count == 1 and snap["stale_seconds"] == 105


def test_corrupt_previous_expression_is_ignored(tmp_path):
    prev = _day_dir(tmp_path, "2026-11-16")
    prev.mkdir(parents=True)
    (prev / "expression.json").write_text("{broken", encoding="utf-8")
    assert read_intraday(tmp_path, now=NOW, client=fake_client())["prev_expression"] is None
