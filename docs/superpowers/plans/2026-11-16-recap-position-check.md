# 复盘「今日执行」仓位核对 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 复盘日收盘持仓占比与前一交易日存档上限比对，给出是否超限与累计统计；账户规模在批次页填写并按生效日期留档。

**Spec:** `docs/superpowers/specs/2026-11-16-recap-position-check-design.md`（规则以设计为准）。

## Global Constraints

- 账户规模、市值不进提示词与推送；`ratio_pct`、`cap_pct` 为百分数。
- 缺任一输入时 `exceeded` 为 None 并给 `reason`，不给"未超限"。
- 恰好等于上限不算超限。
- 提交 `git commit -s`；CRLF；前端只用 `pnpm exec`。

## Review Focus

1. 累计统计逐日取收盘价：必须一次区间读取，不能逐日调用。→ Task 2 `test_stats_reads_closes_once`。
2. 账户规模在核对日之后才生效：该日按"未填写账户规模"处理，不得用后来的值。→ Task 1/2 测试。
3. 09:30 之后保存的预设版本不得用于当天。→ Task 2 `test_cap_ignores_versions_after_open`。
4. 提示词不含金额。→ Task 3 断言账户规模与市值数字不在文本中。

---

### Task 1: 账户规模偏好与接口

**Files:** `backend/app/services/preferences.py`、`backend/app/api/settings.py`；Test `backend/tests/test_account_size.py`

**Produces:** `get_account_size_history() -> list[dict]`（按 `effective_date` 升序，元素 `{"effective_date": str, "amount": float}`）、`add_account_size(effective_date: str, amount: float) -> list[dict]`（同日覆盖；金额须为有限正数，日期须为 YYYY-MM-DD，否则 ValueError）、`account_size_on(history, day: str) -> float | None`；`GET/PUT /api/settings/preferences/account-size`，PUT 体 `{"effective_date", "amount"}`，返回 `{"history": [...]}`，非法输入 400。

- [ ] 测试：追加与排序、同日覆盖、非法金额 / 日期拒绝、读时清洗坏数据、按日取值（早于首条为 None）、接口往返与 400。
- [ ] 实现 → 通过 + Ruff。

### Task 2: 仓位核对与累计统计

**Files:** Create `backend/app/services/position_check.py`；Test `backend/tests/test_position_check.py`

**Produces:**
- `holdings_on(open_lots, entries, day: str) -> tuple[dict[str, float], int, int]`：返回 `{symbol: qty 合计}`、`inferred_lots`、`unknown_lots`。
- `applicable_cap(data_dir: Path, day: str) -> dict`：`{"cap_pct", "cap_date", "cell", "reason"}`。
- `check_day(day, holdings, closes, account_size, cap, *, intraday=False, inferred=0, unknown=0) -> dict`：返回设计 4.4 结构。
- `build_position_check(repo, as_of: str, open_lots, entries, *, intraday: bool) -> tuple[dict, dict]`：返回 `(position_check, position_days)`；`position_days = {"checked", "exceeded", "by_cell": [{"cell", "label", "checked", "exceeded"}]}`，窗口为 `regime_history` 中不晚于 `as_of`、且前一交易日有存档的最近 120 天；收盘价用一次 `repo.get_enriched_range(start, end, symbols=…, columns=[symbol, date, close, raw_close])` 读取。

- [ ] 测试：buy_date 边界；当天卖出不计；未结清缺 buy_date 用 `created_at` 日期并计 inferred；已结清缺 buy_date 不计并计 unknown；上限取前一交易日 09:30 前最后一版（`test_cap_ignores_versions_after_open`）；无存档 / 未设置原因；超限、未超限、相等、空仓、缺账户规模、盘中、缺收盘价；统计汇总与按格分组；`test_stats_reads_closes_once`。
- [ ] 实现 → 通过 + Ruff。

### Task 3: 接入今日执行与提示词

**Files:** `backend/app/services/market_recap_execution.py`；Test `backend/tests/test_market_recap_execution.py`（现有则追加）

- [ ] `build_execution` 增加 `position_check` 与 `stats_position`（即 `position_days`）字段；仅当二者都无内容且原有内容也空时才返回 None。
- [ ] `execution_prompt_block` 增加："- 仓位: 收盘占比 62%, 上限 80%(强·放量), 未超限" 或原因；"- 仓位纪律: 已核对 n 天, 超限 m 天"。测试断言不含账户规模与市值数字。
- [ ] 通过 + 复盘相关回归。

### Task 4: 前端

**Files:** `frontend/src/lib/api.ts`、`frontend/src/custom/review/RecapExecution.tsx`(+test)、`frontend/src/custom/lots/Page.tsx`(+test)、`frontend/src/lib/queryKeys.ts`

- [ ] 复盘"今日执行"仓位行与累计行（可展开按格）；不可判定显示原因；推断批次附注。
- [ ] 批次页顶部"账户规模 … · 修改"（弹窗：金额、生效日期，默认今天）；未填买入日期的批次显示提示。
- [ ] 测试 → 实现 → `pnpm build`；临时数据目录实测。

### Task 5: 文档与提交

- [ ] `docs/data-directory.md`（偏好 `account_size_history`）、`docs/holding-analysis.md` 或复盘相关文档补"仓位核对"；提交。
