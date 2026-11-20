# 资金证据（持仓分析 + 做T）实施计划

> **执行方式**：superpowers:executing-plans + TDD（先写失败测试）。**待设计文档第 7 节口径问题确认后才开始实现。**
> 设计：`docs/superpowers/specs/2026-11-20-fund-evidence-design.md`。
> 前端用 `pnpm exec`，不要用 npx；工作区 CRLF，脚本改写保留 `\r\n`；不自动提交。

## 全局约束

- 资金证据只展示：不改变 `can_adopt`、`ready`、情景价位、风险指纹、信号状态、数量。每个任务都要有「改变资金证据不改变判定」的回归断言。
- 三态：`ok` / `empty`（成功但无记录）/ `unavailable`（来源不可用）。`unavailable` 与 `empty` 永不输出为 0。
- 每项带 `source`、`unit`、`data_through`、`available_at`；主力净额带 `model_basis="vendor_model"`。
- 业务层不读厂商字段，只读 provider 转换后的源中立契约。

## 任务 1：provider 契约（`app/plugins/meoz/provider.py`、`plugin.yaml`）

- [ ] 测试（`tests/test_meoz_fund_evidence.py`）：`get_fund_flow(symbols, start, end)`、`get_fund_flow_now(symbols)`、`get_margin_balance(symbols, start, end)`、`get_northbound(symbol)` 的单位（元/股）、卖出为负、日期转 ISO、空数据返回空结构、字段缺失行被跳过、403/502 抛异常（由上层标不可用）。
- [ ] 实现四个方法；`fundflow_kp` 用 `service="live"`，`margin_detail`、`northbound_holding` 用 `service="history"`；`screening` 用 `fields="symbol,tradedate,main_net_amount"`。
- [ ] `plugin.yaml` `datasets` 追加 `fund_flow`、`margin_balance`、`northbound_holding`，`description` 补一句。
- [ ] 北向按 6 位代码单只查询（待 Q7），取最近两期，不做编码映射；测试断言「返回行全部归属所查代码」。

## 任务 2：服务 `app/services/fund_evidence.py`

- [ ] 测试（`tests/test_fund_evidence.py`）：
  - 窗口：盘中/午休不含当日；15:30 后含当日（待 Q2）；周末/节假日取最近交易日（用 `load_market_calendar`）。
  - 5 日合计、方向、日明细；缺中间某日 → 标记缺口，不补 0。
  - 龙虎榜：从 `dragon_tiger` 逐日载荷中按 `thscode` 抽取上榜原因、净买额、游资席位；不在榜 = `empty`；某日 `no_data`/`source_unavailable` → 整项 `unavailable`；逐日带 `source`。
  - 融资：最新可得日 vs 前第 5 个有数据交易日；非两融 → `empty`。
  - 北向：最近两期差；仅一期则只给当期。
  - TTL 缓存命中/失效、并发（不持锁取数）；取数异常 → `unavailable` 且不抛出。
  - `evidence_text`：不可用时的固定句式「资金数据不可用，不能视为无资金流入/流出」；任何路径都不出现「0 元」代替不可用。
- [ ] 实现 `fetch_*`、`summarize_holding(...)`、`evidence_text(...)`、`visible_at(evidence, moment)`、`intraday_main_net(symbols, now)`（4 分钟缓存）。

## 任务 3：持仓分析接入（`services/holding_analysis.py`，局部追加）

- [ ] 测试（`tests/test_holding_analysis.py` 追加）：行带 `fund_evidence` 与 `evidence` 文字；`can_adopt`、`risk_status.fingerprint`、情景价位与有无资金证据**完全一致**；来源不可用时研究仍完成、`warnings` 不新增阻断项；ETF 不取资金证据。
- [ ] 在 `analyze_holdings` 业绩事件之后追加取数与行字段，保持与既有写法相同的隔离方式。
- [ ] 核查 AI 解读提示词（`test_holding_interpretation.py` 对应实现）：证据依据列表中的资金句子只能引用；增加测试断言提示词含「不得推断主力意图」类约束，若不含则最小补充。

## 任务 4：做T 接入（`position_t_market.py`、`strategy/position_t.py`、`position_t_service.py`）

- [ ] 测试（`tests/test_position_t_market.py`、`test_position_t_rules.py`、`test_position_t_live_monitor.py` 追加）：`environment["fund_evidence"]` 进入信号；**同一行情、不同资金证据（含缺失）下信号状态/价位/数量逐字段相同**；触发事件写入 `signal_history.payload.signal.fund_evidence` 且带 `available_at`；过期 >10 分钟显示不可用；非交易时段不取数。
- [ ] `env` 追加一键；信号结果追加一键；不改任何判定分支。

## 任务 5：前端

- [ ] `frontend/src/lib/api.ts`：类型 `FundEvidence`（三态、单位、`data_through`、`available_at`）；`HoldingAnalysisRow.fund_evidence?`、`PositionTSignal.fund_evidence?`。
- [ ] `HoldingAnalysis.tsx`：独立组件 `FundDetails`，展开区 `AuthenticDetails` 之后；文案显示来源、单位、截止日、「厂商模型口径」；不可用「资金数据不可用」；红涨绿跌沿用约定并带正负号；主列表不新增列。
- [ ] `PositionTMonitor.tsx`：信号详情一行当日主力净额 + 取数时刻 + 约 4 分钟更新提示；候选行不变。
- [ ] 组件测试：加载/不可用/空/有数据；`pnpm exec vitest run` 受影响文件，`pnpm build`。

## 任务 6：文档与验证

- [ ] 更新 `docs/holding-analysis.md`（「数据与降级」不再写「未接入资金流」，说明展示-only 与口径）、`docs/position-t-contract.md`、`docs/custom-data-source.md`（三个新数据集契约）、规划文档第 11.5 节进度。
- [ ] 后端：`uv run pytest tests/test_fund_evidence.py tests/test_meoz_fund_evidence.py tests/test_holding_*.py tests/test_position_t_*.py tests/test_earnings_events.py tests/test_meoz_provider.py tests/test_meoz_dragon_tiger.py -q`；`uv run ruff check` 新增/改动文件。
- [ ] 前端 `pnpm build`；`git diff --check`；`git status` 与 `git diff --stat` 说明改动与剩余风险。
- [ ] 交易时段补测：盘中 `fundflow_kp` 当日行更新节奏、15:00 后定版时点，结果回写核对表。

## 评审重点

1. 盘中不得把「当日累计」混进 5 日合计，也不得把 T-1 融资、季度北向写成当日。
2. `unavailable`/`empty` 路径任何地方都不得出现 0 值或「无资金流入」文案。
3. 做T 判定对资金证据零依赖（对照测试）。
4. 与 C、D 在 `holding_analysis.py`、`HoldingAnalysis.tsx` 的合并冲突仅限相邻行。
