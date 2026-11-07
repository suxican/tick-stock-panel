import type { PositionTDailyAggregate, PositionTDailyReview as DailyReport } from '@/lib/api'
import { directionLabel, Empty, formatTime, money, Notice } from './positionTShared'

const sourceLabels = { simulation_auto: '自动模拟', simulation_manual: '手工模拟', manual_record: '人工登记' }
const percent = (value: number | null) => value == null ? '无起始样本' : `${(value * 100).toFixed(1)}%`

export function PositionTDailyReview({ report }: { report: DailyReport }) {
  const { summary, coverage } = report
  const available = report.evidence_status === 'available'
  return <section aria-label="当日做 T 复盘" className="space-y-4">
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-secondary"><span className="font-medium text-foreground">{report.source_mode === 'simulation' ? '模拟账户 · 非真实交易' : '人工登记账户'}</span><span>{report.status === 'final' ? '已归档' : '预览 · 尚未归档'}</span><span>生成于 {formatTime(report.generated_at)}</span></div>
    {!available ? <Empty title="该日记录不足">没有可核验的交易或监控证据，暂不能判断策略表现。</Empty> : <>
      <dl className="grid grid-cols-2 gap-x-6 rounded-lg border border-border px-4 lg:grid-cols-4">{[
        ['当日配对净收益', money(summary.realized_net)],
        ['当日成交费用', money(summary.trade_fees)],
        ['当日起始完成率', percent(summary.completion_rate)],
        ['未完成／未决计划', `${report.unfinished.length} 个`],
      ].map(([label, value]) => <div key={label} className="py-3 text-xs"><dt className="text-secondary">{label}</dt><dd className="mt-1 font-mono text-base font-medium text-foreground">{value}</dd></div>)}</dl>
      <p className="text-xs leading-relaxed text-secondary">共 {summary.trade_count} 笔成交，买入 {summary.buy_qty} 股、卖出 {summary.sell_qty} 股。当日首次成交的 {summary.started_count} 个计划中完成 {summary.completed_count} 个；当日结束共 {summary.closed_today_count} 个（含此前起始）。配对净收益按第二笔成交日归属并扣两腿费用；当日成交费用仅计当天发生。</p>
      {(summary.realized_net == null || summary.trade_fees == null || coverage.estimated_fee_count > 0) && <Notice>{summary.realized_net == null || summary.trade_fees == null ? '存在未核对费用，相关收益或费用显示待核对。' : ''}{coverage.estimated_fee_count > 0 && `有 ${coverage.estimated_fee_count} 笔费用使用合同暂估，结果仍需核对。`}</Notice>}
      {report.unfinished.length > 0 && <div className="rounded-lg border border-border bg-surface p-4"><h3 className="text-sm font-medium text-foreground">报告时点待处理</h3><ul className="mt-2 space-y-2 text-xs leading-relaxed">{report.unfinished.map(item => <li key={item.plan_id}><span className="font-medium">{item.symbol} · {directionLabel(item.direction)}</span><span className="ml-2 text-secondary">相对计划基准{item.exposure_qty > 0 ? '多出' : item.exposure_qty < 0 ? '缺少' : '持平'} {Math.abs(item.exposure_qty)} 股{(item.remaining_first_qty ?? 0) > 0 && ` · 第一笔仍可继续 ${item.remaining_first_qty} 股`}{item.pending_order_qty > 0 && ` · 未决委托 ${item.pending_order_qty} 股`}</span></li>)}</ul><p className="mt-2 text-xs text-secondary">这是报告时点的持仓差异，未完成风险不包含在已实现收益中。</p></div>}
    </>}
    {report.suggestions.length > 0 && <div><h3 className="text-sm font-medium text-foreground">复盘结论</h3><ul className="mt-2 space-y-2 text-xs leading-relaxed text-secondary">{report.suggestions.map(item => <li key={item.code}><span className="font-medium text-foreground">{item.title}。</span>{item.detail}</li>)}</ul></div>}
    <p className="text-xs leading-relaxed text-secondary">收盘权益、持续持有对照及最大回撤：{report.valuation.reason}。暂不据此判断净增量收益。</p>
    {available && <details className="rounded-lg border border-border p-4"><summary className="cursor-pointer text-sm font-medium text-foreground">分股票、方向与阻断原因</summary><div className="mt-3 space-y-4">
      <AggregateTable title="分股票" rows={report.by_symbol} />
      <AggregateTable title="分方向" rows={report.by_direction.map(row => ({ ...row, key: directionLabel(row.key) }))} />
      <div><h4 className="text-xs font-medium text-foreground">阻断原因</h4>{report.blockers.length ? <ul className="mt-2 space-y-1 text-xs text-secondary">{report.blockers.map(item => <li key={item.reason}>{item.reason} · {item.count} 次分钟样本</li>)}</ul> : <p className="mt-2 text-xs text-secondary">未记录阻断原因{coverage.sample_count === 0 ? '，当日监控样本不足' : ''}。</p>}</div>
      {!!report.execution_blockers?.length && <div><h4 className="text-xs font-medium text-foreground">自动执行等待与暂停</h4><ul className="mt-2 space-y-1 text-xs text-secondary">{report.execution_blockers.map(item => <li key={item.reason}>{item.reason} · {item.count} 次账户分钟样本</li>)}</ul></div>}
    </div></details>}
    <details className="rounded-lg border border-border p-4"><summary className="cursor-pointer text-sm font-medium text-foreground">成交、样本与计算口径{report.trades.length > 0 && `（${report.trades.length} 笔）`}</summary><div className="mt-3 space-y-3">
      {report.trades.length > 0 && <div className="overflow-x-auto"><table className="w-full min-w-[600px] text-left text-xs"><thead className="text-secondary"><tr>{['北京时间', '股票 / 步骤', '来源', '股数 / 价格', '当笔费用'].map(label => <th key={label} className="pb-2 font-normal">{label}</th>)}</tr></thead><tbody>{report.trades.map(trade => <tr key={trade.id} className="border-t border-border"><td className="py-2">{formatTime(trade.executed_at)}</td><td>{trade.symbol}<span className="block text-secondary">第 {trade.leg} 笔 · {trade.side === 'buy' ? '买入' : '卖出'}</span></td><td>{sourceLabels[trade.source]}</td><td className="font-mono">{trade.quantity} 股 / {trade.price.toFixed(2)}</td><td className="font-mono">{money(trade.fee)}{trade.fee_estimated && <span className="block font-sans text-secondary">合同暂估</span>}</td></tr>)}</tbody></table></div>}
      <p className="text-xs leading-relaxed text-secondary">监控样本 {coverage.sample_count} 条 · 自动执行状态样本 {coverage.execution_sample_count ?? 0} 条 · 已记录机会 {coverage.triggered_episode_count} 个 · 未关联计划 {coverage.unaccepted_episode_count} 个。机会未关联计划不代表错失盈利。{coverage.first_sample_at && `覆盖 ${formatTime(coverage.first_sample_at)} 至 ${formatTime(coverage.last_sample_at)}。`}</p>
      <p className="text-xs text-secondary">配对毛价差 {money(summary.realized_gross)} · 配对费用 {money(summary.matched_fees)} · 盈利 / 亏损 / 持平 / 待核对计划 {summary.win_count} / {summary.loss_count} / {summary.flat_count} / {summary.unknown_pnl_count}</p>
      <p className="text-xs text-secondary">策略 {report.strategy_versions.join('、') || '无记录'} · 配置快照 {report.config_evidence.length} 份</p>
      <ul className="space-y-1 text-xs leading-relaxed text-secondary">{report.limitations.map((text, index) => <li key={index}>{text}</li>)}</ul>
    </div></details>
  </section>
}

function AggregateTable({ title, rows }: { title: string; rows: (PositionTDailyAggregate & { key: string })[] }) {
  if (!rows.length) return null
  return <div><h4 className="mb-2 text-xs font-medium text-foreground">{title}</h4><div className="overflow-x-auto"><table className="w-full min-w-[480px] text-left text-xs"><thead className="text-secondary"><tr>{['范围', '配对净收益', '配对股数', '当日起始完成率'].map(label => <th key={label} className="pb-2 font-normal">{label}</th>)}</tr></thead><tbody>{rows.map(row => <tr key={row.key} className="border-t border-border"><td className="py-2">{row.key}</td><td className="font-mono">{money(row.realized_net)}</td><td className="font-mono">{row.matched_quantity}</td><td className="font-mono">{percent(row.completion_rate)} · {row.completed_count}/{row.started_count}</td></tr>)}</tbody></table></div></div>
}
