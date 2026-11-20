# 持仓做T / 持仓分析：猫爪阶段 1 实施计划

> **执行方式**：使用 superpowers:executing-plans（本会话内由 AI 直接实施）。步骤用复选框 `- [ ]` 跟踪。

**目标**：
- 做T 读取当日交易所涨跌停价和日级停牌名单，对大部分持仓不再全部暂停。
- 持仓分析新增 ST 和停牌两类风险状态，命中后须先确认才能采纳。

**架构**：
- 新增 `suspensions` 数据集，接入方式与 `st_status` 相同（能力路由、盘前同步、按日落盘）。
- `market_reference.day_reference(day)` 统一读取当日的三张表。
- 做T 和持仓分析只通过这个函数取数，业务层不直接依赖猫爪。

**技术栈**：Python / FastAPI / polars / pytest；React + TS / vitest（前端用 `pnpm exec`，不要用 npx）。

**设计文档**：`docs/superpowers/specs/2026-11-19-holding-meoz-stage1-design.md`

## 全局约束

- 缺数据时一律视为「未知」：不能判为可交易，也不能判为无风险。
- 不能用接收时间冒充市场时间。
- 风险状态不改变 `ready`、情景和价位。
- 工作区文件是 CRLF 换行，脚本改写时必须保留 `\r\n`。
- 不自动提交。

## 评审重点（测试不容易覆盖、最可能出问题的地方）

1. **当天没有停牌名单文件时：** 做T 必须显示 `unknown` 并暂停；持仓分析必须显示「停牌名单不可用」，并且需要用户确认。由任务 2、3 的测试覆盖。
2. **周末或盘后做分析时：** 风险名单按最近一个交易日的文件取，并在 `as_of` 中写明日期。由任务 3 的测试覆盖。
3. **上市日期未知或不在证券目录中的股票：** 不能判为 `trading`。由任务 2 的测试覆盖。
4. **一字涨停时只有一边盘口为空：** 不能误报疑似停牌。由任务 2 的测试覆盖。
5. **旧报告缺少 `risk_status` 字段时：** 采纳必须按当前风险重新核对，有风险就要求确认，不能直接放行。由任务 4 的测试覆盖。

---

### 任务 1：`suspensions` 数据集与 `day_reference` 读取

**文件**
- `app/data_providers/schemas.py`：新增 `SUSPENSION_SCHEMA`。
- `app/plugins/meoz/provider.py`、`plugin.yaml`：实现 `get_suspensions`，`datasets` 中加 `suspensions`。
- `app/data_providers/capabilities.py`、`app/services/preferences.py`、`app/api/settings.py`：在 `limit_events` 后面照抄一份 `suspensions`，标签「停牌」，描述「按日的停牌名单」。
- 前端：`frontend/src/lib/api.ts`、`provider-routing.ts`、`provider-routing.test.ts`、`pages/settings/DataSources.tsx`。
- `app/services/market_reference.py`：`DATASETS` 中新增一项，并新增 `day_reference`。
- `app/custom/market_reference.py`：盘前任务改为 09:05 和 09:20，同步 `price_limits`、`st_status`、`suspensions`。
- 测试：`tests/test_market_reference.py`、`tests/test_capability_matrix.py`。

**接口**
- `SUSPENSION_SCHEMA = {"date": Date, "symbol": String, "timing": String, "source": String}`
- `MeozProvider.get_suspensions(day) -> pl.DataFrame`
  - 查询 `suspend`，参数为 `{"startdate": ymd, "enddate": ymd}`。
  - 先用 `_same_day` 校验交易日，再只保留 `suspend_type == "S"` 的行。
  - `timing` 取 `suspend_timing`。
  - 结果按 `symbol` 去重。
- `market_reference.DATASETS["suspensions"] = Spec("get_suspensions", SUSPENSION_SCHEMA, date(2020, 1, 2), time(9, 0))`
- `market_reference.day_reference(day) -> dict`，返回以下三项：
  - `"price_limits"`：`{symbol: {"limit_up", "limit_down", "no_limit"}}`；
  - `"st"`：`{symbol: st_type}`；
  - `"suspensions"`：`{symbol: timing}`。
  - 某张表当天没有文件，或 reader 未启用时，对应项为 `None`。
  - 按 `(day, 三个文件的 mtime)` 做进程内缓存。

**步骤**
- [ ] 写测试：
  - `test_suspensions_keep_suspend_rows_only`：输入 S 行和 R 行，只保留 S；后缀补全；`timing` 为 None 或 "09:30-10:30" 时都原样保留；请求参数为 startdate/enddate。
  - `test_day_reference_none_when_missing`
  - `test_day_reference_reads_three_tables`：用 tmp_path 写入 parquet，并调用 `enable_reader`。
  - 能力矩阵测试：`REFERENCE_CAPS` 加上 `suspensions`。
- [ ] 运行 `uv run pytest tests/test_market_reference.py tests/test_capability_matrix.py -q`，确认新测试失败。
- [ ] 实现。
- [ ] 运行同一条命令，确认通过；再运行 `pnpm exec vitest run src/lib/provider-routing.test.ts`。

### 任务 2：做T 的涨跌停价、日级状态和疑似停牌

**文件**
- `app/services/position_t_market.py::_quote`
- 测试：`tests/test_position_t_market.py`

**接口**
- 依赖：`market_reference.day_reference(now.date())`。
- 产出：quote 新增以下字段：
  - `price_limit_source`：`"quote" | "exchange_daily" | "theoretical"`；
  - `trade_status_basis`：`"intraday" | "daily" | "none"`。

**规则**
- **涨跌停价**：
  1. 报价自带有效的 `limit_up_price`/`limit_down_price` 时，用报价的值，来源为 `quote`。
  2. 否则交易所价存在时：
     - `no_limit=True`：追加原有的错误「当日无涨跌幅限制，首版暂停新建计划」。
     - 否则使用交易所价，来源为 `exchange_daily`。
  3. 都没有时：来源为 `theoretical`，并保留原有错误「理论价仅作观察」。
- **交易状态**：
  1. 报价或盘口明确给出状态时，basis 为 `intraday`。
  2. 否则当日停牌名单存在时：
     - 该股在名单中：判为 `suspended`，basis 为 `daily`，追加错误「当日停牌名单含该股」。
     - 该股不在名单中，且 `instrument` 与上市日期都已确认：判为 `trading`，basis 为 `daily`。
     - 上市日期等信息未确认：判为 `unknown`，追加原有错误。
  3. 名单不存在时：判为 `unknown`，追加原有错误。
- **疑似停牌**：盘口 `bid_prices[0]` 和 `ask_prices[0]` 都缺失或 ≤0 时，追加错误「买卖一均为空，疑似盘中停牌」。

**步骤**
- [ ] 写测试（用 monkeypatch 替换 `day_reference`）：
  - `test_exchange_daily_limits_used_when_quote_lacks`：没有「理论价仅作观察」，`price_limit_source == "exchange_daily"`。
  - `test_exchange_no_limit_blocks`
  - `test_daily_status_trading_when_not_suspended`：`trade_status == "trading"`、basis 为 `daily`，且 `data_errors` 中没有与状态相关的错误。
  - `test_daily_status_suspended_when_listed`
  - `test_status_unknown_without_suspension_table`
  - `test_status_unknown_without_listing_date`
  - `test_both_sides_empty_flags_suspected_halt`
  - `test_one_side_empty_no_halt_flag`
- [ ] 运行 `uv run pytest tests/test_position_t_market.py -q`，确认失败。
- [ ] 实现。
- [ ] 跑全部 `tests/test_position_t_*.py`，确认通过。

### 任务 3：持仓分析的风险状态

**文件**
- `app/services/holding_analysis.py`
- 测试：`tests/test_holding_analysis.py`

**接口**
- `risk_day(now) -> date | None`：取 ≤ `now.date()` 的最近一个交易日（用 `load_market_calendar`）；日历失败时返回 None。
- `risk_status(symbol, day) -> {"items": [...], "as_of": iso | None, "fingerprint": str}`
  - 每项为 `{"id", "label", "detail", "source"}`，`id` 取值：
    - `st`：label「风险警示」，detail 为 ST 类型；
    - `suspended`：label「停牌」，detail 为「全天」或时段；
    - `st_unknown`：label「ST 名单不可用」；
    - `suspension_unknown`：label「停牌名单不可用」。
  - `day` 为 None 时，只返回两个 unknown 项。
  - `fingerprint` 用现有的 `_hash` 对 items 计算。
- `_analyze_row` 生成的每行加上 `risk_status`，取值来自 `analyze_holdings` 中计算好的结果。

**步骤**
- [ ] 写测试：
  - `test_risk_status_hits_st_and_suspension`
  - `test_risk_status_unknown_when_tables_missing`
  - `test_risk_status_uses_latest_session_on_weekend`
  - `test_risk_does_not_change_ready`
- [ ] 确认失败 → 实现 → 运行 `uv run pytest tests/test_holding_analysis.py -q`，确认通过。

### 任务 4：采纳时确认风险、概览展示风险变化、提醒暂停

**文件**
- `app/api/holding_analysis.py`
- `app/services/holding_analysis_store.py::adopt_plan`
- `app/services/holding_alert_guard.py`
- 测试：`tests/test_holding_analysis_api.py`、`tests/test_holding_alert_guard.py`

**接口**
- `AdoptBody.acknowledged_risks: list[str]`，默认 `[]`，最多 10 项。
- 采纳接口在调用 store 之前，先计算当前风险：`risk_status(row.symbol, risk_day(cn_now()))`，然后：
  - 报告行没有 `risk_status`，或其 fingerprint 与当前不同：返回 409「风险状态已变化，请重新分析」。
  - 当前风险项 id 集合不是 `acknowledged_risks` 的子集：返回 409「请先确认全部风险」。
- `adopt_plan(report_id, row_key, expected_input_version, acknowledged_risks=())`：计划中写入 `acknowledged_risks` 和 `risks_acknowledged_at`。
- **概览**：对每个已采纳计划重新计算风险；fingerprint 与计划行中的不同时，往 `review_reasons` 中追加「风险状态已变化」。
- **提醒检查**：fingerprint 不一致时跳过该计划，即不放行提醒。

**步骤**
- [ ] 写测试：
  - `test_adopt_requires_ack_of_all_risks`
  - `test_adopt_conflict_when_risk_changed`
  - `test_adopt_legacy_report_without_risk_status_conflicts`
  - `test_adopt_saves_acknowledged_risks`
  - `test_overview_flags_risk_change`
  - `test_guard_skips_plan_when_risk_changed`
- [ ] 确认失败 → 实现 → 跑 `tests/test_holding_*.py`，确认通过。

### 任务 5：前端

**文件**
- `frontend/src/custom/lots/HoldingAnalysis.tsx` 及其测试
- `PositionTMonitor.tsx`
- `frontend/src/lib/api.ts`：类型和 `adopt` 的参数

**内容**
- **持仓分析行**：
  - 显示一个标签：有命中项时为「风险 N」；只有 unknown 项时为「风险未知」；没有任何项时不显示。
  - 展开区列出每项的 label、detail、来源，以及「截至 {as_of}」。
  - 点「采纳」时，若风险项不为空，弹出确认框，每项一个「已知悉」复选框；全部勾选后才能点「确认采纳」。提交时带上 `acknowledged_risks`。
  - 已采纳计划的 `review_reasons` 已经会展示，不需要额外处理。
- **做T 信号详情**：`trade_status_basis === "daily"` 时显示「日级状态已确认，盘中状态未确认」；同时显示涨跌停价来源。

**步骤**
- [ ] 写测试：
  - `shows risk badge and requires all acknowledgements before adopting`
  - `adopts directly when no risks`
- [ ] 确认失败 → 实现 → 运行 `pnpm exec vitest run src/custom/lots` 和 `pnpm exec tsc --noEmit -p .`，确认通过。

### 任务 6：文档

- `docs/position-t-monitor.md`：改写「证券状态」一行和第 74 行的说明。
- `docs/position-t-contract.md`：做相应的同步修改。
- `docs/holding-analysis.md`：在「数据与降级」中加入风险状态与确认流程。
- 规划文档 11.5 节：标注阶段 1 已实施。
- 最后一步：查看 `git status` 和 `git diff --stat`，并跑一遍相关测试集。
