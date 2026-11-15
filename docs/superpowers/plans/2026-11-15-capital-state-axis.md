# 市场环境「资金状态轴」实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给 `regime_history` 的每个交易日加上独立的资金状态（总量档位、集中度档位、承接标记），在市场环境页展示，并作为影子分组进入离线校准。

**Architecture:** 新增纯函数模块 `market_capital.py`（聚合表达式、单行整理、全序列标注），结构仿照 `market_phase.py`。`regime_builder` 在日聚合时多算两列，并在每次写入后对全量序列重标资金派生列。校准脚本和环境页只读这些列。

**Tech Stack:** Python 3.11、Polars、pytest（`uv run`）；React 18、TypeScript、ECharts、vitest（`pnpm exec`）。

**Spec:** `docs/superpowers/specs/2026-11-15-capital-state-axis-design.md`

## Global Constraints

- 现有 `score`、`state`、`phase` 的任何数值不得变化；资金列不得进入首板过滤、仓位闸门、回测环境过滤或挖掘。
- 无未来函数：任一日的派生值只用当日及之前的行；分位参考窗口不含当日。
- 单位：成交额为人民币元；`amount_top5_share`、`amount_change_5d` 为小数制；`amount_multiple` 无单位；分位为 0–100。
- 窗口按 `regime_history` 的行（实际交易日）计，不用自然日。
- 任一输入缺失则对应输出为 null，不补零、不猜测。
- 旧 `regime_history`（缺新列）必须可读，页面显示"未计算"。
- 文案不得把成交额称为"资金流入"或"增量资金"。
- 前端命令只用 `pnpm` / `pnpm exec`，不用 `npx`。工作区为 CRLF，脚本改写文件须保留 `\r\n`。
- 提交使用 `git commit -s`（项目要求 DCO）。只有用户同意提交时才执行各任务的提交步骤。
- 本机跑后端测试如遇 Polars CPU 检查报错，临时设置 `POLARS_SKIP_CPU_CHECK=1`；`uv` 缓存不可写时把 `UV_CACHE_DIR` 指向临时目录。不改应用配置。

## Review Focus

1. 某日行情同步不完整（有效股票数骤降）：该日及以它为基准的日子不应得出"缩量"，应为 null。→ Task 1 `test_incomplete_day_is_not_comparable`。
2. 历史不足 60 个有效样本的开头一段：档位应为 null，而不是按少量样本定档。→ Task 1 `test_percentile_needs_60_samples`。
3. 增量 upsert 之后旧日期的资金标签丢失（upsert 以新行的列为准对齐旧行）。→ Task 2 `test_incremental_upsert_keeps_capital_labels`。
4. 用户尚未重算、最新一行没有资金列：环境页不应显示 0 或"缩量"。→ Task 4 "未计算"测试。
5. 成交额全天相同或大量并列：分位应取中位秩（50），不应落到 0 或 100 触发档位。→ Task 1 `test_ties_use_mid_rank`。

---

### Task 1: 资金状态纯函数模块

**Files:**
- Create: `backend/app/services/market_capital.py`
- Test: `backend/tests/test_market_capital.py`

**Interfaces:**
- Consumes: 无。
- Produces（后续任务按这些名字引用）:

```python
VOLUME_EXPAND, VOLUME_FLAT, VOLUME_SHRINK = "expand", "flat", "shrink"
CONC_CONCENTRATED, CONC_NEUTRAL, CONC_DISPERSED = "concentrated", "neutral", "dispersed"
FLAG_VOLUME_STALL, FLAG_SHRINK_ADVANCE = "volume_stall", "shrink_advance"

TOP_SHARE = 0.05            # 前 5% 个股
TOP_MIN_STOCKS = 100        # 有效股票少于此数不算集中度
BASELINE_DAYS = 20
BASELINE_MIN_COMPARABLE = 15
COUNT_TOLERANCE = 0.05      # 有效股票数相差不超过 5% 才可比
CHANGE_DAYS = 5
PCTL_WINDOW = 252
PCTL_MIN_SAMPLES = 60
PCTL_HIGH, PCTL_LOW = 75, 25

AGG_COLUMNS = ("amount_valid_count", "amount_top5_share")
DERIVED_COLUMNS = ("amount_multiple", "amount_change_5d", "amount_multiple_pctl",
                   "top5_share_pctl", "capital_volume", "capital_concentration", "capital_flag")
REQUIRED_COLUMNS = frozenset({"date", "total_amount", "median_pct", *AGG_COLUMNS})

def with_amount_rank(df: pl.DataFrame) -> pl.DataFrame
def capital_daily_aggs() -> list[pl.Expr]
def finalize_capital_row(r: dict) -> dict
def classify_capital_series(daily: pl.DataFrame) -> pl.DataFrame
```

行为约定：

- `with_amount_rank`：追加 `_amount_rank` 列——同一 `date` 内、`amount > 0` 的行按成交额降序的序数排名（`rank("ordinal", descending=True).over("date")`），其余行为 null。已有该列时原样返回。
- `capital_daily_aggs`：供 `group_by("date").agg(...)` 拼接，要求 df 含 `amount` 与 `_amount_rank`。产出 `amount_valid_count`（`amount > 0` 的行数）、`_amount_valid_sum`、`_amount_top5_sum`（排名 ≤ `ceil(amount_valid_count × TOP_SHARE)` 的成交额之和）。
- `finalize_capital_row`：返回 `{"amount_valid_count": int, "amount_top5_share": float | None}`。有效数 < `TOP_MIN_STOCKS` 或有效成交额之和 ≤ 0 时占比为 None；否则四舍五入到 6 位。
- `classify_capital_series`：输入缺 `REQUIRED_COLUMNS` 中任一列时抛 `ValueError`。按 `date` 升序返回，追加（或覆盖）`DERIVED_COLUMNS`，其余列原样保留。规则：
  - **可比**：日 j 相对日 i 可比，当且仅当两日 `amount_valid_count` 均非空且 > 0、日 j 的 `total_amount > 0`、`|count_j / count_i − 1| ≤ COUNT_TOLERANCE`。
  - `amount_multiple`：日 i 的 `total_amount`（须 > 0）÷ 此前 `BASELINE_DAYS` 行中可比日成交额的中位数；可比日不足 `BASELINE_MIN_COMPARABLE` 为 null。保留 4 位。
  - `amount_change_5d`：含当日在内最近 10 行都相对当日可比时，后 5 行均值 ÷ 前 5 行均值 − 1；否则 null。保留 4 位。
  - 分位：某列当日值相对**此前**最多 `PCTL_WINDOW` 个非空值，`(小于的个数 + 0.5 × 相等的个数) ÷ n × 100`；n < `PCTL_MIN_SAMPLES` 为 null。保留 1 位。
  - `capital_volume`：`amount_multiple_pctl ≥ 75` 为 expand，`≤ 25` 为 shrink，其余 flat；分位为 null 则为 null。`capital_concentration` 对 `top5_share_pctl` 同理。
  - `capital_flag`：expand 且 `median_pct ≤ 0` 为 volume_stall；shrink 且 `median_pct > 0` 为 shrink_advance；其余或输入为 null 时为 null。

- [ ] **Step 1: 写失败测试**

`backend/tests/test_market_capital.py`，先放两个辅助函数再写用例：

```python
def _series(amounts, *, counts=None, medians=None, shares=None):
    """从 2024-01-01 起每个工作日一行。"""
    n = len(amounts)
    days, cur = [], date(2024, 1, 1)
    while len(days) < n:
        if cur.weekday() < 5:
            days.append(cur)
        cur += timedelta(days=1)
    return pl.DataFrame({
        "date": days, "total_amount": [float(a) for a in amounts],
        "amount_valid_count": counts or [5000] * n,
        "median_pct": medians or [0.001] * n,
        "amount_top5_share": shares or [0.40] * n,
    })

def _last(df, col):
    return df[col][-1]
```

用例与断言：

```python
def test_top5_share_from_daily_aggs():
    # 200 只股票成交额 1..200，另有 1 只零成交、1 只空值
    amounts = [float(i) for i in range(1, 201)] + [0.0, None]
    df = pl.DataFrame({"date": [date(2024, 1, 2)] * 202, "amount": amounts})
    row = with_amount_rank(df).group_by("date").agg(*capital_daily_aggs()).to_dicts()[0]
    out = finalize_capital_row(row)
    assert out["amount_valid_count"] == 200
    assert out["amount_top5_share"] == round(1955 / 20100, 6)   # 前 10 只: 191..200

def test_top5_count_rounds_up():
    # 101 只 → 前 ceil(5.05)=6 只
    df = pl.DataFrame({"date": [date(2024, 1, 2)] * 101, "amount": [float(i) for i in range(1, 102)]})
    row = with_amount_rank(df).group_by("date").agg(*capital_daily_aggs()).to_dicts()[0]
    assert finalize_capital_row(row)["amount_top5_share"] == round(sum(range(96, 102)) / sum(range(1, 102)), 6)

def test_top5_share_null_below_100_stocks():
    df = pl.DataFrame({"date": [date(2024, 1, 2)] * 99, "amount": [1.0] * 99})
    row = with_amount_rank(df).group_by("date").agg(*capital_daily_aggs()).to_dicts()[0]
    assert finalize_capital_row(row) == {"amount_valid_count": 99, "amount_top5_share": None}

def test_multiple_uses_median_of_prior_20_days():
    out = classify_capital_series(_series([100.0] * 20 + [150.0]))
    assert _last(out, "amount_multiple") == 1.5
    assert out["amount_multiple"][14] is None          # 此前只有 14 个可比日
    assert out["amount_multiple"][15] == 1.0           # 满 15 个即可计算

def test_incomplete_day_is_not_comparable():
    # 第 21 天有效股票数骤降 20%: 当日无可比基准; 之后的日子把它排除
    counts = [5000] * 20 + [4000] + [5000] * 5
    out = classify_capital_series(_series([100.0] * 20 + [60.0] + [100.0] * 5, counts=counts))
    assert out["amount_multiple"][20] is None
    assert out["capital_volume"][20] is None
    assert out["amount_multiple"][25] == 1.0            # 20 行里 19 个可比, 中位数仍为 100

def test_multiple_null_when_fewer_than_15_comparable():
    counts = [4000] * 6 + [5000] * 15                   # 最后一天前 20 行只有 14 个可比
    assert _last(classify_capital_series(_series([100.0] * 21, counts=counts)), "amount_multiple") is None

def test_change_5d():
    out = classify_capital_series(_series([100.0] * 5 + [110.0] * 5))
    assert _last(out, "amount_change_5d") == 0.1
    assert out["amount_change_5d"][8] is None           # 不足 10 行

def test_percentile_needs_60_samples():
    amounts = [100.0 + i for i in range(76)]            # 倍数自下标 15 起有值
    out = classify_capital_series(_series(amounts))
    assert out["amount_multiple_pctl"][74] is None      # 此前只有 59 个有效倍数
    assert out["amount_multiple_pctl"][75] is not None

def test_ties_use_mid_rank():
    out = classify_capital_series(_series([100.0] * 120))
    assert _last(out, "amount_multiple_pctl") == 50.0
    assert _last(out, "capital_volume") == VOLUME_FLAT
    assert _last(out, "capital_concentration") == CONC_NEUTRAL

def test_volume_and_flag_labels():
    base = [100.0 + (i % 7) for i in range(120)]        # 倍数有小幅波动
    stall = classify_capital_series(_series(base + [200.0], medians=[0.001] * 120 + [-0.002]))
    assert _last(stall, "capital_volume") == VOLUME_EXPAND
    assert _last(stall, "capital_flag") == FLAG_VOLUME_STALL
    advance = classify_capital_series(_series(base + [200.0]))
    assert _last(advance, "capital_flag") is None
    shrink = classify_capital_series(_series(base + [50.0]))
    assert _last(shrink, "capital_volume") == VOLUME_SHRINK
    assert _last(shrink, "capital_flag") == FLAG_SHRINK_ADVANCE

def test_concentration_labels():
    shares = [0.40 + 0.001 * (i % 7) for i in range(120)]
    assert _last(classify_capital_series(_series([100.0] * 121, shares=shares + [0.60])), "capital_concentration") == CONC_CONCENTRATED
    assert _last(classify_capital_series(_series([100.0] * 121, shares=shares + [0.20])), "capital_concentration") == CONC_DISPERSED
    assert _last(classify_capital_series(_series([100.0] * 121, shares=shares + [None])), "capital_concentration") is None

def test_later_rows_never_change_earlier_labels():
    amounts = [100.0 + (i * 37 % 23) for i in range(200)]
    full = classify_capital_series(_series(amounts))
    head = classify_capital_series(_series(amounts[:150]))
    assert_frame_equal(full.head(150), head)

def test_missing_required_column_raises():
    with pytest.raises(ValueError):
        classify_capital_series(_series([100.0] * 5).drop("amount_valid_count"))

def test_null_count_rows_get_null_outputs_and_keep_other_columns():
    df = _series([100.0] * 30, counts=[None] * 30).with_columns(pl.lit("keep").alias("extra"))
    out = classify_capital_series(df)
    assert out["extra"].to_list() == ["keep"] * 30
    assert all(out[c].null_count() == 30 for c in DERIVED_COLUMNS)
```

- [ ] **Step 2: 运行，确认失败**

Run: `cd backend && uv run pytest tests/test_market_capital.py -q`
Expected: 收集阶段报 `ModuleNotFoundError: app.services.market_capital`。

- [ ] **Step 3: 实现 `market_capital.py`**

按上方 Interfaces 的常量、签名和行为约定实现。模块头注释写明口径（单位、可比规则、阈值为研究初始值未经校准、成交额不等于资金流入）。序列标注部分行数为千级，逐日 Python 循环即可（与 `classify_phase_series` 一致）；日聚合部分必须是 Polars 表达式，不得逐行循环。

- [ ] **Step 4: 运行，确认通过**

Run: `cd backend && uv run pytest tests/test_market_capital.py -q && uv run ruff check app/services/market_capital.py tests/test_market_capital.py`
Expected: 14 passed；Ruff 无告警。

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/market_capital.py backend/tests/test_market_capital.py
git commit -s -m "feat(regime): 资金状态纯函数(总量/集中度/承接标记)"
```

---

### Task 2: 接入环境历史的计算与持久化

**Files:**
- Modify: `backend/app/services/regime_builder.py`（`_aggregate_daily` 约 158–332 行；`refresh_phase_labels` 之后新增函数；`compute_regime_incremental` 约 706 行）
- Modify: `backend/app/api/regime.py:163`
- Modify: `backend/app/services/repair_daily.py:200`
- Create: `docs/market-capital.md`
- Modify: `docs/data-directory.md:108`
- Test: `backend/tests/test_regime_builder.py`

**Interfaces:**
- Consumes: Task 1 的 `with_amount_rank`、`capital_daily_aggs`、`finalize_capital_row`、`classify_capital_series`、`REQUIRED_COLUMNS`、`DERIVED_COLUMNS`。
- Produces: `regime_builder.refresh_capital_labels(data_dir: Path, *, start: date | None = None) -> int`；`regime_history` 新增 `AGG_COLUMNS` 与 `DERIVED_COLUMNS` 共 9 列；`POST /api/regime/recompute` 响应新增 `capital_days: int`。

- [ ] **Step 1: 写失败测试**（追加到 `backend/tests/test_regime_builder.py`）

```python
def _capital_enriched(n_symbols=200):
    return pl.DataFrame({
        "symbol": [f"{i:06d}.SZ" for i in range(n_symbols)],
        "date": [date(2024, 1, 2)] * n_symbols,
        "change_pct": [0.01] * n_symbols,
        "amount": [float(i) for i in range(1, n_symbols + 1)],
        "close": [10.0] * n_symbols, "ma20": [9.5] * n_symbols,
        "signal_limit_up": [False] * n_symbols, "signal_limit_down": [False] * n_symbols,
        "signal_broken_limit_up": [False] * n_symbols,
    })

def test_aggregate_daily_adds_capital_columns():
    row = regime_builder._aggregate_daily(_capital_enriched()).to_dicts()[0]
    assert row["amount_valid_count"] == 200
    assert row["amount_top5_share"] == round(1955 / 20100, 6)
    assert row["total_amount"] == 20100.0

def test_aggregate_daily_without_amount_column_keeps_capital_null():
    row = regime_builder._aggregate_daily(_capital_enriched().drop("amount")).to_dicts()[0]
    assert row["amount_valid_count"] == 0 and row["amount_top5_share"] is None

def _history(n, **extra):
    days, cur = [], date(2024, 1, 1)
    while len(days) < n:
        if cur.weekday() < 5:
            days.append(cur)
        cur += timedelta(days=1)
    return pl.DataFrame({
        "date": days, "state": ["range"] * n, "score": [50] * n,
        "total_amount": [100.0 + (i % 7) for i in range(n)], "median_pct": [0.001] * n,
        "amount_valid_count": [5000] * n, "amount_top5_share": [0.4] * n, **extra,
    })

def test_refresh_capital_labels_roundtrip(tmp_path):
    regime_builder.upsert_regime_history(tmp_path, _history(120))
    assert regime_builder.refresh_capital_labels(tmp_path) == 120
    out = regime_builder.load_regime_history(tmp_path)
    assert out["capital_volume"][-1] in {"expand", "flat", "shrink"}
    assert_frame_equal(out.select("date", "state", "score", "total_amount"),
                       _history(120).select("date", "state", "score", "total_amount"))

def test_refresh_capital_labels_skips_old_schema(tmp_path):
    regime_builder.upsert_regime_history(tmp_path, _history(120).drop("amount_valid_count", "amount_top5_share"))
    before = regime_builder.regime_path(tmp_path).stat().st_mtime_ns
    assert regime_builder.refresh_capital_labels(tmp_path) == 0
    assert regime_builder.regime_path(tmp_path).stat().st_mtime_ns == before

def test_incremental_upsert_keeps_capital_labels(tmp_path):
    full = _history(121)
    regime_builder.upsert_regime_history(tmp_path, full.head(120))
    regime_builder.refresh_capital_labels(tmp_path)
    regime_builder.upsert_regime_history(tmp_path, full.tail(1))   # 新行不含派生列
    regime_builder.refresh_capital_labels(tmp_path)
    out = regime_builder.load_regime_history(tmp_path)
    assert out.height == 121
    assert out["capital_volume"].head(120).null_count() < 120      # 旧日期标签仍在
    assert out["capital_volume"][-1] is not None

def test_refresh_capital_labels_with_start_keeps_earlier_rows(tmp_path):
    regime_builder.upsert_regime_history(tmp_path, _history(120))
    regime_builder.refresh_capital_labels(tmp_path)
    before = regime_builder.load_regime_history(tmp_path)
    cut = before["date"][100]
    assert regime_builder.refresh_capital_labels(tmp_path, start=cut) == 120
    assert_frame_equal(regime_builder.load_regime_history(tmp_path), before)
```

文件顶部补 `from datetime import timedelta`。

- [ ] **Step 2: 运行，确认失败**

Run: `cd backend && uv run pytest tests/test_regime_builder.py -q -k "capital"`
Expected: 6 个用例失败（`KeyError: 'amount_valid_count'` 或 `AttributeError: refresh_capital_labels`）。

- [ ] **Step 3: 修改 `_aggregate_daily`**

`amount` 在 `avail` 中时，聚合前调用 `with_amount_rank(df)`，在 `group_by("date").agg(...)` 里拼接 `capital_daily_aggs()`；持久化行字典末尾加 `**finalize_capital_row(r)`。`amount` 缺失时不加聚合，`finalize_capital_row(r)` 对缺键返回 `{"amount_valid_count": 0, "amount_top5_share": None}`。不得改动 `metrics`、`classify_state`、`_compute_subscores` 的任何输入。

- [ ] **Step 4: 新增 `refresh_capital_labels` 并接到三处调用点**

`refresh_capital_labels(data_dir, *, start=None) -> int` 紧跟在 `refresh_phase_labels` 后定义，行为与它对称：历史为空或缺 `REQUIRED_COLUMNS` 时返回 0 且不写文件；`classify_capital_series` 抛异常时记 warning 返回 0；`start` 非空时只替换 `date >= start` 的行（标注本身仍用全量序列计算）；用 `atomic_write_parquet` 写回；返回行数。

在以下三处、紧接 `refresh_phase_labels(...)` 之后调用，参数与之相同：

- `regime_builder.compute_regime_incremental`
- `api/regime.py` 的 `regime_recompute`，并把返回值放进响应的 `capital_days`
- `repair_daily.py`（带 `start=start`）

- [ ] **Step 5: 运行受影响模块的全部测试**

Run: `cd backend && uv run pytest tests/test_regime_builder.py tests/test_market_phase.py tests/test_regime_state.py tests/test_regime_score_non_finite.py tests/test_market_capital.py -q`
Expected: 全部通过，包含改动前已有的用例（它们断言了 `score`/`state` 的具体数值，是"现有结果不变"的证据）。

再跑：`uv run pytest tests -q -k "regime or repair_daily or first_board_context or trade_journal"`
Expected: 全部通过。

- [ ] **Step 6: 写文档**

- 新建 `docs/market-capital.md`：功能边界、9 列的口径与单位、可比规则、阈值表（标明研究初始值、未经校准）、回填方式（环境页点"重算"）、已知限制（成交额不等于资金流入；有效股票数可比只是数量近似，不证明同池）、测试文件位置。体例参照 `docs/market-phase.md`。
- `docs/data-directory.md:108` 的 `regime_history/` 一行，在内容描述里加上"成交额倍数、成交集中度与资金档位"，并指向 `market_capital.py`。

- [ ] **Step 7: Ruff 与提交**

Run: `cd backend && uv run ruff check app/services/regime_builder.py app/services/market_capital.py app/api/regime.py app/services/repair_daily.py tests/test_regime_builder.py`
Expected: 本次改动不新增告警。

```bash
git add backend/app/services/regime_builder.py backend/app/api/regime.py backend/app/services/repair_daily.py backend/tests/test_regime_builder.py docs/market-capital.md docs/data-directory.md
git commit -s -m "feat(regime): 环境历史新增资金状态列并随写入重标"
```

---

### Task 3: 校准脚本的资金影子分组

**Files:**
- Modify: `backend/app/services/market_game_calibration.py`（`prepare_history` 约 84–122 行、`SHADOW_DEFINITIONS` 约 139 行、`_shadow_keys` 约 155 行）
- Modify: `docs/market-game.md`（"规则可信度"分组表）
- Test: `backend/tests/test_market_game_calibration.py`

**Interfaces:**
- Consumes: `regime_history` 的 `capital_volume`、`capital_concentration`、`capital_flag` 列（Task 2），取值见 Task 1 常量。
- Produces: 六个分组键 `shadow:capital_expand` / `shadow:capital_shrink`、`shadow:capital_concentrated` / `shadow:capital_dispersed`、`shadow:capital_volume_stall` / `shadow:capital_expand_advance`。

- [ ] **Step 1: 写失败测试**（追加）

```python
def _capital(frame, day_index, **cols):
    target = frame['date'][day_index]
    return frame.with_columns(*[
        pl.when(pl.col('date') == target).then(pl.lit(value)).otherwise(pl.lit(None, dtype=pl.Utf8)).alias(name)
        for name, value in cols.items()])

def test_capital_shadow_groups_follow_regime_labels():
    frame = _capital(history(10), 5, capital_volume='expand', capital_concentration='concentrated')
    assert _shadow(calibrate_history(frame))[5] == [
        'shadow:capital_expand', 'shadow:capital_concentrated', 'shadow:capital_expand_advance']
    stall = _capital(history(10), 5, capital_volume='expand', capital_flag='volume_stall')
    assert _shadow(calibrate_history(stall))[5] == ['shadow:capital_expand', 'shadow:capital_volume_stall']
    weak = _capital(history(10), 5, capital_volume='shrink', capital_concentration='dispersed')
    assert _shadow(calibrate_history(weak))[5] == ['shadow:capital_shrink', 'shadow:capital_dispersed']
    flat = _capital(history(10), 5, capital_volume='flat', capital_concentration='neutral')
    assert _shadow(calibrate_history(flat))[5] == []

def test_capital_shadow_absent_on_old_schema_and_keeps_other_groups():
    old = calibrate_history(history(30))
    new = calibrate_history(_capital(history(30), 5, capital_volume='expand'))
    assert all(not any(key.startswith('shadow:capital') for key in keys) for keys in _shadow(old))
    strip = lambda result: {g['group']: g for g in result['groups'] if not g['group'].startswith('shadow:capital')}
    assert strip(old) == strip(new)
    assert old['coverage'] == new['coverage']
```

`history()` 的 `median_pct` 为 `.01`，所以 expand 且无标记的那一天归入 `capital_expand_advance`。写用例前先读 `calibrate_history` 的返回结构，按真实的键名（`groups`、`coverage` 等）调整 `strip` 和断言。

同时把现有 `test_shadow_groups_split_and_publish_fixed_definitions` 里 `shadow_definitions` 的期望集合，扩成原 3 个加上 `shadow:capital_expand`、`shadow:capital_concentrated`、`shadow:capital_volume_stall`。

- [ ] **Step 2: 运行，确认失败**

Run: `cd backend && uv run pytest tests/test_market_game_calibration.py -q`
Expected: 新增 2 个用例与被修改的 1 个用例失败，其余通过。

- [ ] **Step 3: 实现**

- `prepare_history`：每行在 `_metrics` 之外追加 `capital_volume`、`capital_concentration`、`capital_flag`（用 `row.get`，旧 schema 为 None）。不得放进 `_metrics`，否则会改变 `complete_inputs` 和 `coverage`。
- `_shadow_keys`：在现有三段之后按"总量 → 集中度 → 承接"的顺序追加，不限趋势：
  - `capital_volume` 为 expand / shrink → `shadow:capital_expand` / `shadow:capital_shrink`
  - `capital_concentration` 为 concentrated / dispersed → `shadow:capital_concentrated` / `shadow:capital_dispersed`
  - `capital_flag` 为 volume_stall → `shadow:capital_volume_stall`；`capital_volume` 为 expand 且 `median_return` 非空且 > 0 → `shadow:capital_expand_advance`
- `SHADOW_DEFINITIONS` 追加三条，`card` 统一为 `'资金状态轴'`：

| group | control | label | definition |
|---|---|---|---|
| `shadow:capital_expand` | `shadow:capital_shrink` | 资金放量 | 成交额倍数（对前20日中位数）相对此前252日分位≥75；对照组为分位≤25 |
| `shadow:capital_concentrated` | `shadow:capital_dispersed` | 成交集中 | 成交额前5%个股占比相对此前252日分位≥75；对照组为分位≤25 |
| `shadow:capital_volume_stall` | `shadow:capital_expand_advance` | 放量滞涨（资金轴） | 资金放量且中位涨跌幅≤0；对照组为资金放量且中位涨跌幅>0 |

- [ ] **Step 4: 运行，确认通过**

Run: `cd backend && uv run pytest tests/test_market_game_calibration.py -q && uv run pytest tests -q -k "market_game and (calibration or credibility)"`
Expected: 全部通过。若可信度归档相关测试断言了影子行数量，按新增 3 行更新，并确认旧归档（只有 3 个影子分组）仍可读取。

- [ ] **Step 5: 更新文档并提交**

`docs/market-game.md` 的分组表追加上面三行，并在表后那段多重比较说明里补一句：资金三组使用滚动分位阈值，为研究初始值，与原三组同样不进入规则、报告或仓位。

```bash
git add backend/app/services/market_game_calibration.py backend/tests/test_market_game_calibration.py docs/market-game.md
git commit -s -m "feat(market-game): 校准新增资金状态影子分组"
```

---

### Task 4: 市场环境页展示

**Files:**
- Modify: `frontend/src/lib/api.ts`（`RegimeRow`，约 727–762 行）
- Modify: `frontend/src/pages/Regime.tsx`（趋势图 option 约 489–560 行；情绪周期页签约 739–802 行）
- Test: `frontend/src/pages/Regime.test.tsx`

**Interfaces:**
- Consumes: `GET /api/regime/history` 透传的 9 个新列（后端无需改接口）。
- Produces: `RegimeRow` 新增可选字段：

```ts
amount_valid_count?: number | null
amount_top5_share?: number | null      // 小数制
amount_multiple?: number | null
amount_change_5d?: number | null       // 小数制
amount_multiple_pctl?: number | null   // 0–100
top5_share_pctl?: number | null        // 0–100
capital_volume?: 'expand' | 'flat' | 'shrink' | null
capital_concentration?: 'concentrated' | 'neutral' | 'dispersed' | null
capital_flag?: 'volume_stall' | 'shrink_advance' | null
```

固定文案：

| 取值 | 显示 |
|---|---|
| expand / flat / shrink | 放量 / 平量 / 缩量 |
| concentrated / neutral / dispersed | 集中 / 中性 / 分散 |
| volume_stall / shrink_advance | 放量滞涨 / 缩量上涨 |

资金卡（`<section aria-label="资金状态">`，放在情绪周期页签内、阶段概览块之后，不依赖 `phaseStreak`，只要 `latest` 存在就渲染）：

```
资金  2026-10-30
放量 · 集中            放量滞涨
成交额 1.32 倍（20日中位） · 5日 +8.4%
前5%个股占成交 41.2%
```

- `capital_volume` 与 `capital_concentration` 都为空：整张卡只显示标题和一句"未计算，点击右上角「重算」补全"，不显示任何数值。
- 只有其中一项为空：该项显示"—"，其余照常。数值为空显示"—"。
- 倍数保留 2 位；5 日变化带正负号、1 位小数的百分数；占比 1 位小数的百分数。
- 承接标记为空时不渲染该元素。鼠标悬停卡片标题给出一句口径说明："成交额只反映交易活跃度，不等于资金流入；档位为相对近一年的分位。"

趋势图：图例 `data` 追加 `'成交额分位'`，`selected` 中为 `false`；series 追加一条折线，数据为各行 `amount_multiple_pctl ?? null`，`yAxisIndex: 0`，写法与四个子维度一致。图标题的 hint 改为"点图例可叠加四维指标、涨停数与成交额分位"。

- [ ] **Step 1: 写失败测试**（追加到 `Regime.test.tsx`）

```ts
const capital = { amount_valid_count: 5000, amount_top5_share: .412, amount_multiple: 1.32,
  amount_change_5d: .084, amount_multiple_pctl: 88, top5_share_pctl: 81,
  capital_volume: 'expand', capital_concentration: 'concentrated', capital_flag: 'volume_stall' } as const

it('资金卡显示档位、倍数、5日变化和集中度', async () => {
  vi.mocked(api.regimeHistory).mockResolvedValue({ rows: rows.map(row => ({ ...row, ...capital })), total: rows.length })
  await render()
  const card = host.querySelector('[aria-label="资金状态"]')!
  expect(card.textContent).toContain('放量 · 集中')
  expect(card.textContent).toContain('放量滞涨')
  expect(card.textContent).toContain('成交额 1.32 倍')
  expect(card.textContent).toContain('5日 +8.4%')
  expect(card.textContent).toContain('前5%个股占成交 41.2%')
})

it('旧数据没有资金列时显示未计算, 不显示数值', async () => {
  await render()
  const card = host.querySelector('[aria-label="资金状态"]')!
  expect(card.textContent).toContain('未计算')
  expect(card.textContent).not.toMatch(/\d倍|缩量|平量|放量/)
})

it('成交额分位可叠加到趋势图左轴, 默认不显示', async () => {
  vi.mocked(api.regimeHistory).mockResolvedValue({ rows: rows.map(row => ({ ...row, ...capital })), total: rows.length })
  await render()
  const option = trendChart().option
  expect(option.legend.selected['成交额分位']).toBe(false)
  const series = (option.series as { name: string; data: (number | null)[]; yAxisIndex: number }[]).find(item => item.name === '成交额分位')
  expect(series).toMatchObject({ data: rows.map(() => 88), yAxisIndex: 0 })
})
```

`render`、`trendChart`、`host` 沿用该文件已有的辅助函数；资金卡在情绪周期页签内，若默认页签不是它，先按文件里已有的方式切换页签再断言。

- [ ] **Step 2: 运行，确认失败**

Run: `cd frontend && pnpm exec vitest run src/pages/Regime.test.tsx`
Expected: 新增 3 个用例失败，原有 7 个通过。

- [ ] **Step 3: 实现**

先改 `api.ts` 的 `RegimeRow`，再改 `Regime.tsx`。卡片复用页面现有的 `cardCls` 与 `regime-card-heading` 等类名；确需新样式时加在环境页现有样式文件里，不新建文件。

- [ ] **Step 4: 运行测试与构建**

Run: `cd frontend && pnpm exec vitest run src/pages/Regime.test.tsx && pnpm build`
Expected: 10 passed；构建成功，无 TypeScript 错误。

- [ ] **Step 5: 页面实测**

按记忆中的做法用临时数据目录启动预览（`DATA_DIR` + `TICKFLOW_ENV_FILE` 指向临时位置，不碰真实 `data/`），打开市场环境页的情绪周期页签，检查：有资金数据、无资金数据两种状态；1280 像素与 390 像素宽度下文字不截断、不遮挡；浅色与深色主题。截图留证。

- [ ] **Step 6: 提交**

```bash
git add frontend/src/lib/api.ts frontend/src/pages/Regime.tsx frontend/src/pages/Regime.test.tsx
git commit -s -m "feat(regime): 环境页展示资金状态卡与成交额分位"
```

（若改了样式文件，一并加入。）

---

### Task 5: 真实数据回填与校准记录

本任务会重写真实 `data/regime_history/part.parquet`，**开始前须得到用户明确同意**。

**Files:**
- Modify: `docs/market-capital.md`（追加"首次回填结果"一节）

**Interfaces:**
- Consumes: Task 2 的重算接口与 Task 3 的校准分组。
- Produces: 无代码。

- [ ] **Step 1: 备份并记录重算前的基线**

复制 `data/regime_history/part.parquet` 到 `tmp/regime_before.parquet`。记下重算前文件的行数和日期范围。

- [ ] **Step 2: 全量重算并计时**

在环境页点"重算"，或调用 `POST /api/regime/recompute`（不带参数）。记录耗时。如与此前的重算耗时可比，报告差异；明显变慢要说明。

- [ ] **Step 3: 核对现有结果未变**

用一段只读脚本（放在 `tmp/`，不提交）对比重算前后两份文件：按 `date` 连接后，`score`、`state`、`phase` 三列逐行相同。
Expected: 差异行数为 0。若不为 0，停止并报告差异日期，不得继续。

- [ ] **Step 4: 抽查资金档位**

- 2024-09-30 与 2024-10-08 的 `capital_volume` 应为 `expand`。
- 2024-01 至 2024-02 微盘股下跌期间，列出 `capital_flag` 的分布，人工看是否合理。
- 统计全历史三档各自的占比（预期放量、缩量各约四分之一），以及 `capital_volume` 为 null 的天数和原因（开头样本不足、不可比日）。

- [ ] **Step 5: 运行校准并归档**

Run: `cd backend && uv run --frozen python -m scripts.calibrate_market_game --start 2020-01-02 --split 2023-01-01 --archive`
Expected: 正常结束；博弈页"规则可信度"卡出现三组资金影子检验，各带天数和结论。

- [ ] **Step 6: 把结果写进文档并提交**

在 `docs/market-capital.md` 追加"首次回填结果"：数据区间、重算耗时、三档占比、null 天数、三组影子检验的训练段与测试段差值和结论（方向一致、方向不一致或样本不足）。只写实际跑出的数字；"方向一致"不得表述为收益保证。

```bash
git add docs/market-capital.md
git commit -s -m "docs(regime): 记录资金状态轴首次回填与校准结果"
```
