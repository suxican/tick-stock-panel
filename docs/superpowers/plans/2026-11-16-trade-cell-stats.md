# 交易记录按「买入前格子」统计 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 结清时冻结买入前一交易日的环境格子，复盘累计统计按格子分组。

**Spec:** `docs/superpowers/specs/2026-11-16-trade-cell-stats-design.md`

## Global Constraints

- 格子缺失一律 None / "未知"，不用相邻日顶替；旧记录无字段可读。
- 情绪三档复用 `market_game_calibration.MATRIX_SENTIMENT`，格子顺序复用 `preferences.ENV_MATRIX_CELLS`，标签复用 `position_check.cell_label`。
- 只提交本计划涉及的文件（工作区另有他人未提交改动）；`git commit -s`；CRLF。

## Review Focus

1. 买入日是节假日或不在环境历史中：仍取其之前最后一个交易日，不报错。→ Task 1 `test_buy_cell_uses_last_day_before_non_trading_buy_date`。

---

### Task 1: 后端冻结与统计

**Files:** `backend/app/services/trade_journal_context.py`、`backend/app/strategy/trade_journal.py`、`backend/app/api/lots.py`；Tests `backend/tests/test_trade_journal_context.py`、`backend/tests/test_trade_journal.py`、`backend/tests/test_trade_journal_api.py`

**Produces:** `buy_cell(data_dir: Path, buy_date: str | None) -> str | None`；`build_entry(..., buy_cell: str | None = None)` 写入 `buy_cell`；`stats()["by_cell"]: list[{"key","label","total","win_rate","avg_pnl_pct"}]`，"未知"键为 `unknown`。

- [ ] 测试：前一交易日格子；买入日非交易日；无前一交易日 / 无买入日 / 缺 state / 缺 capital_volume / 未知 state → None；`build_entry` 写入与默认 None；`by_cell` 分组顺序与"未知"；结清接口写入 `buy_cell`。
- [ ] 实现 → `uv run pytest tests -q -k "trade_journal or lots"` 通过 + Ruff。

### Task 2: 前端与文档

**Files:** `frontend/src/lib/api.ts`（`TradeStats.by_cell?`）、`frontend/src/custom/review/RecapExecution.tsx`(+test)、`docs/holding-analysis.md`、`docs/data-directory.md`

- [ ] 测试：存在 `by_cell` 时展开后显示"按买入前格子"表与格子标签；缺字段不报错。
- [ ] 实现 → vitest + `pnpm build` → 提交。
