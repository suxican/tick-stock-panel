# 机会记录核验 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给 `GET /api/first-board/opportunities` 增加可选 `verify`，返回收盘结果与模拟成交核验，并在复盘页用按钮触发。

**Architecture:** 纯函数模块 `first_board_verification.py` 做收盘判定与成交汇总；`FirstBoardService.opportunities` 在 `verify=True` 时读当日日线并调用现有 `build_paper_trade_audit`；前端 `OpportunityList` 用按钮切换到 `verify=true` 查询。

**Tech Stack:** Python/FastAPI/polars/pytest（`backend/`，`uv run --no-sync`）；React/TanStack Query/vitest（`frontend/`，`pnpm exec`）。

**Spec:** `docs/superpowers/specs/2026-11-21-first-board-opportunities-verify-design.md`

## Global Constraints

- 默认（不带 `verify`）响应与现状逐字段一致；核验结果不持久化。
- 收盘判定容差 0.005；日线缺失、缺价、价格自相矛盾、今日未到 15:00 一律 `unverified`，绝不记为 `not_sealed`。
- 多张买单只标 `ambiguous`，不挑一张；审计不可用或明细截断时全部提示股 `evidence_missing`。
- 前端证据缺失统一显示“待核验”；不自动核验、不进轮询。
- 中文文案；工作区 CRLF，脚本改写须保持 `\r\n`；不自动提交。

## Review Focus

- 盘后日线尚未同步（无当日行）：显示待核验而不是未封。（Task 1）
- 同一只股票多条买入候选事件、其中只有一条有买单：取那一条；两条各有买单：`ambiguous`。（Task 1）
- 账户未关联：每行 `no_account`，且不调用审计。（Task 2）
- 审计明细被截断：全部提示股待核验。（Task 1）
- 核验请求失败时页面仍显示未核验的机会记录并可重试。（Task 3）

---

### Task 1: 核验纯函数

**Files:**
- Create: `backend/app/services/first_board_verification.py`
- Test: `backend/tests/test_first_board_verification.py`

**Interfaces:**
- Produces:
  - `close_results(daily: pl.DataFrame, symbols: Iterable[str], day: date, *, closed: bool) -> dict[str, dict]`，值为 `{"status", "close_price", "limit_price", "reason"}`；`daily` 含 `symbol, date, raw_close, raw_high, raw_prev_close` 及可选 `limit_up_price, limit_up`；`closed=False` 时全部 `unverified`（原因“当日尚未收盘”）。
  - `paper_results(audit: dict | None, alerted_symbols: Iterable[str], *, has_account: bool) -> dict[str, dict]`，值为 `{"status", "reason", "net_return"}`；`audit` 为 `build_paper_trade_audit` 的返回（用其 `trades`、`status`、`reason`、`coverage.truncated`）。
  - `apply_verification(report: dict, close: dict[str, dict], paper: dict[str, dict]) -> dict`：给每行加 `close`、`paper`，给 `summary` 加四个计数，置 `verified: True`，返回同一报告。

- [ ] **Step 1: 写失败测试**，覆盖：收盘价等于涨停价 → `sealed`；低于 → `not_sealed`；缺日线行 → `unverified`；收盘高于涨停价或高于最高价 → `unverified`；`closed=False` → 全部 `unverified`；一个有买单的行 → 取其状态，`closed` 带 `net_return`；两行各有买单 → `ambiguous`；仅 `not_ordered` → `not_ordered`；`no_account`；审计 `unavailable` 与 `truncated` → `evidence_missing`；`apply_verification` 的四个计数与分母（`close_checked_count` 不含 `unverified`，`paper_checked_count` 只数结论明确的提示股）。
- [ ] **Step 2: 运行确认失败** — `cd backend; uv run --no-sync pytest tests/test_first_board_verification.py -q`，预期：模块不存在。
- [ ] **Step 3: 实现**三个函数；涨停价用 `app.strategy.first_board.observation_limit_price(symbol, day, raw_prev_close, row, historical=True)`；归因行状态优先级只用于说明“恰有一张买单取该行”，不做多行择优。
- [ ] **Step 4: 运行确认通过** — 同上命令，预期全部 PASS。

### Task 2: 服务参数与接口

**Files:**
- Modify: `backend/app/services/first_board.py`（`opportunities` 增加 `verify: bool = False`）
- Modify: `backend/app/api/first_board.py`（`opportunities` 路由增加 `verify: bool = False`）
- Modify: `docs/first-board.md`（机会记录小节补核验说明）
- Test: `backend/tests/test_first_board_api.py`

**Interfaces:**
- Consumes: Task 1 的三个函数；`self.repo.get_daily_batch(symbols, start, end, columns=[...])`；`build_paper_trade_audit(data_dir, account_id, *, events, start, end, revision)`；所选版本的 `paper_account_id` 取自 `self._versions` 中同 `revision` 的条目。
- Produces: `FirstBoardService.opportunities(day, *, revision=None, verify=False) -> dict`；路由 `verify` 查询参数。

- [ ] **Step 1: 写失败测试**（沿用 `client` 夹具与 `_opportunity_events`）：
  - `test_opportunities_verify_is_off_by_default`：默认响应不含 `verified`，行里没有 `close`、`paper`。
  - `test_opportunities_verify_adds_close_and_paper`：给 `service.repo.get_daily_batch` 赋值返回含两只股票当日日线的 DataFrame（一只收盘等于涨停价，一只缺行），monkeypatch `app.services.first_board_trade_audit.build_paper_trade_audit` 返回含 `trades` 的假审计；断言 `close`、`paper`、计数与审计被调用时的 `account_id`、`start == end == day`、`revision`。
  - `test_opportunities_verify_without_account_skips_audit`：不设账户 → 每行 `paper.status == "no_account"`，审计函数未被调用。
  - `test_opportunities_verify_before_close_marks_today_unverified`：`fb._now` 设为当日 10:00 → 全部 `close.status == "unverified"`。
  - `test_opportunities_verify_survives_daily_read_failure`：`get_daily_batch` 抛 `ValueError` → `close` 全部 `unverified` 且接口 200。
- [ ] **Step 2: 运行确认失败** — `uv run --no-sync pytest tests/test_first_board_api.py -k opportunities_verify -q`，预期失败（参数不存在）。
- [ ] **Step 3: 实现**：`verify=False` 走原路径；`verify=True` 时读日线（列取 Task 1 所需，读取失败捕获 `OSError`、`ValueError`、`pl.exceptions.PolarsError` 并转为全部 `unverified`），`closed` 为 `day < 今日` 或今日且北京时间 ≥ 15:00；有账户时仅对有买入候选事件的情形调用审计，其余按 Task 1 规则；用 `apply_verification` 合并。
- [ ] **Step 4: 运行确认通过并回归** — `uv run --no-sync pytest tests -k first_board -q`，预期全部通过。
- [ ] **Step 5: 文档** — `docs/first-board.md` 机会记录小节补核验口径与“不自动、不持久化”。

### Task 3: 前端按钮与两列

**Files:**
- Modify: `frontend/src/lib/api.ts`（类型与 `firstBoardOpportunities(day, verify = false)`）、`frontend/src/lib/queryKeys.ts`（键加 `verify`）
- Modify: `frontend/src/custom/first-board/OpportunityList.tsx`
- Test: `frontend/src/custom/first-board/OpportunityList.test.tsx`、`frontend/src/custom/first-board/FirstBoard.test.tsx`（调用签名变化处）

**Interfaces:**
- Consumes: Task 2 的响应字段。
- Produces: `api.firstBoardOpportunities(day: string, verify?: boolean)`；`QK.firstBoardOpportunities(day: string, verify: boolean)`。

- [ ] **Step 1: 写失败测试**：点击“核验收盘与成交”后以 `(day, true)` 请求；出现“收盘封板 x 只”“已核验模拟成交 x 只”及分母；表格出现“收盘结果”“模拟成交”两列，文案为收盘封板／收盘未封／待核验，以及未关联账户、未下单、待成交、已成交、已平仓（含收益）、来源不唯一、待核验；核验请求失败时仍显示未核验表格并可重试；未点击时不出现这两列。
- [ ] **Step 2: 运行确认失败** — `cd frontend; pnpm exec vitest run src/custom/first-board/OpportunityList.test.tsx`，预期失败。
- [ ] **Step 3: 实现** 类型、请求、键与组件；按钮点后 `verify` 置真，核验前保留已有未核验数据；默认请求仍为 `(day, false)`。
- [ ] **Step 4: 运行确认通过并回归** — `pnpm exec vitest run src/custom/first-board`、`pnpm exec tsc --noEmit -p .`，预期通过。
- [ ] **Step 5: 浏览器核对** — 用临时数据目录起后端，种入事件档案；日线与账本在该环境不可得时，核验应显示待核验与未关联账户，并如实说明未覆盖真实日线与账本。

## Self-Review

- 规格覆盖：接口与参数（Task 2）、收盘判定与待核验原因（Task 1）、成交汇总规则与分母（Task 1）、按钮与展示（Task 3）、默认不变与不持久化（全局约束与 Task 2 测试）。
- 类型一致：`close`、`paper` 字段名与状态枚举在三个任务中一致；`QK` 与 `api` 的 `verify` 参数同名。
