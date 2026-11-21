# 首板「机会记录」读侧聚合 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新增只读接口 `GET /api/first-board/opportunities`，把某日某规则版本的首板事件按股票聚合成“机会记录”。

**Architecture:** 纯函数 `aggregate_opportunities` 只做聚合，不读文件、不读行情；`FirstBoardService.opportunities` 负责参数校验、读取事件档案和选版本（与首次提示排名共用同一段版本选择）；路由只做参数绑定。事件写入、通知、回放、排名、模拟买入一律不动。

**Tech Stack:** Python 3、FastAPI、pytest（`backend/` 下运行，用 `uv run`）。

**Spec:** `docs/superpowers/specs/2026-11-21-first-board-opportunities-design.md`

## Global Constraints

- 所有对话与文档用中文；代码、路径、标识符保持原文。
- 只读：接口不得写入或改动事件档案（测试须断言文件字节不变）。
- 时间一律由事件的 `ts`（毫秒整数）转北京时间（`app.market_time.CN_TZ`），输出 `datetime.isoformat()`。
- `ts` 必须是 `int` 且不是 `bool`；否则该事件跳过并计入 `summary.skipped_count`，不猜测。
- `pattern == "exit"` 的事件不参与聚合，也不计入 `event_count`。
- 不提供收盘结果和模拟成交字段；`last_state` 只表示盘中最后一次观察。
- 版本默认选法与 `first_alert_rankings` 相同：有 `buy_candidate` 的最新版本，否则当天最新版本，否则当前配置版本；所选日期没有该版本时 `ValueError("所选日期没有这个规则版本的记录")`（路由转 400）。
- 工作区是 CRLF，脚本改写文件须保持 `\r\n`；后端改动可能让 dev 栈退出，验证用测试，不依赖运行中的服务。
- 不自动提交；提交前由用户确认。

## Review Focus

- 档案里事件顺序不可靠（`events()` 返回倒序，同毫秒多条）：聚合结果须与输入顺序无关。（Task 1）
- 同股多形态同一毫秒提示：`first_alert.pattern` 取 `(pattern, id)` 字典序最小，与排名的 `_first_alert_trades` 一致。（Task 1、2）
- 只有 `broken`、没有 `sealed`：`broken` 与 `resealed` 均为 `null`，不能凭空推断触板。（Task 1）
- 触板→开板→回封→再开板：`broken` 取首次开板，`resealed` 取首次回封，不被后来的开板覆盖。（Task 1）
- 其他规则版本的事件、`exit` 事件混在同一天档案里：不得进入结果。（Task 1、2）
- 当天档案不存在或为空、日期在未来：空列表与 400，而不是 500。（Task 2）

---

### Task 1: 聚合纯函数

**Files:**
- Create: `backend/app/services/first_board_opportunities.py`
- Test: `backend/tests/test_first_board_opportunities.py`

**Interfaces:**
- Consumes: 事件字典列表，字段 `id, ts, symbol, name, pattern, pattern_label, event_type, revision, price, blocked_reasons`（与 `FirstBoardService._event` 产出一致）。
- Produces: `aggregate_opportunities(events: Iterable[dict], *, day: date, revision: int) -> dict`，返回 `{"day": str, "revision": int, "summary": {...}, "rows": [...]}`。`available_revisions` 由 Task 2 的服务层补充，不在此函数内。

返回结构：

```python
{
  "day": "2026-10-16", "revision": 4,
  "summary": {"stock_count": int, "alerted_count": int, "touched_count": int,
              "sealed_unalerted_count": int, "skipped_count": int},
  "rows": [{
    "symbol": str, "name": str,
    "patterns": [{"pattern": str, "pattern_label": str}],      # 按首次出现先后
    "first_alert": {"at": str, "price": float | None, "pattern": str, "event_id": str} | None,
    "first_touch": {"at": str, "price": float | None} | None,  # 最早 sealed
    "broken": str | None,      # first_touch 之后首次 broken 的 at
    "resealed": str | None,    # 该 broken 之后首次 sealed 的 at
    "last_state": {"event_type": str, "at": str},
    "first_record": {"at": str, "event_type": str, "blocked_reasons": list[str]},
    "alerted": bool, "sealed_unalerted": bool, "event_count": int,
  }],
}
```

`rows` 按 `(first_record.at, symbol)` 升序。`touched_count` = `first_touch` 非空的股票数。

- [ ] **Step 1: 写失败测试** `backend/tests/test_first_board_opportunities.py`，测试辅助 `ev(id, minute, symbol, event_type, pattern="platform", revision=4, **extra)`，`ts` 取 2026-10-16 北京时间 10:`minute` 的毫秒值。用例（名称与断言）：
  - `test_merges_patterns_of_one_stock_into_one_row`：同股 platform/trend 各一条 `buy_candidate` 不同分钟 → 1 行，`patterns` 为两个形态按先后，`first_alert.pattern` 为较早者，`event_count == 2`。
  - `test_same_millisecond_alert_picks_smallest_pattern_then_id`：两形态同 `ts` → `first_alert.pattern == "platform"`（字典序小于 `trend`）。
  - `test_full_lifecycle_alert_touch_break_reseal`：`buy_candidate`→`sealed`→`broken`→`sealed`→`broken` → `first_touch`、`broken`（首次）、`resealed`（首次回封）正确，`last_state.event_type == "broken"`，`alerted` 为真，`sealed_unalerted` 为假。
  - `test_sealed_without_alert_is_flagged_and_keeps_first_record`：只有 `approaching`（带 `blocked_reasons=["行业强度不足"]`）后 `sealed` → `sealed_unalerted` 真，`first_record.event_type == "approaching"` 且原因保留；`summary` 中 `alerted_count == 0, touched_count == 1, sealed_unalerted_count == 1`。
  - `test_broken_without_prior_touch_is_not_inferred`：只有 `broken` → `first_touch` 与 `broken`、`resealed` 均为 `None`。
  - `test_result_is_independent_of_input_order`：同一批事件正序与倒序传入，结果相等。
  - `test_ignores_other_revisions_and_exit_events`：另一版本事件、`pattern="exit"` 的 `exit_candidate` 不出现；`event_count` 不含它们。
  - `test_skips_events_without_valid_ts_and_counts_them`：`ts` 缺失、字符串、`True` 各一条 → 跳过，`summary.skipped_count == 3`，其余事件正常聚合。
  - `test_empty_input_returns_empty_report`：空输入 → `rows == []`，各计数为 0。
- [ ] **Step 2: 运行确认失败** — `cd backend; uv run --no-sync pytest tests/test_first_board_opportunities.py -v`，预期：`ModuleNotFoundError`/导入失败。
- [ ] **Step 3: 实现** `aggregate_opportunities`（`first_board_opportunities.py`）：先把合法事件按 `(ts, pattern, id)` 排序再逐只股票归并，保证与输入顺序无关；`day` 只用于输出，事件日期以 `ts` 为准。`ts` 校验用 `type(ts) is int`。
- [ ] **Step 4: 运行确认通过** — 同上命令，预期全部 PASS。
- [ ] **Step 5: 提交（待用户确认后）** — `git add backend/app/services/first_board_opportunities.py backend/tests/test_first_board_opportunities.py`，信息 `feat(first-board): 新增按股票聚合首板事件的纯函数`，带 `-s` 与 Co-Authored-By 行。

---

### Task 2: 服务方法、路由与接口测试

**Files:**
- Modify: `backend/app/services/first_board.py`（`first_alert_rankings` 附近，约 1252–1296 行；新增 `opportunities`，并把版本选择抽成 `_select_revision`）
- Modify: `backend/app/api/first_board.py`（`first_alert_rankings` 路由之后）
- Modify: `docs/first-board.md`（「信号记录」相关小节补一段接口说明，并注明“L3 局部扩展、不改写入链路”）
- Test: `backend/tests/test_first_board_api.py`

**Interfaces:**
- Consumes: Task 1 的 `aggregate_opportunities`；服务现有 `_validation_path`、`_read_day`、`_file_signature` 的调用方式见 `first_alert_rankings`。
- Produces:
  - `FirstBoardService._select_revision(records: list[dict], revision: int | None, fallback: int) -> tuple[int, list[int]]`，返回 `(选中版本, 降序可用版本列表)`，无该版本时抛 `ValueError("所选日期没有这个规则版本的记录")`；`first_alert_rankings` 改为调用它，行为不变。
  - `FirstBoardService.opportunities(self, day: date, *, revision: int | None = None) -> dict`：返回 Task 1 的报告并加 `available_revisions`。
  - 路由 `GET /api/first-board/opportunities`，查询参数 `day: date`（必填）、`revision: int | None = Query(None, ge=0)`；`ValueError` 转 400。

- [ ] **Step 1: 写失败测试**（`test_first_board_api.py`，沿用 `client` 夹具和 `service._write_day`、`fb._now` monkeypatch）：
  - `test_opportunities_endpoint_aggregates_one_row_per_stock_and_is_read_only`：写入同日两只股票共 5 条事件（含一只 `sealed` 未提示、一只多形态），断言行数、`first_alert`、`sealed_unalerted`、`summary`，以及请求前后事件文件字节不变。
  - `test_opportunities_default_revision_matches_rankings`：沿用排名测试的多版本事件（版本 4、5 有买入，6 只有 `watch`），断言 `opportunities` 默认版本与 `first-alert-rankings` 的 `revision` 相同，`available_revisions` 相同，`revision=2` 返回 400。
  - `test_opportunities_first_alert_time_matches_rankings_signal_at`：同日同版本，每只股票的 `first_alert.at` 等于排名行的 `signal_at`。
  - `test_opportunities_reports_empty_future_and_invalid_queries`：无档案 → 200 且 `rows == []`；日期晚于当前日 → 400；缺 `day` → 422；`revision=-1` → 422。
- [ ] **Step 2: 运行确认失败** — `uv run --no-sync pytest tests/test_first_board_api.py -k opportunities -v`，预期：404（路由不存在）。
- [ ] **Step 3: 实现** `_select_revision`、`opportunities` 与路由。`opportunities` 的校验和读取方式与 `first_alert_rankings` 一致（日期不得晚于北京当日、`revision` 类型与范围、读 `_validation_path("events", f"{day}.json")`，文件不存在按空事件处理，不加缓存）；先抽 `_select_revision` 并确认排名相关测试仍绿，再接新方法。
- [ ] **Step 4: 运行确认通过** — `uv run --no-sync pytest tests/test_first_board_opportunities.py tests/test_first_board_api.py tests/test_first_board_service.py -v`，预期全部 PASS；再跑 `uv run --no-sync pytest tests -k first_board -q`，预期无新增失败（失败项如实记录）。
- [ ] **Step 5: 文档** — 在 `docs/first-board.md`「信号记录」流程附近补接口说明（字段含义、`last_state` 不是收盘结果、不含收盘与模拟成交核验）；保持 CRLF。
- [ ] **Step 6: 检查 diff 并提交（待用户确认后）** — `git diff --stat` 只应含上述文件；信息 `feat(first-board): 新增机会记录读侧聚合接口`，带 `-s` 与 Co-Authored-By 行。

---

## Self-Review

- **规格覆盖**：接口与参数（Task 2）、行字段与汇总（Task 1）、版本选择复用（Task 2 `_select_revision`）、排除 `exit`、`ts` 校验、只读、与排名 `signal_at` 一致、无缓存（Task 2）、不含收盘与模拟成交（全局约束）、文档说明（Task 2 Step 5）均有对应。
- **类型一致**：`aggregate_opportunities` 的返回由 `opportunities` 原样透传并只追加 `available_revisions`；`_select_revision` 的返回顺序在两处调用一致。
- **范围**：不含前端、不含缓存、不含通知去抖，与设计文档非目标一致。
