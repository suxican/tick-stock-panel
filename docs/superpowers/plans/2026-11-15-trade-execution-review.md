# 复盘「今日执行」与交易记录 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 结清批次时留下交易记录，并在 AI 复盘快照中由程序给出"今日执行"与累计统计。

**Architecture:** 新增独立的交易记录域模块（纯函数 + `JsonDefinitionStore`），批次模块语义不变；批次 API 增加"结清"端点，先写记录再走现有删除逻辑。复盘侧新增一个快照装配模块，与 `holdings` 并列写入 `snapshot["execution"]`，并生成只含比率与标签的提示词文本段。前端在批次页加结清弹窗与已结清列表，在复盘数据面板加"今日执行"。

**Tech Stack:** Python 3.11 / FastAPI / Pydantic v2 / Polars / pytest / ruff；React 18 / TypeScript / TanStack Query / Tailwind / vitest。

**Spec:** [docs/superpowers/specs/2026-11-15-trade-execution-review-design.md](../specs/2026-11-15-trade-execution-review-design.md)

## Global Constraints

- 开工前完整阅读 `CONTRIBUTING.md` 与 `docs/secondary-development.md`。
- 批次模块 `backend/app/strategy/lots.py` 不加卖出字段；`DELETE /api/lots/{lot_id}` 行为不变。
- 记录只存价格；`pnl_pct` 读取时计算，为**百分数**（`2.53` 表示 2.53%）。
- 数量、价格、成本金额、备注不得进入提示词或推送。
- 日期按北京时间（`app.market_time.cn_today`）；持有天数按交易日，建仓当日记 0，日历用 `huichun_calendar.load_market_calendar(BUNDLED_CALENDAR_PATH, ...)`。
- 任何缺失数据输出 `None`，不得用 0 或自然日替代。
- 归因固定五项及中文名：`planned_profit` 按计划止盈、`planned_stop` 按计划止损、`early_exit` 提前卖出、`late_stop` 拖延未止损、`unplanned` 计划外交易。
- 前端请求与类型只放 `frontend/src/lib/api.ts`，查询键只放 `frontend/src/lib/queryKeys.ts`。
- 前端命令一律 `pnpm` / `pnpm exec`，禁止 `npx` / `npm`。
- 提交使用 `git commit -s`（DCO）。本仓库规定不得自动提交：每个任务的提交步骤须先取得用户同意。
- 新增与改动文件通过 `uv run ruff check`；收尾执行 `git diff --check`。

## Review Focus

1. 批次没有买入日：结清必须成功，`hold_days` 与 `buy_phase` 为 `None`，统计的平均持有天数跳过该笔。→ 任务 1、2
2. 卖出日不是交易日（周末补录）：接受，`hold_days` 为 `None`，不报错。→ 任务 2
3. 卖出价等于成本（盈亏为 0）：计入总笔数，不算盈利也不算亏损。→ 任务 1
4. 记录目录里有损坏文件：跳过该文件，列表与统计照常返回。→ 任务 1
5. 旧复盘报告没有 `execution` 字段、或新报告 `execution` 为 `null`：面板正常渲染，不出现"今日执行"。→ 任务 5

## 文件结构

| 文件 | 职责 |
| --- | --- |
| 新建 `backend/app/strategy/trade_journal.py` | 记录的校验、构建、存取、盈亏与统计（纯函数，不读行情） |
| 新建 `backend/app/services/trade_journal_context.py` | 结清时冻结的上下文：持有交易日数、买入日阶段、是否已登记失效条件 |
| 改 `backend/app/api/lots.py` | 新增 `POST /{lot_id}/close` |
| 新建 `backend/app/api/trade_journal.py`，改 `backend/app/main.py` | 记录列表与删除路由 |
| 新建 `backend/app/services/market_recap_execution.py` | 快照 `execution` 装配与提示词文本段 |
| 改 `backend/app/services/market_recap.py` | 接入装配、提示词、系统提示词新章节 |
| 改 `frontend/src/lib/api.ts`、`queryKeys.ts` | 类型、请求、查询键 |
| 新建 `frontend/src/custom/lots/CloseLotDialog.tsx`、`ClosedTrades.tsx`，改 `Page.tsx` | 结清弹窗、已结清列表 |
| 新建 `frontend/src/custom/review/RecapExecution.tsx`，改 `RecapSnapshot.tsx` | "今日执行"面板 |
| 改 `docs/data-directory.md`、`docs/holding-analysis.md`、`操作说明书.md` | 文档 |

---

### Task 1: 交易记录域模块

**Files:**
- Create: `backend/app/strategy/trade_journal.py`
- Test: `backend/tests/test_trade_journal.py`

**Interfaces:**
- Produces:
  - `REASONS: dict[str, str]`（五项 key → 中文名，顺序即上文顺序）
  - `validate_close(lot: dict, sell_price, sell_date, reason, note, *, today: date) -> None`，非法抛 `ValueError`（中文信息）
  - `build_entry(lot: dict, *, sell_price: float, sell_date: str, reason: str, note: str, name: str, had_invalidation: bool, buy_phase: str | None, hold_days: int | None) -> dict`，字段见 spec 5.1，`id = f"tj_{lot['id']}"`
  - `pnl_pct(entry: dict) -> float`：`(sell_price / cost_price - 1) * 100`
  - `load_all(data_dir: Path) -> list[dict]`、`save_one(data_dir, entry) -> None`、`delete_one(data_dir, entry_id: str) -> bool`
  - `stats(entries: list[dict]) -> dict | None`（空列表返回 `None`）

`stats` 返回结构（所有比率为百分数，无样本的项为 `None`）：

```python
{"total": int, "wins": int, "losses": int, "win_rate": float | None,
 "avg_win_pct": float | None, "avg_loss_pct": float | None,   # avg_loss_pct 为正的幅度
 "payoff_ratio": float | None,                                # avg_win_pct / avg_loss_pct
 "avg_hold_days": float | None,                               # 跳过 hold_days 为 None 的记录
 "by_reason": [{"key", "label", "total", "win_rate", "avg_pnl_pct"}],   # 按 REASONS 顺序, 只列出现过的
 "by_phase":  [{"key", "label", "total", "win_rate", "avg_pnl_pct"}]}   # buy_phase 为 None 归入 key="unknown", label="未知"
```

阶段中文名取 `app.services.market_phase.PHASE_LABELS`。存储用 `JsonDefinitionStore("user_data/trade_journal", glob_pattern="tj_*.json", label="trade journal")`。

- [ ] **Step 1: 写失败测试**

```python
from datetime import date
import pytest
from app.strategy import trade_journal as tj

LOT = {"id": "lot_a1", "symbol": "600519.SH", "qty": 100, "cost_price": 1680.0,
       "buy_date": "2026-11-05", "target_pct": 10, "stop_pct": 5}
TODAY = date(2026, 11, 13)

def _entry(**kw):
    base = dict(sell_price=1722.0, sell_date="2026-11-13", reason="planned_profit", note="",
                name="贵州茅台", had_invalidation=True, buy_phase="rally", hold_days=6)
    base.update(kw)
    return tj.build_entry(LOT, **base)

def test_build_entry_freezes_lot_fields_and_derives_id():
    e = _entry()
    assert e["id"] == "tj_lot_a1" and e["lot_id"] == "lot_a1"
    assert (e["qty"], e["cost_price"], e["buy_date"], e["stop_pct"]) == (100, 1680.0, "2026-11-05", 5)
    assert tj.pnl_pct(e) == pytest.approx(2.5)           # 百分数, 不是 0.025

@pytest.mark.parametrize("kw, msg", [
    (dict(sell_price=0), "卖出价"), (dict(sell_price=True), "卖出价"),
    (dict(sell_date="2026/11/13"), "卖出日"), (dict(sell_date="2026-11-14"), "不能晚于今天"),
    (dict(sell_date="2026-11-04"), "不能早于买入日"), (dict(reason="other"), "归因"),
    (dict(note="x" * 201), "备注"),
])
def test_validate_close_rejects(kw, msg):
    args = dict(sell_price=1722.0, sell_date="2026-11-13", reason="planned_profit", note="")
    args.update(kw)
    with pytest.raises(ValueError, match=msg):
        tj.validate_close(LOT, today=TODAY, **args)

def test_validate_close_allows_missing_buy_date():
    tj.validate_close({**LOT, "buy_date": None}, 1722.0, "2026-11-13", "unplanned", "", today=TODAY)

def test_store_roundtrip_overwrites_same_lot_and_skips_corrupt(tmp_path):
    tj.save_one(tmp_path, _entry()); tj.save_one(tmp_path, _entry(sell_price=1700.0))
    (tmp_path / "user_data" / "trade_journal" / "tj_bad.json").write_text("{", encoding="utf-8")
    rows = tj.load_all(tmp_path)
    assert [r["sell_price"] for r in rows] == [1700.0]
    assert tj.delete_one(tmp_path, "tj_lot_a1") is True and tj.load_all(tmp_path) == []

def test_stats_groups_and_ignores_breakeven_and_missing_hold_days():
    es = [_entry(sell_price=1848.0, hold_days=4),                                   # +10%
          _entry(sell_price=1596.0, reason="late_stop", buy_phase="ebb", hold_days=None),  # -5%
          _entry(sell_price=1680.0, reason="early_exit", buy_phase=None, hold_days=2)]     # 0%
    s = tj.stats(es)
    assert (s["total"], s["wins"], s["losses"]) == (3, 1, 1)
    assert s["win_rate"] == pytest.approx(100 / 3)
    assert (s["avg_win_pct"], s["avg_loss_pct"], s["payoff_ratio"]) == pytest.approx((10, 5, 2))
    assert s["avg_hold_days"] == pytest.approx(3)
    assert [g["key"] for g in s["by_reason"]] == ["planned_profit", "early_exit", "late_stop"]
    assert {g["key"]: g["label"] for g in s["by_phase"]}["unknown"] == "未知"

def test_stats_empty_and_one_sided():
    assert tj.stats([]) is None
    assert tj.stats([_entry(sell_price=1848.0)])["payoff_ratio"] is None
```

- [ ] **Step 2: 运行确认失败**

Run: `cd backend && uv run pytest tests/test_trade_journal.py -q`
Expected: FAIL（`ModuleNotFoundError: app.strategy.trade_journal`）

- [ ] **Step 3: 实现 `backend/app/strategy/trade_journal.py`**

按上方接口实现。`validate_close` 的错误信息须包含测试里的关键词；`sell_price` 拒绝 `bool`；备注上限 200 字。模块头注释说明"批次不做会计，卖出留痕在此"。

- [ ] **Step 4: 运行确认通过，并检查 ruff**

Run: `cd backend && uv run pytest tests/test_trade_journal.py -q && uv run ruff check app/strategy/trade_journal.py tests/test_trade_journal.py`
Expected: 全部 PASS，ruff 无告警

- [ ] **Step 5: 提交（先取得用户同意）**

```bash
git add backend/app/strategy/trade_journal.py backend/tests/test_trade_journal.py
git commit -s -m "feat(trade-journal): 交易记录域模块与统计"
```

---

### Task 2: 结清端点与记录路由

**Files:**
- Create: `backend/app/services/trade_journal_context.py`
- Create: `backend/app/api/trade_journal.py`
- Modify: `backend/app/api/lots.py`（在 `delete_lot` 之后追加）
- Modify: `backend/app/main.py`（导入与 `app.include_router(trade_journal.router)`，紧随 `lots.router`）
- Modify: `backend/app/services/holding_analysis.py`（新增一个公开小函数）
- Test: `backend/tests/test_trade_journal_api.py`、`backend/tests/test_trade_journal_context.py`

**Interfaces:**
- Consumes: 任务 1 全部导出；`lots_api.delete_lot`、`lots_domain.load_all`；`repo.get_name_map(symbols) -> dict[str, str]`
- Produces:
  - `holding_analysis.lots_invalidation_flags(data_dir, symbols: list[str]) -> dict[str, bool]`：该股票在批次来源（`_key("lots", "", symbol)`）的研究设置里 `invalidation` 非空即 `True`；研究文件不可读时全部 `False`
  - `trade_journal_context.hold_days(buy_date: str | None, sell_date: str) -> int | None`：两日都在交易日历内时为会话序号之差，否则 `None`；日历异常返回 `None`
  - `trade_journal_context.buy_phase(data_dir, buy_date: str | None) -> str | None`：`load_regime_history` 中该日的 `phase`，无则 `None`
  - `POST /api/lots/{lot_id}/close`，请求体 `CloseLotModel{sell_price: float, sell_date: str, reason: str, note: str = ""}`，返回 `{"ok": True, "entry": {...含 pnl_pct}}`；批次不存在 404；校验失败 400（中文 detail）
  - `GET /api/trade-journal` → `{"entries": [...按 sell_date、created_at 倒序, 各含 pnl_pct], "stats": dict | None}`
  - `DELETE /api/trade-journal/{entry_id}` → `{"ok": bool}`；id 不匹配 `^tj_[a-z0-9_]+$` 返回 400

结清顺序：在 `lots_api._write_lock` 内读取批次并 `save_one` 记录，释放锁后调用现有 `delete_lot(lot_id, request)`（它自己加锁并重载引擎）。

- [ ] **Step 1: 写失败测试**

`test_trade_journal_context.py`：

```python
from app.services import trade_journal_context as ctx

def test_hold_days_counts_sessions_and_degrades():
    assert ctx.hold_days("2026-11-05", "2026-11-13") == 6     # 周四 → 次周周五
    assert ctx.hold_days("2026-11-13", "2026-11-13") == 0
    assert ctx.hold_days(None, "2026-11-13") is None
    assert ctx.hold_days("2026-11-05", "2026-11-14") is None  # 周六非交易日

def test_buy_phase_reads_regime_history(tmp_path, monkeypatch):
    import polars as pl
    from datetime import date
    df = pl.DataFrame({"date": [date(2026, 11, 5)], "phase": ["rally"]})
    monkeypatch.setattr("app.services.regime_builder.load_regime_history", lambda _d: df)
    assert ctx.buy_phase(tmp_path, "2026-11-05") == "rally"
    assert ctx.buy_phase(tmp_path, "2026-11-06") is None and ctx.buy_phase(tmp_path, None) is None
```

（若内置日历不覆盖 2026-11，改用日历覆盖范围内等价的周四/周五/周六日期，并在测试注释里写明。）

`test_trade_journal_api.py`（复用 `tests/test_lots.py` 的 `_lot` / `_EngineStub` / `_make_request` / `_patch_channels` 写法，`repo` 另加 `get_name_map=lambda syms: {"600519.SH": "贵州茅台"}`；把 `cn_today` 打桩为 2026-11-13）：

```python
def test_close_writes_entry_then_removes_lot_and_rules(tmp_path, monkeypatch): ...
    # 断言: 返回 entry.id == "tj_lot_test1", entry.name == "贵州茅台", entry.pnl_pct ≈ (卖出价/1500-1)*100
    #       批次文件不存在; monitor_rules 中 lot_test1_p / lot_test1_d 不存在; engine.set_calls 增加
def test_close_missing_lot_is_404_and_writes_nothing(tmp_path, monkeypatch): ...
def test_close_invalid_input_is_400_and_keeps_lot(tmp_path, monkeypatch): ...      # reason="other"
def test_close_without_buy_date_succeeds_with_null_context(tmp_path, monkeypatch): ...
    # entry.hold_days is None and entry.buy_phase is None
def test_close_again_after_success_is_404_and_single_entry(tmp_path, monkeypatch): ...
def test_list_sorted_desc_with_stats_and_delete(tmp_path): ...
    # 两条记录 → entries[0].sell_date 较新; stats.total == 2; 删除后 total == 1
def test_delete_rejects_bad_id(tmp_path): ...                                     # "../x" → 400
def test_invalidation_flags(tmp_path): ...
    # 用 HoldingAnalysisStore 写入一只股票的 invalidation → {sym_a: True, sym_b: False}
```

每个测试写出完整断言（按注释），不留占位。

- [ ] **Step 2: 运行确认失败**

Run: `cd backend && uv run pytest tests/test_trade_journal_api.py tests/test_trade_journal_context.py -q`
Expected: FAIL（模块或路由不存在）

- [ ] **Step 3: 实现上下文模块与 `lots_invalidation_flags`**

- [ ] **Step 4: 实现两个路由并在 `main.py` 注册**

`trade_journal.py` 路由保持薄：只做参数校验、调用任务 1 的函数、附加 `pnl_pct`。

- [ ] **Step 5: 运行确认通过，并回归相邻模块**

Run: `cd backend && uv run pytest tests/test_trade_journal.py tests/test_trade_journal_api.py tests/test_trade_journal_context.py tests/test_lots.py tests/test_holding_analysis.py tests/test_holding_analysis_store.py tests/test_holding_analysis_api.py -q`
Expected: 全部 PASS

Run: `cd backend && uv run ruff check app/api/lots.py app/api/trade_journal.py app/services/trade_journal_context.py app/services/holding_analysis.py tests/test_trade_journal_api.py tests/test_trade_journal_context.py`
Expected: 新增与改动行无告警

- [ ] **Step 6: 提交（先取得用户同意）**

```bash
git add backend/app/api/lots.py backend/app/api/trade_journal.py backend/app/main.py backend/app/services/trade_journal_context.py backend/app/services/holding_analysis.py backend/tests/test_trade_journal_api.py backend/tests/test_trade_journal_context.py
git commit -s -m "feat(lots): 结清批次并写入交易记录"
```

---

### Task 3: 复盘快照 `execution` 与提示词

**Files:**
- Create: `backend/app/services/market_recap_execution.py`
- Modify: `backend/app/services/market_recap.py`（`_SYSTEM_PROMPT`、`_build_user_prompt`、`recap_market_stream` 第 475–487 行附近）
- Test: `backend/tests/test_market_recap_execution.py`，并在 `backend/tests/test_market_recap_prompt.py` 追加

**Interfaces:**
- Consumes: 任务 1 的 `load_all` / `pnl_pct` / `stats` / `REASONS`；任务 2 的 `lots_invalidation_flags`；`lots_domain.load_all`；`market_recap_holdings._raw_closes(repo, day, symbols)`；`repo.get_name_map`
- Produces:
  - `build_execution(repo, snapshot: dict) -> dict | None`
  - `execution_prompt_block(execution: dict | None) -> str`
  - `_build_user_prompt(..., execution_context: str = "")`

`build_execution` 返回（三个列表皆空且 `stats` 为 `None` 时返回 `None`）：

```python
{"as_of": str,
 "opened": [{"symbol", "name", "has_invalidation": bool}],
 "closed": [{"symbol", "name", "pnl_pct": float, "hold_days": int | None, "reason": str, "reason_label": str}],
 "stop_breached": [{"symbol", "name", "stop_price": float, "close": float}],
 "stats": dict | None}
```

规则：
- `opened` = 未结清批次中 `buy_date == as_of` 的（`has_invalidation` 取 `lots_invalidation_flags`）+ 记录中 `buy_date == as_of` 的（取其 `had_invalidation`）；同一 symbol 去重。
- `closed` = 记录中 `sell_date == as_of`。
- `stop_breached` = 未结清且 `stop_pct > 0` 的批次，`close <= round(cost_price * (1 - stop_pct / 100), 4)`；`snapshot["intraday"]` 为真或 `_raw_closes` 返回 `None` 时为空列表；单只取不到收盘价则跳过该只。
- `stats` = `trade_journal.stats(sell_date <= as_of 的记录)`。

`execution_prompt_block` 输出以 `## 今日执行(程序判定)` 开头，逐行列出新建、结清（名称、盈亏百分比、持有天数、归因中文名）、触及止损价仍持有（只写名称）、累计统计（总笔数、胜率、盈亏比、最需关注的归因分组）。`execution` 为空返回 `""`。

系统提示词改动：
- 第 47 行的节数说明改为"基础 5 节；输入含"持仓联动"时加「📦 持仓联动」；输入含"今日执行"时加「🧾 执行复盘」"。
- 在「📦 持仓联动」之后、「⛔ 失效条件」之前插入：

```
## 🧾 执行复盘
仅当输入含"今日执行"时输出本节,否则整节省略。页面已展示执行表与累计统计,本节只做解释,2-4 句:今天的买卖与昨日剧本、持仓条件是否一致;累计统计里最该改的一类交易是什么。**不得改判归因,不得给出价位、数量或操作动作**;触及止损价仍持有的,只点名并说明这与计划不一致。
```

- [ ] **Step 1: 写失败测试**

`test_market_recap_execution.py`（用 `tmp_path` 写入批次与记录；`repo` 为 `SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path), get_name_map=...)`；对 `_raw_closes` 打桩）：

```python
def test_execution_lists_opened_closed_breached_and_stats(tmp_path, monkeypatch): ...
    # as_of=2026-11-13: 一个当日买入批次(无失效条件)、一条当日结清记录(+2.5%, planned_profit)、
    # 一个 stop_pct=5 成本 100 的批次收盘 94.9 → stop_breached == [{"stop_price": 95.0, "close": 94.9, ...}]
    # 收盘恰为 95.0 也算触及; 95.01 不算
def test_execution_intraday_or_missing_closes_skips_breach(tmp_path, monkeypatch): ...
def test_execution_stats_excludes_entries_sold_after_as_of(tmp_path, monkeypatch): ...
def test_execution_none_when_nothing_to_show(tmp_path): ...
def test_prompt_block_has_no_qty_price_cost_or_note(tmp_path, monkeypatch): ...
    # 记录 qty=700, cost_price=1680.0, sell_price=1722.0, note="追高了" →
    # block 中不含 "700" "1680" "1722" "追高了"; 含 "按计划止盈" 与 "+2.5%"
def test_prompt_block_empty_for_none(): assert execution_prompt_block(None) == ""
```

`test_market_recap_prompt.py` 追加：

```python
def test_user_prompt_includes_execution_context_only_when_given(): ...
def test_system_prompt_declares_execution_section():
    assert "## 🧾 执行复盘" in _SYSTEM_PROMPT and "不得改判归因" in _SYSTEM_PROMPT
```

每个测试写出完整断言，不留占位。

- [ ] **Step 2: 运行确认失败**

Run: `cd backend && uv run pytest tests/test_market_recap_execution.py tests/test_market_recap_prompt.py -q`
Expected: 新增用例 FAIL

- [ ] **Step 3: 实现 `market_recap_execution.py`**

- [ ] **Step 4: 接入 `market_recap.py`**

在 `attach_holdings_check(snapshot, repo, reports)` 之后，以同样的 try/except 降级方式写 `snapshot["execution"] = build_execution(repo, snapshot)`（失败记 warning 并置 `None`）；`_build_user_prompt` 在 `holdings_context` 之后追加 `execution_context`；调用处传 `execution_prompt_block((snapshot or {}).get("execution"))`；按上文修改 `_SYSTEM_PROMPT`。`SNAPSHOT_VERSION` 不变。

- [ ] **Step 5: 运行确认通过，并回归复盘相关测试**

Run: `cd backend && uv run pytest tests/test_market_recap_execution.py tests/test_market_recap_prompt.py tests/test_market_recap_snapshot.py tests/test_market_recap_holdings.py tests/test_market_recap_plan.py tests/test_market_recap_loop.py -q`
Expected: 全部 PASS

Run: `cd backend && uv run ruff check app/services/market_recap_execution.py app/services/market_recap.py tests/test_market_recap_execution.py`
Expected: 新增与改动行无告警

- [ ] **Step 6: 提交（先取得用户同意）**

```bash
git add backend/app/services/market_recap_execution.py backend/app/services/market_recap.py backend/tests/test_market_recap_execution.py backend/tests/test_market_recap_prompt.py
git commit -s -m "feat(recap): 快照新增今日执行与执行复盘章节"
```

---

### Task 4: 批次页结清弹窗与已结清列表

**Files:**
- Modify: `frontend/src/lib/api.ts`（`Lot` 接口之后加类型；`lotDelete` 之后加请求）
- Modify: `frontend/src/lib/queryKeys.ts`（`lots` 附近加 `tradeJournal: ['trade-journal'] as const`）
- Create: `frontend/src/custom/lots/CloseLotDialog.tsx`、`frontend/src/custom/lots/ClosedTrades.tsx`
- Modify: `frontend/src/custom/lots/Page.tsx`
- Test: `frontend/src/custom/lots/CloseLotDialog.test.tsx`、`frontend/src/custom/lots/ClosedTrades.test.tsx`

**Interfaces:**
- Consumes: 任务 2 的三个接口
- Produces（`api.ts`）:

```ts
export type TradeReason = 'planned_profit' | 'planned_stop' | 'early_exit' | 'late_stop' | 'unplanned'
export const TRADE_REASON_LABELS: Record<TradeReason, string>   // 中文名同 Global Constraints
export interface TradeJournalEntry {
  id: string; lot_id: string; symbol: string; name: string
  qty: number; cost_price: number; buy_date: string | null
  sell_price: number; sell_date: string; reason: TradeReason; note: string
  had_invalidation: boolean; buy_phase: string | null; hold_days: number | null
  /** 百分数; 卖出价相对登记成本, 不含费用 */
  pnl_pct: number
  created_at: string
}
export interface TradeStatsGroup { key: string; label: string; total: number; win_rate: number | null; avg_pnl_pct: number | null }
export interface TradeStats {
  total: number; wins: number; losses: number; win_rate: number | null
  avg_win_pct: number | null; avg_loss_pct: number | null; payoff_ratio: number | null
  avg_hold_days: number | null; by_reason: TradeStatsGroup[]; by_phase: TradeStatsGroup[]
}
// api.lotClose(id, body: { sell_price: number; sell_date: string; reason: TradeReason; note: string })
//   → { ok: boolean; entry: TradeJournalEntry }
// api.tradeJournalList() → { entries: TradeJournalEntry[]; stats: TradeStats | null }
// api.tradeJournalDelete(id) → { ok: boolean }
```

- `CloseLotDialog` props：`{ lot: Lot; name: string; onClose: () => void }`。标题行显示成本与数量；字段为卖出价、卖出日（默认北京时间今天）、五项归因单选、可选备注（`maxLength={200}`）；未选归因或卖出价非正数时"结清并记录"禁用；提交中禁用；后端 400 的 detail 显示在弹窗内。每次挂载表单为初始值（由 `Page.tsx` 以 `key={lot.id}` 条件渲染保证）。
- 成功后失效：`Page.tsx` 中现有删除所失效的全部查询键，外加 `qk.tradeJournal`、前缀 `['holding-analysis']` 与前缀 `['review-bellwethers']`。
- `ClosedTrades`：自取 `api.tradeJournalList()`；默认收起，标题"已结清 N 笔"；展开后按接口顺序列出名称、卖出日、盈亏（红涨绿跌沿用页面现有配色工具）、持有天数（`null` 显示"—"）、归因中文名；每行删除沿用页面现有"再次点击确认"交互；无记录时整块不渲染；加载失败显示一行轻提示与重试。底部一行小字"盈亏为卖出价相对登记成本，不含费用"。
- `Page.tsx`：每行"删除"左侧加"结清"按钮（`title="卖出后结清并留下交易记录"`）；批次表下方渲染 `<ClosedTrades />`。

- [ ] **Step 1: 写失败测试**

沿用 `frontend/src/custom/lots/Page.test.tsx` 的 jsdom + `createRoot` + `vi.mock('@/lib/api')` 写法。

`CloseLotDialog.test.tsx`：
- 渲染后"结清并记录"为禁用；填入卖出价 `1722.5` 并选"按计划止盈"后可点；点击后 `lotClose` 以 `('lot_a1', { sell_price: 1722.5, sell_date: <今天>, reason: 'planned_profit', note: '' })` 被调用一次，随后 `onClose` 被调用。
- `lotClose` 以 `new Error('卖出日不能晚于今天')` 拒绝时，弹窗内出现该文字，`onClose` 未被调用，按钮恢复可点。

`ClosedTrades.test.tsx`：
- `entries: []` 时容器文本为空。
- 两条记录时显示"已结清 2 笔"；展开后含"按计划止盈"、"+2.50%"，`hold_days: null` 的行含"—"。
- 点击某行删除两次后 `tradeJournalDelete` 以该行 id 被调用一次。

- [ ] **Step 2: 运行确认失败**

Run: `cd frontend && pnpm exec vitest run src/custom/lots/CloseLotDialog.test.tsx src/custom/lots/ClosedTrades.test.tsx`
Expected: FAIL（组件不存在）

- [ ] **Step 3: 加类型、请求与查询键**

- [ ] **Step 4: 实现两个组件并接入 `Page.tsx`**

弹窗外观与关闭行为沿用仓库现有弹窗组件（先查 `frontend/src/components/` 里批次页或监控中心已在用的那个），不新造。

- [ ] **Step 5: 运行确认通过**

Run: `cd frontend && pnpm exec vitest run src/custom/lots`
Expected: 全部 PASS（含原有 `Page.test.tsx`；其 `vi.mock('@/lib/api')` 需补上 `tradeJournalList` 桩）

- [ ] **Step 6: 构建**

Run: `cd frontend && pnpm build`
Expected: 构建成功，无 TypeScript 错误

- [ ] **Step 7: 提交（先取得用户同意）**

```bash
git add frontend/src/lib/api.ts frontend/src/lib/queryKeys.ts frontend/src/custom/lots/
git commit -s -m "feat(lots): 批次页结清弹窗与已结清列表"
```

---

### Task 5: 复盘面板"今日执行"

**Files:**
- Modify: `frontend/src/lib/api.ts`（`RecapSnapshot` 加 `execution?: RecapExecution | null` 及其类型）
- Create: `frontend/src/custom/review/RecapExecution.tsx`
- Modify: `frontend/src/custom/review/RecapSnapshot.tsx:446-448`（在 `HoldingsLinkage` 之后渲染）
- Test: `frontend/src/custom/review/RecapSnapshot.test.tsx`（追加）

**Interfaces:**
- Consumes: 任务 3 的 `execution` 结构；任务 4 的 `TradeStats`、`TRADE_REASON_LABELS`
- Produces:

```ts
export interface RecapExecution {
  as_of: string
  opened: { symbol: string; name: string; has_invalidation: boolean }[]
  closed: { symbol: string; name: string; pnl_pct: number; hold_days: number | null; reason: TradeReason; reason_label: string }[]
  stop_breached: { symbol: string; name: string; stop_price: number; close: number }[]
  stats: TradeStats | null
}
```

- `RecapExecution` props：`{ execution: RecapExecution; onOpenStock?: (symbol: string) => void }`（与同文件其他分区的点击行为一致）。
- 版式：标题"今日执行"。最多三行摘要，空的行不显示：
  - `新建 N 笔`，其后列名称；未登记失效条件的名称后标"未登记失效条件"。
  - `结清 N 笔`，其后列"名称 ±x.xx% · 归因中文名"。
  - `触及止损价仍持有 N 只`，其后列名称（警示色）。
- `stats` 非空时显示一个默认收起的"累计统计"：展开后第一行为总笔数、胜率、盈亏比、平均持有天数（`null` 显示"—"），其下为"按归因""按买入时阶段"两个小表（分组、笔数、胜率、平均盈亏）。`stats.total < 10` 时在标题旁标"样本不足"。
- 分区底部小字："盈亏为卖出价相对登记成本，不含费用"。

- [ ] **Step 1: 写失败测试（追加到 `RecapSnapshot.test.tsx`，沿用文件内已有的快照构造与渲染辅助）**

- `renders today's execution summary and collapsed stats`：给定一条 opened（`has_invalidation: false`）、一条 closed（`pnl_pct: 2.5`、`reason_label: '按计划止盈'`）、一条 stop_breached、`stats.total = 3` → 文本含"今日执行"、"新建 1 笔"、"未登记失效条件"、"+2.50%"、"按计划止盈"、"触及止损价仍持有 1 只"、"样本不足"；展开前不含"按买入时阶段"，点击"累计统计"后包含。
- `omits the execution section when absent or null`：`execution` 缺省与为 `null` 两种情况下文本都不含"今日执行"，其余分区照常渲染。
- `hides empty rows`：仅有 `stats` 时不含"新建"、"结清"、"触及止损价"。

- [ ] **Step 2: 运行确认失败**

Run: `cd frontend && pnpm exec vitest run src/custom/review/RecapSnapshot.test.tsx`
Expected: 新增用例 FAIL

- [ ] **Step 3: 加类型，实现 `RecapExecution.tsx`，在 `RecapSnapshot.tsx` 接入**

接入条件：`snapshot.execution` 存在即渲染（后端已保证无内容时为 `null`）。

- [ ] **Step 4: 运行确认通过并构建**

Run: `cd frontend && pnpm exec vitest run src/custom/review && pnpm build`
Expected: 全部 PASS，构建成功

- [ ] **Step 5: 提交（先取得用户同意）**

```bash
git add frontend/src/lib/api.ts frontend/src/custom/review/
git commit -s -m "feat(review): 复盘面板新增今日执行"
```

---

### Task 6: 文档与整体验证

**Files:**
- Modify: `docs/data-directory.md`、`docs/holding-analysis.md`、`操作说明书.md`

- [ ] **Step 1: `docs/data-directory.md` 在 `user_data/lots` 条目附近新增一行**

内容须包含：路径 `user_data/trade_journal/tj_<批次id>.json`；写入入口为结清批次（`backend/app/api/lots.py`），读取入口为批次页已结清列表与 AI 复盘"今日执行"；不设条数上限；删除后累计统计丢失且无法从行情或批次重建；含个人交易数据，按用户数据管理。

- [ ] **Step 2: `docs/holding-analysis.md` 新增"结清与交易记录"小节**

写明：结清与删除的区别；只支持整批结清，分批卖出需先拆分批次；盈亏口径；五项归因；复盘"今日执行"各行的判定规则；数量、价格、备注不发给模型；自定义复盘提示词的用户需手动加入「🧾 执行复盘」章节。

- [ ] **Step 3: `操作说明书.md` 在批次提醒与"18. AI 复盘"两节各补一段使用步骤**

- [ ] **Step 4: 后端整体验证**

Run: `cd backend && uv run pytest tests/test_trade_journal.py tests/test_trade_journal_api.py tests/test_trade_journal_context.py tests/test_market_recap_execution.py tests/test_lots.py tests/test_position_t_lots_sync.py tests/test_holding_analysis.py tests/test_holding_analysis_store.py tests/test_holding_analysis_api.py tests/test_holding_alert_guard.py tests/test_market_recap_prompt.py tests/test_market_recap_snapshot.py tests/test_market_recap_holdings.py tests/test_market_recap_plan.py tests/test_market_recap_loop.py tests/test_market_bellwether.py -q`
Expected: 全部 PASS

- [ ] **Step 5: 前端整体验证**

Run: `cd frontend && pnpm exec vitest run src/custom/lots src/custom/review && pnpm build`
Expected: 全部 PASS，构建成功

- [ ] **Step 6: 页面联调**

启动开发服务，在预览中：新建一个测试批次 → 点"结清"并提交 → 确认批次消失、"已结清 1 笔"出现、监控中心不再有该批次的两条规则 → 打开复盘页生成一篇复盘 → 确认数据面板出现"今日执行"且正文有「🧾 执行复盘」→ 删除该测试记录。窄屏（375 宽）下检查弹窗可操作、表格不溢出。联调产生的测试批次与记录在结束前清理。

- [ ] **Step 7: 收尾检查并提交（先取得用户同意）**

Run: `git diff --check && git status --short`
Expected: 无空白错误；改动仅限本计划列出的文件

```bash
git add docs/data-directory.md docs/holding-analysis.md 操作说明书.md
git commit -s -m "docs: 结清批次、交易记录与复盘今日执行"
```
