# 首板机会记录：收盘结果与模拟成交核验（设计，已确认）

> 日期 2026-11-21。状态：用户已确认方案（可选核验参数加手动按钮）。在 [机会记录读侧聚合](2026-11-21-first-board-opportunities-design.md) 之上补两项核验，不改事件写入、通知、回放、排名和模拟盘账本。

## 1. 目的

机会记录目前只给事件侧的时间点。复盘还要回答两件事：这只股票收盘到底有没有封住；提示之后有没有真的在模拟盘里买到、结果如何。两项数据分别来自日线和模拟盘账本，与盘中事件口径不同，必须分开标注、证据不足时写“待核验”，不能用事件状态顶替。

## 2. 接口

`GET /api/first-board/opportunities?day=&revision=&verify=true`，`verify` 缺省为 `false`，此时返回与现在完全一致（盘中轮询不变重）。`verify=true` 时报告多出：

- 每行 `close`：`{status, close_price, limit_price, reason}`，`status` 为 `sealed | not_sealed | unverified`。
- 每行 `paper`：`{status, reason, net_return}`，`status` 为 `no_account | no_signal | not_ordered | pending | cancelled | expired | open | partially_closed | closed | ambiguous | evidence_missing`。
- 报告 `verified: true`，`summary` 增加 `close_sealed_count`、`close_checked_count`、`paper_filled_count`、`paper_checked_count`。

## 3. 口径

**收盘结果**：读当日日线。原价收盘价与涨停价（`observation_limit_price`，历史口径）相差不超过 0.005 为 `sealed`，否则 `not_sealed`。以下一律 `unverified` 并写原因：当日是今天且未到 15:00、日线缺该股、缺原价或涨停价、价格自相矛盾（收盘或最高高于涨停价、收盘高于最高）、日线读取失败。`close_checked_count` 为非 `unverified` 的股票数，分母是 `stock_count`。

**模拟成交**：账户取所选规则版本的 `paper_account_id`，没有则全部 `no_account`。调用现有 `build_paper_trade_audit`（只读，按“事件→委托→成交→平仓”归因），再按股票汇总其全部买入候选事件的归因行：

- 没有买入候选事件 → `no_signal`。
- 全部归因行的买单合计超过一张 → `ambiguous`（来源不唯一，不选其一）。
- 恰有一张买单 → 取该行状态；`closed` 带 `net_return`（该行 `closed_net_return`）。
- 没有买单 → 全部行为 `not_ordered` 时 `not_ordered`，否则 `evidence_missing`。
- 审计结果为不可用、出错或明细被截断（`coverage.truncated`）→ 全部提示股 `evidence_missing`，原因取审计给出的说明。

`paper_filled_count` 为 `open | partially_closed | closed` 的股票数；`paper_checked_count` 为提示股里结论明确（非 `evidence_missing | ambiguous | no_account`）的股票数，分母是 `alerted_count`。

## 4. 前端

机会记录区增加“核验收盘与成交”按钮，点击后以 `verify=true` 请求；结果出来后汇总多“收盘封板 x 只（已核日线 N/M）”“已核验模拟成交 x 只（已核提示股 N/M）”，表格多“收盘结果”“模拟成交”两列。不自动核验，不进入轮询，不保存结果。

## 5. 目标、非目标、反目标

**目标**：收盘与成交结论都带证据来源和分母，证据不足一律显示“待核验”。

**非目标**：不新增持久化、不读行情接口、不改账本和事件；不做按版本或账户的选择器；不提供组合收益。

**反目标**：把盘中最后观察当作收盘结果；在多张买单时挑一张算盈亏；盘后日线未同步时把“缺数据”算成“未封”；核验拖慢盘中信号页。

## 6. 方案对比

| 方案 | 取舍 |
| --- | --- |
| **A. `verify` 可选参数 + 手动按钮（选定）** | 默认路径不变；核验按需付费（整本账本校验） |
| B. 每次都附带核验 | 盘中 5 秒轮询会反复整本校验账本 |
| C. 单独的核验接口 | 前端要合并两次响应，分母与行集合容易对不上 |
