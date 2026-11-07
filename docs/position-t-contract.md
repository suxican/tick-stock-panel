# 持仓做 T 首版内部契约

实现基线：30794343。本文为本次新增能力的接口约定，不描述既有可用 API。
现有 `/api/lots` 和旧批次提醒保持兼容。支持 manual / simulation，模拟账户可显式开启自动模拟，不接券商、不自动提交真实委托。

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
enabled=false，auto_simulation_enabled=false，minute_timestamp_semantics="unknown"（确认后 start/end）。自动模拟开关仅对 simulation 账户有效，不代替 enabled；旧配置缺失按关闭处理。完整字段以 strategy/position_t.py 的 DEFAULT_CONFIG 为准。设置是研发初值，非盈利承诺；未知字段拒绝。

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

- GET /overview?account_id= -> {accounts, account|null, positions, signals, plans, health, config, simulation|null}；simulation 是当前选中模拟账户的 {state,reasons,last_run}，state 为 disabled/waiting/running/paused/idle。
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
- GET /daily-reviews?account_id=&date= -> {report}；按成交交易日汇总的持久盘后报告，盘中返回 preliminary，归档为 final。summary.realized_net 按第二笔当天 FIFO 匹配归属；summary.matched_fees 为这些配对的双边费用，summary.trade_fees 为当天发生交易费用。含 by_symbol/by_direction、unfinished、trades、plans、blockers、execution_blockers、suggestions、coverage、strategy_versions、config_evidence、valuation 与 limitations。unfinished.remaining_first_qty 表示仍可继续的首笔股数，即使暂时没有敞口或挂单也须列入。execution_blockers 按账户／分钟最后一次执行等待或暂停原因计数，coverage.execution_sample_count 给出样本数，不等同逐委托完整决策日志。无完整收盘证据的 valuation 字段为 null。
- GET /signal-history?account_id=&symbol=&date=&limit=500 -> {events}，limit 为 1–1000；按时间正序返回最近的触发／更新／失效证据，字段为 signal、previous_state、recorded_at、context（分钟、报价、环境及当时账户／持仓；重启失效可为 null）。
- POST /replays {account_id,symbol,start_date,end_date,direction?} -> {replay} (保守历史模拟，数据不足返回明确错误)
- GET /capabilities -> {manual:true,simulation:true,auto_simulation:true,broker:false,auto_order:false,...}；auto_order 仍表示真实券商自动下单能力。

overview 单次批量读取；默认 5 秒刷新，账户/持仓切换不得保留上一上下文结果。
版本/资金冲突409，业务和缺数据422。接口薄层，行情和业务在 service 中。

计划详情同时返回 ledger_events 审计事实；events 为扁平委托事件。plan.monitor 返回第二笔监控说明与风险，不改变成交事实。新增可选字段 state(waiting_first|waiting|ready|risk|paused)、actionable_price、net_profit、expires_at、plan_version、account_version、position_version；ready 仅在新鲜执行报价、实际剩余成本、资源及委托状态同时满足时成立。客户端必须按 expires_at 降级旧 ready；历史缺少字段时不能推断机会有效。

站内通知复用行情流 strategy_alert，source 为 position_t，kind 为 signal/second_leg_ready/plan_risk/execution；稳定 event_id 去重，account_id/plan_id/symbol 用于定位 /lots，机会事件带 expires_at。持久记录与广播分离，重试不重复记录；发送前重验，未送达的持续有效机会可更新待发快照，已送达事件不重复投递。

回放保留 max_adverse_excursion 的分钟收盘口径，新增 max_adverse_excursion_close、max_adverse_excursion_intraminute_bound 及 adverse_excursion_basis（close/intraminute_bound 中文说明）。新字段是兼容新增，不把分钟内风险上界标为真实亏损或胜率。

实现和运行边界见 [首版使用与接入说明](position-t-monitor.md)。

自动模拟计划附加 `execution_mode="automatic_simulation"`，模拟订单和成交附加 `source="simulation_auto"`；手工计划不接管。信号接受与成交模拟分离，订单仅使用提交之后的完整分钟保守撮合。模拟成交及订单水位原子落账，幂等重试不重复耗用资金／库存。停用自动执行会取消剩余模拟委托并保留已成交敞口；候选、行情不足和日历未知不得自动生成成交。API 不开放伪造 execution_mode/source 的写入口。

自动模拟与盘后复盘属于局部 L3：在现有做 T 服务内组合小模块，复用原账本和 Provider 数据，不新增后台线程或行情来源。新增 SQLite 派生报告及采样表按缺失创建，旧账户开关默认关闭；账户及计划沿用 positionT 查询失效范围，日报使用集中管理的账户／日期查询键，仅打开复盘页时每 30 秒更新临时统计，归档后停止轮询。合并上游需复核服务运行顺序、订单／成交原子事务、配置默认值、日切及按成交日归属的复盘契约。

批次同步复用现有 `/lots` 扩展页面（L2）和做 T 服务／账本（局部 L3），不增加框架或后台同步线程。写入后前端使已有 `positionTAll` 查询失效；持仓由账本即时读取，不增加独立缓存，也不修改批次文件。以后合并上游时需复核新增接口与账本事务边界。

## 上线边界

候选只测算；接受计划才原子预留。每账户同股最多一个未完成计划。
人工可卖未知/过期和行情断流必须暂停，已有成交永不因信号过期删除。
强制先验证数据授权能力，不绕 provider，不静默构造实时价格/板块/时间戳。
保留实际未完成敞口、T+1可卖消耗、持续持有对照，不以收盘价假造第二笔成交。
