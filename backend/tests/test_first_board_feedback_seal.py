"""昨日首板反馈 × 昨日封板质量: 每行附昨日首封/开板/封单额, 按封板质量分层看今日表现。

数据只读本地涨停池归档 (market_reference/v1/limit_events), 不新增上游调用。
"""

from datetime import date, datetime
from types import SimpleNamespace

import polars as pl
import pytest

from app.data_providers.schemas import LIMIT_EVENT_SCHEMA
from app.market_time import CN_TZ
from app.services import market_reference
from app.services.first_board_feedback import FirstBoardFeedback

DAY = date(2026, 10, 26)
PREVIOUS = date(2026, 10, 23)
BEFORE = date(2026, 10, 22)
WARMUP = date(2026, 10, 21)
NOW = datetime(2026, 10, 26, 10, 0, tzinfo=CN_TZ)
SYMBOLS = ("600000.SH", "600001.SH", "300001.SZ", "300002.SZ")
CHANGE = {"600000.SH": .10, "600001.SH": .04, "300001.SZ": -.05, "300002.SZ": -.01}


def _history():
    rows = []
    for symbol in SYMBOLS:
        rows.append({"symbol": symbol, "date": BEFORE, "name": "测试股票", "raw_close": 10.0, "limit_up_price": 10.995})
        rows.append({"symbol": symbol, "date": PREVIOUS, "name": "测试股票", "raw_close": 11.0, "limit_up_price": 10.995})
    return pl.DataFrame(rows)


def _quotes():
    stamp = int(NOW.timestamp() * 1000)
    return pl.DataFrame([{"symbol": s, "date": DAY, "raw_close": 11.0 * (1 + c), "close": 11.0 * (1 + c),
                          "open": 11.0, "change_pct": c, "limit_up_price": 12.1, "quote_ts": stamp}
                         for s, c in CHANGE.items()])


def _pool(data_dir, rows):
    full = []
    for r in rows:
        base = {k: None for k in LIMIT_EVENT_SCHEMA}
        base.update({"date": PREVIOUS, "name": "测试股票", "type": "u", "is_break": False, "source": "meoz",
                     "amount": 5e8})
        base.update(r)
        full.append(base)
    out = market_reference.path_of(data_dir, "limit_events", PREVIOUS)
    out.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(full, schema=LIMIT_EVENT_SCHEMA).write_parquet(out)


@pytest.fixture
def service(tmp_path, monkeypatch):
    repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        get_first_board_history_signature=lambda *_: "v1",
        get_first_board_history=lambda *_a, **_k: (_history(), "v1"),
        get_index_daily=lambda *_a, **_k: pl.DataFrame({"date": [WARMUP, BEFORE, PREVIOUS]}),
    )
    svc = FirstBoardFeedback(repo)
    monkeypatch.setattr(svc._membership, "prepare", lambda **_: None)
    svc._membership._membership = ([], "members-v1", None)
    return svc, tmp_path


def _rows(result):
    return {r["symbol"]: r for r in result["rows"]}


def _seal_rows():
    return [
        {"symbol": "600000.SH", "first_time": "09:31:00", "last_time": "09:31:00", "open_times": 0, "fd_amount": 8e7},
        {"symbol": "600001.SH", "first_time": "09:50:00", "last_time": "13:40:00", "open_times": 2, "fd_amount": 3e7},
        {"symbol": "300001.SZ", "first_time": "14:20:00", "last_time": "14:55:00", "open_times": None, "fd_amount": 1e7},
        {"symbol": "300002.SZ", "first_time": "10:30:00", "last_time": "10:30:00", "open_times": 0, "fd_amount": 2e7},
    ]


def test_rows_carry_yesterday_seal_facts(service):
    svc, data_dir = service
    _pool(data_dir, _seal_rows())
    rows = _rows(svc.evaluate(_quotes(), DAY, now=NOW, live=True))
    assert rows["600000.SH"]["seal"] == {"first_time": "09:31:00", "opened": False, "fd_amount": 8e7}
    assert rows["600001.SH"]["seal"]["opened"] is True
    # open_times 缺失: 按首封 != 末封判断开过板 (与封板质量同口径)
    assert rows["300001.SZ"]["seal"]["opened"] is True


def test_layers_split_today_performance_by_yesterday_seal_quality(service):
    svc, data_dir = service
    _pool(data_dir, _seal_rows())
    layers = svc.evaluate(_quotes(), DAY, now=NOW, live=True)["layers"]["all"]
    assert layers["early"]["sample_count"] == 2            # 600000 09:31, 600001 09:50
    assert layers["early"]["mean_change_pct"] == pytest.approx(.07)
    assert layers["late"]["sample_count"] == 2             # 300001 14:20, 300002 10:30
    assert layers["late"]["mean_change_pct"] == pytest.approx(-.03)
    assert layers["unopened"]["sample_count"] == 2         # 600000, 300002
    assert layers["reopened"]["sample_count"] == 2         # 600001, 300001(缺失按首末封)
    assert layers["early"]["up_ratio"] == 1.0
    assert layers["early"]["limit_up_count"] == 1          # 600000 今日 +10% 收在涨停价 12.1


def test_layers_follow_board_scope(service):
    svc, data_dir = service
    _pool(data_dir, _seal_rows())
    layers = svc.evaluate(_quotes(), DAY, now=NOW, live=True)["layers"]
    assert layers["main"]["early"]["sample_count"] == 2
    assert layers["growth"]["early"]["sample_count"] == 0
    assert layers["growth"]["early"]["mean_change_pct"] is None


def test_missing_archive_gives_null_seal_and_no_layers(service):
    svc, _ = service
    result = svc.evaluate(_quotes(), DAY, now=NOW, live=True)
    assert all(r["seal"] is None for r in result["rows"])
    assert result["layers"] is None
    assert result["seal_source"] is None


def test_stock_absent_from_archive_has_null_seal_and_is_left_out_of_layers(service):
    svc, data_dir = service
    _pool(data_dir, _seal_rows()[:3])
    result = svc.evaluate(_quotes(), DAY, now=NOW, live=True)
    assert _rows(result)["300002.SZ"]["seal"] is None
    layers = result["layers"]["all"]
    assert layers["late"]["sample_count"] + layers["early"]["sample_count"] == 3
    assert result["seal_source"] == {"date": str(PREVIOUS), "matched": 3, "sample": 4}


def test_fd_amount_blank_when_unit_suspect(service):
    # 实测 2020-05~2023-11 上游封单额口径不同 (封单额/成交额中位约 0.02%): 不给封单额
    svc, data_dir = service
    _pool(data_dir, [{**r, "fd_amount": 5e4} for r in _seal_rows()])
    rows = _rows(svc.evaluate(_quotes(), DAY, now=NOW, live=True))
    assert all(r["seal"]["fd_amount"] is None for r in rows.values())
    assert rows["600000.SH"]["seal"]["first_time"] == "09:31:00"


def test_opened_unknown_before_2020(tmp_path):
    # 上游开板次数 2020-01-02 前为字段缺失 (记 0 或 null), 不能据此判「未开板」
    from app.services.first_board_feedback import load_yesterday_seal

    day = date(2019, 12, 10)
    base = {k: None for k in LIMIT_EVENT_SCHEMA}
    base.update({"date": day, "symbol": "600000.SH", "name": "测试", "type": "u", "is_break": False,
                 "source": "meoz", "first_time": "09:40:00", "last_time": "13:00:00", "open_times": 0,
                 "fd_amount": 8e7, "amount": 5e8})
    out = market_reference.path_of(tmp_path, "limit_events", day)
    out.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame([base], schema=LIMIT_EVENT_SCHEMA).write_parquet(out)
    assert load_yesterday_seal(tmp_path, day)["600000.SH"]["opened"] is None


def test_u_row_wins_over_ub_row(service):
    # 同一股同时有 u 与 ub 记录时以 u 为准, 无论在文件中的先后
    svc, data_dir = service
    rows = [{"symbol": "600000.SH", "type": "ub", "first_time": "14:59:00", "last_time": "14:59:00",
             "open_times": 3, "fd_amount": None}, *_seal_rows()]
    _pool(data_dir, rows)
    assert _rows(svc.evaluate(_quotes(), DAY, now=NOW, live=True))["600000.SH"]["seal"]["first_time"] == "09:31:00"


def test_tail_resealed_first_board_tagged_ub_gets_seal(service):
    # 终审实测: 猫爪 type 是收盘竞价前状态, 尾盘回封的首板被标为 ub (近 60 日 19 只);
    # 反馈样本按收盘封住选出, 这些股也应拿到封板信息, 且一定开过板
    svc, data_dir = service
    rows = _seal_rows()
    rows[3] = {"symbol": "300002.SZ", "type": "ub", "first_time": "10:30:00", "last_time": "14:58:00",
               "open_times": 2, "fd_amount": 2e7}
    _pool(data_dir, rows)
    result = svc.evaluate(_quotes(), DAY, now=NOW, live=True)
    seal = _rows(result)["300002.SZ"]["seal"]
    assert seal["first_time"] == "10:30:00" and seal["opened"] is True
    assert result["seal_source"]["matched"] == 4


def test_optional_seal_failure_does_not_take_down_feedback(service, monkeypatch):
    # 封板信息是可选展示: 读取出现意外异常时只置空, 反馈主体照常
    import app.services.first_board_feedback as fbf

    svc, data_dir = service
    _pool(data_dir, _seal_rows())
    monkeypatch.setattr(fbf, "load_yesterday_seal", lambda *_: (_ for _ in ()).throw(TypeError("bad")))
    result = svc.evaluate(_quotes(), DAY, now=NOW, live=True)
    assert result["status"] in ("ready", "partial")
    assert result["layers"] is None and all(r["seal"] is None for r in result["rows"])
