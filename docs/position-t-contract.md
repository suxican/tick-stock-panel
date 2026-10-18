# 持仓做 T 首版内部契约

实现基线：30794343。本文为本次新增能力的接口约定，不描述既有可用 API。
现有 `/api/lots` 和旧批次提醒保持兼容。首版 manual / simulation，不接券商、不自动下单。

## 公共约定

- JSON 数量整数股，价格/金额对外 number，内部 Decimal 或定点；未知为 null。
- 时间为带时区 ISO 8601 字符串，交易日 YYYY-MM-DD，北京时间；不要服务器本地时间隐式判断。
- direction: `buy_sell`（先买后卖）、`sell_buy`（先卖后买）。
- 每次修改带 expected_version（新建可省）；资金变动带 idempotency_key。
- reasons 为中文字符串数组，reason_codes 为稳定代码数组（可选）。不把人工登记标为券商已验证。

## 数据字段

Account: id, name, mode(manual|simulation), available_cash(number|null), broker_frozen_cash,
cash_net_of_frozen(bool), trade_date, snapshot_at, reconciled(bool), version, event_seq,
config（见下文）。可返回 effective_cash, reserved_cash, needs_reconciliation。

Position: account_id, symbol, name, exchange(SH|SZ|BJ), board(main|chinext|star|bse),
security_type(stock), qty, sellable_qty(number|null), today_bought, today_sold,
broker_frozen_qty, sellable_net_of_frozen(bool), external_sell_qty, core_qty, target_qty,
max_t_qty, max_extra_qty, cost_price(number|null), cost_method, enabled,
trade_date, snapshot_at, reconciled, version, benchmark_symbol, sector_id。
返回 effective_sellable, reserved_qty。snapshot 人工覆盖须明确 included_event_seq；存在未决委托/活动计划时拒绝盲目覆盖，需走核对。
批次同步创建的持仓还返回只读 `source="lots"`、`source_synced_at` 和 `source_lot_ids`，不通过持仓表单伪造来源。

Config: min_net_profit=20, min_net_return=0.002, max_daily_plans=6,
max_symbol_daily_plans=2, daily_quantity_ratio=0.4, cooldown_minutes=15,
single_risk_budget=100, daily_risk_budget=500, max_extra_notional=10000,
quote_max_age_seconds=10, position_max_age_seconds=900,
new_plan_cutoff="14:00", max_leg_minutes=45, commission_rate=0.0003,
min_commission=5, stamp_tax_rate=0.0005, transfer_rate=0.00001,
slippage_per_share=0.01, fee_effective_from="2023-08-28", fees_confirmed=false,
enabled=false，minute_timestamp_semantics="unknown"（确认后 start/end）。完整字段以 strategy/position_t.py 的 DEFAULT_CONFIG 为准。设置是研发初值，非盈利承诺；未知字段拒绝。

Signal: id, account_id, symbol, direction, state(none|watching|triggered|blocked|paused|expired),
reasons, evidence(object), first_price([low,high]|null), second_price([low,high]|null),
stop_price, cover_guard, quantity, net_min, net_max, fees(object), created_at, expires_at,
strategy_version, account_version, position_version, market(object), source_mode。

Plan: id, account_id, symbol, direction, quantity, state, leg1_filled, leg2_filled,
matched_qty, unmatched_qty, exposure_qty, matched_pnl, reserved_cash, reserved_qty,
first_price, second_price, stop_price, cover_guard, created_at, updated_at, version,
reasons, legs(list), strategy_version。
计划显示状态推荐 first_pending/first_partial/second_pending/second_partial/completed/exposure/cancelled。

Execution: id, plan_id, leg(1|2), order_id, quantity, price, executed_at,
fee(number|null), idempotency_key, proceeds_available=false。人工卖出所得默认待核对；模拟账本可即时计入。费用未提供时按订单累计收费分组暂估，实际费用修正须留痕。

Order event: plan_id, leg, order_id, event_id, status(submitted|partially_filled|filled|cancel_pending|cancelled|rejected),
quantity, cumulative_filled, cancelled_quantity, price, occurred_at。
状态回报不自动产生实际成交；撤单最终累计与逐笔不一致时不释放未决资源。

## API /api/position-t

- GET /overview?account_id= -> {accounts, account|null, positions, signals, plans, health, config}
- POST /accounts {name,mode,available_cash,trade_date,...} -> {account}
- PUT /accounts/{id}/snapshot {available_cash,broker_frozen_cash,cash_net_of_frozen,trade_date,snapshot_at,reconciled,included_event_seq,expected_version} -> {account}
- PUT /accounts/{id}/positions/{symbol} Position + included_event_seq + expected_version -> {position}
- POST /accounts/{id}/sync-lots {expected_version} -> {created:[{symbol,name,qty}], skipped:[{symbol,reason}], account_version}；仅新增缺失股票，同股批次合并，已有持仓不覆盖。整批创建在一个事务中检查账户版本与证券唯一性；重复同步无新增时不增加事件水位。新持仓保持可卖未知、待核对、监控关闭。
- PUT /accounts/{id}/config Config + expected_version -> {account,config}
- POST /plans {signal_id,quantity,expected_account_version,expected_position_version,idempotency_key} -> {plan}
- POST /plans/{id}/stop-first-leg {expected_version} -> {plan}
- POST /order-events Order event -> {plan}
- POST /executions Execution -> {plan,execution}
- POST /executions/{id}/fee {fee,reason,idempotency_key} -> {plan,execution}（仅费用更正，留审计）
- GET /plans/{id} -> {plan,executions,events}
- GET /reviews?account_id=&date= -> {review}
- POST /replays {account_id,symbol,start_date,end_date,direction?} -> {replay} (保守历史模拟，数据不足返回明确错误)
- GET /capabilities -> {manual:true,simulation:true,broker:false,auto_order:false,...}

overview 单次批量读取；默认 5 秒刷新，账户/持仓切换不得保留上一上下文结果。
版本/资金冲突409，业务和缺数据422。接口薄层，行情和业务在 service 中。

计划详情同时返回 ledger_events 审计事实；events 为扁平委托事件。plan.monitor 返回第二笔监控说明与风险，不改变成交事实。

实现和运行边界见 [首版使用与接入说明](position-t-monitor.md)。

批次同步复用现有 `/lots` 扩展页面（L2）和做 T 服务／账本（局部 L3），不增加框架或后台同步线程。写入后前端使已有 `positionTAll` 查询失效；持仓由账本即时读取，不增加独立缓存，也不修改批次文件。以后合并上游时需复核新增接口与账本事务边界。

## 上线边界

候选只测算；接受计划才原子预留。每账户同股最多一个未完成计划。
人工可卖未知/过期和行情断流必须暂停，已有成交永不因信号过期删除。
强制先验证数据授权能力，不绕 provider，不静默构造实时价格/板块/时间戳。
保留实际未完成敞口、T+1可卖消耗、持续持有对照，不以收盘价假造第二笔成交。
