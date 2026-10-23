import { useEffect, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, type FirstBoardConfig, type FirstBoardExecutionOptions, type FirstBoardPaperValidation, type FirstBoardReplayValidation, type FirstBoardTradeValidation, type FirstBoardTradeValidationRequest } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { beijingDay, buttonClass, ErrorNotice, inputClass, LoadingRows, money, Notice, panelClass, pct, stamp } from './shared'

const modeLabels = { replay: '已记录信号成交回放（模型估计）', paper: '模拟盘成交归因' }
const defaults: FirstBoardExecutionOptions = { amount: 10000, commission_pct: .0003, stamp_tax_pct: .0005, slippage_bps: 5, participation_rate: .01, entry_window_minutes: 5, max_exit_sessions: 5 }
const optionFields: { key: keyof FirstBoardExecutionOptions; label: string; scale: number; min: number; max: number; step: number }[] = [
  { key: 'amount', label: '每笔金额（元）', scale: 1, min: 1, max: 100000000, step: 100 },
  { key: 'commission_pct', label: '双边佣金（%）', scale: 100, min: 0, max: 1, step: .01 },
  { key: 'stamp_tax_pct', label: '卖出印花税（%）', scale: 100, min: 0, max: 5, step: .01 },
  { key: 'slippage_bps', label: '单边滑点（基点）', scale: 1, min: 0, max: 200, step: 1 },
  { key: 'participation_rate', label: '成交量参与率（%）', scale: 100, min: .01, max: 10, step: .1 },
  { key: 'entry_window_minutes', label: '入场窗口（分钟）', scale: 1, min: 3, max: 30, step: 1 },
  { key: 'max_exit_sessions', label: '退出观察上限（交易日）', scale: 1, min: 1, max: 10, step: 1 },
]
const tradeLabels: Record<string, string> = { no_fill: '未成交', unverifiable: '无法验证', open_position: '持有中', closed: '已闭环', not_ordered: '未发现委托', pending: '委托待成交', filled: '已成交', cancelled: '已撤单', expired: '委托已过期', open: '持有中', partially_closed: '部分退出', evidence_missing: '证据缺失' }
const groupLabels: Record<string, string> = { S0F0: '两项未满足', S1F0: '仅强势板块', S0F1: '仅领涨带动', S1F1: '同板块双条件', unknown: '未知组' }
const resultLabels: Record<string, string> = { modelled: '已完成模型估计', insufficient_evidence: '证据不足', no_signals: '无已记录信号', ready: '归因完成', incomplete: '部分证据不完整', unavailable: '当前不可用', error: '验证失败' }
const reasonLabels: Record<string, string> = {
  recorded_signal_config_missing: '缺少信号当时的完整规则', recorded_limit_evidence_mismatch: '涨停价与已记录信号不一致',
  minute_window_invalid: '分钟窗口无法验证', minute_prices_outside_daily_limits: '分钟价格超出当日涨跌停范围',
  entry_limit_blocked: '触及涨停，模型未能买入', entry_amount_below_lot: '金额不足最低买入数量', entry_volume_insufficient: '入场分钟成交量不足',
  entry_window_missing: '入场分钟窗口缺失', entry_window_expired: '入场窗口内未成交', entry_session_missing: '信号日分钟记录缺失',
  corporate_action_unresolved: '公司行动影响尚未核实', minute_values_invalid: '分钟价格或成交数据无效',
  exit_limit_blocked: '触及跌停，模型未能退出', exit_volume_insufficient: '退出分钟成交量不足', exit_window_missing: '退出分钟窗口缺失',
  exit_window_exhausted: '已达退出观察上限，仍未平仓', exit_observation_incomplete: '退出观察期尚未完整覆盖',
  calendar_unavailable: '交易日历不可用', signal_day_not_in_calendar: '信号日期不在交易日历中',
  minute_data_missing: '缺少分钟行情', minute_time_invalid: '分钟时间格式无效', minute_basis_unknown: '分钟原价口径未确认',
  daily_evidence_missing: '缺少日线证据', daily_price_basis_or_limits_unknown: '日线价格口径或涨跌停价未确认', daily_limits_invalid: '当日涨跌停价无效',
}
function reasonText(reason: string) {
  const separator = reason.indexOf(':')
  const code = separator < 0 ? reason : reason.slice(0, separator)
  return reasonLabels[code] ? `${reasonLabels[code]}${separator < 0 ? '' : `：${reason.slice(separator + 1).trim()}`}` : reason
}

function validOptions(options: FirstBoardExecutionOptions) {
  return Object.values(options).every(Number.isFinite) && options.amount > 0 && options.amount <= 100000000
    && options.commission_pct >= 0 && options.commission_pct <= .01 && options.stamp_tax_pct >= 0 && options.stamp_tax_pct <= .05
    && options.slippage_bps >= 0 && options.slippage_bps <= 200 && options.participation_rate > 0 && options.participation_rate <= .1
    && Number.isInteger(options.entry_window_minutes) && options.entry_window_minutes >= 3 && options.entry_window_minutes <= 30
    && Number.isInteger(options.max_exit_sessions) && options.max_exit_sessions >= 1 && options.max_exit_sessions <= 10
}

function OptionsReadout({ options }: { options: FirstBoardExecutionOptions }) {
  return <dl className="grid grid-cols-1 gap-2 text-xs sm:grid-cols-2 lg:grid-cols-3">{optionFields.map(field => <div key={field.key} className="flex flex-wrap gap-2"><dt className="text-secondary">{field.label}</dt><dd className="tabular-nums">{Number((options[field.key] * field.scale).toFixed(6))}</dd></div>)}</dl>
}

function ReplayTrace({ trade }: { trade: FirstBoardReplayValidation['trades'][number] }) {
  const stages = [['记录信号', trade.signal_at], ['模型委托', trade.order_at], ['模型入场成交', trade.entry_at], ['退出信号', trade.exit_signal_at], ['模型退出委托', trade.exit_order_at], ['模型退出成交', trade.exit_at]]
  return <div className="space-y-3 py-2 text-xs">
    <p className="break-all text-secondary">信号 ID：{trade.signal_id} · 版本 {trade.revision} · {groupLabels[trade.group] ?? trade.group}</p>
    <ol className="space-y-1">{stages.map(([label, time]) => <li key={label}>{label}：{stamp(time)}</li>)}</ol>
    <p className="text-secondary">分钟标签：入场 {stamp(trade.entry_bar_at)} · 退出信号 {stamp(trade.exit_signal_bar_at)} · 退出成交 {stamp(trade.exit_bar_at)}。以上成交时刻另加保守可用延迟。</p>
    <p>数量 {trade.qty ?? '—'} 股 · 入场原价 {money(trade.entry_raw_price)} · 成交价 {money(trade.entry_price)} · 退出原价 {money(trade.exit_raw_price)} · 成交价 {money(trade.exit_price)}</p>
    <p>买入费用 {money(trade.buy_fee)} 元 · 卖出费用 {money(trade.sell_fee)} 元 · 入场总成本 {money(trade.entry_cost)} 元 · 退出净额 {money(trade.exit_proceeds)} 元</p>
  </div>
}

function recordText(value: unknown) { return value == null ? '—' : String(value) }
function recordMoney(value: unknown) { return typeof value === 'number' ? money(value) : '—' }
function recordTime(record: Record<string, unknown>, key: string, fallback?: string) {
  const value = record[key] ?? (fallback ? record[fallback] : null)
  return typeof value === 'string' || typeof value === 'number' ? stamp(value) : '—'
}
function PaperTrace({ trade }: { trade: FirstBoardPaperValidation['trades'][number] }) {
  return <div className="space-y-3 py-2 text-xs">
    <p className="break-all text-secondary">信号 ID：{trade.signal_id} · 版本 {trade.revision}</p>
    <p>信号：{stamp(trade.signal_at)}</p>
    {trade.status === 'partially_closed' && <p>已退出部分的已实现净收益：{money(trade.realized_net_pnl)} 元；剩余 {trade.remaining_qty ?? '—'} 股，尚不计入闭环收益率。</p>}
    <div><h4 className="font-medium">关联委托（{trade.orders.length}）</h4>{trade.orders.length ? <ul className="space-y-2">{trade.orders.map((order, index) => <li key={index} className="break-all">{recordText(order.id)} · {order.side === 'sell' ? '卖出' : '买入'} {recordText(order.qty)} 股 · {tradeLabels[String(order.status)] ?? recordText(order.status)} · {recordTime(order, 'created_at')}{order.reason ? ` · ${order.reason}` : ''}</li>)}</ul> : <p className="text-secondary">未发现可关联的委托记录。</p>}</div>
    <div><h4 className="font-medium">入场成交（{trade.fills.length}）</h4>{trade.fills.length ? <ul className="space-y-2">{trade.fills.map((fill, index) => <li key={index} className="break-all">委托 {recordText(fill.order_id)} · {recordTime(fill, 'ts', 'date')} · {recordText(fill.qty)} 股 × {recordMoney(fill.price)} 元 · 费用 {recordMoney(fill.fee)} 元{fill.fee_defaulted ? '（历史费用缺失，相关收益已排除）' : ''}</li>)}</ul> : <p className="text-secondary">无可核验的入场成交。</p>}</div>
    <div><h4 className="font-medium">FIFO 退出配对（{trade.exits.length}）</h4>{trade.exits.length ? <ul className="space-y-2">{trade.exits.map((exit, index) => <li key={index} className="break-all">委托 {recordText(exit.sell_order_id)} · {exit.manual_exit ? '人工退出' : '关联退出'} · {recordTime(exit, 'ts', 'date')} · {recordText(exit.qty)} 股 × {recordMoney(exit.price)} 元 · 分摊买入成本 {recordMoney(exit.allocated_buy_cost)} 元 · 分摊卖出费用 {recordMoney(exit.allocated_sell_fee)} 元</li>)}</ul> : <p className="text-secondary">无可核验的退出成交。</p>}</div>
  </div>
}

function ValidationResult({ result, onOpenStock }: { result: FirstBoardTradeValidation; onOpenStock: (symbol: string, name: string) => void }) {
  const replay = result.kind === 'replay' ? result : null
  const paper = result.kind === 'paper' ? result : null
  const signals = replay ? replay.summary.signals : paper!.coverage.signal_count
  const closed = replay ? replay.summary.closed : paper!.summary.closed_signals
  const netPnl = replay ? replay.summary.net_pnl_sum : paper!.summary.closed_net_pnl
  const netReturn = replay ? replay.summary.net_return_mean : paper!.summary.closed_mean_net_return
  const winRate = replay ? replay.summary.win_rate : paper!.summary.closed_win_rate
  return <section className={`${panelClass} min-w-0 space-y-4`} aria-label="交易验证结果">
    <div className="flex flex-wrap items-start justify-between gap-2"><h3 className="text-sm font-semibold">{modeLabels[result.kind]}</h3><span className="text-xs text-secondary">{resultLabels[result.status] ?? result.status} · {stamp(result.created_at)}</span></div>
    <p className="text-xs leading-relaxed text-secondary">信号区间 {result.request.start} 至 {result.request.end} · 规则版本 {result.request.revision} · 结果截止 {result.request.through}{paper ? ` · 账户 ${paper.account_id ?? '未关联'}` : ''}</p>
    {paper?.reason && <Notice error={paper.status === 'error'}>{paper.reason}</Notice>}
    <div className="flex flex-wrap gap-x-6 gap-y-2 text-sm" aria-label="交易验证分母">
      <strong>闭环 {closed} / {signals}</strong>
      {replay && <><span>未成交 {replay.summary.no_fill}</span><span>无法验证 {replay.summary.unverifiable}</span><span>持有中 {replay.summary.open_position}</span></>}
      {paper && <><span>已委托 {paper.summary.submitted_signals}</span><span>入场成交 {paper.summary.filled_signals}</span><span>未成交 {paper.summary.unfilled_signals}</span><span>未发现委托 {paper.summary.unsubmitted_signals}</span><span>持有中 {paper.summary.open_signals}</span><span>部分退出 {paper.summary.partially_closed_signals}</span><span>无法归属 {paper.summary.unknown_signals}</span></>}
    </div>
    <div className="grid gap-3 rounded-btn bg-elevated p-3 text-sm sm:grid-cols-2"><span>闭环净收益合计：{money(closed ? netPnl : null)} 元</span><span>闭环平均净收益率：{pct(closed ? netReturn : null)}</span><span>闭环盈利占比：{pct(closed ? winRate : null)}（分母 {closed}）</span></div>
    {paper && <p className="text-sm">已实现净收益（含部分退出）：{money(paper.summary.realized_net_pnl)} 元。该金额包含已退出的部分，闭环盈利占比分母仍为 {closed}。</p>}
    <p className="text-xs leading-relaxed text-secondary">以上净收益仅统计已闭环且可核验的信号。未成交、持有中、部分退出与无法验证不计作零收益。不是独立组合收益或盈利证明。</p>
    {replay && <div className="space-y-2 text-xs text-secondary"><p>前向样本：{replay.coverage.sample_count} 个锚点 · 股票日 {replay.coverage.stock_days} · 可提示股票日 {replay.coverage.eligible_stock_days} · 负例股票日 {replay.coverage.negative_stock_days} · 未知股票日 {replay.coverage.unknown_stock_days}</p><p>分钟读取 {replay.coverage.minute_days_read} 日 · 交易日历{replay.coverage.calendar_available ? '可用' : '不可用'}。前向采样用于保留当时证据，不等于成交笔数。</p></div>}
    {paper && <div className="space-y-2 text-xs text-secondary"><p>账本委托 {paper.coverage.ledger_order_count} 条 · 成交 {paper.coverage.ledger_fill_count} 条 · 可归属 {paper.coverage.attributable_count} · 无法归属 {paper.coverage.unknown_count}</p><p>历史费用缺失 {paper.coverage.legacy_fee_default_count} 条；相关收益已排除。未发现委托不代表未尝试下单；人工选择和退出会影响结果。</p></div>}
    {signals === 0 && <Notice>所选范围没有可验证的已记录信号。缺少记录不能解释为策略没有机会。</Notice>}
    {replay && Object.keys(replay.by_group).length > 0 && <details><summary className="cursor-pointer text-sm">前向分组与闭环分母</summary><div className="mt-2 overflow-x-auto"><table className="w-full min-w-[650px] text-left text-xs" aria-label="交易验证分组"><thead><tr>{['当时分组', '信号', '闭环', '未成交', '无法验证', '持有中', '闭环平均净收益率'].map(label => <th className="px-2 py-2 font-normal text-secondary" key={label}>{label}</th>)}</tr></thead><tbody>{Object.entries(replay.by_group).map(([group, value]) => <tr key={group} className="border-t border-border"><th className="px-2 py-2 font-medium">{groupLabels[group] ?? group}</th><td className="px-2 py-2">{value.signals}</td><td className="px-2 py-2">{value.closed}</td><td className="px-2 py-2">{value.no_fill}</td><td className="px-2 py-2">{value.unverifiable}</td><td className="px-2 py-2">{value.open_position}</td><td className="px-2 py-2">{pct(value.closed ? value.net_return_mean : null)}</td></tr>)}</tbody></table></div></details>}
    {result.trades.length > 0 && <div className="overflow-x-auto"><table className="w-full min-w-[850px] text-left text-xs" aria-label="交易验证明细"><thead><tr>{['股票 / 信号时间', '执行状态', '闭环净收益', '闭环净收益率', '剩余数量', '原因与交易链'].map(label => <th key={label} className="px-2 py-2 font-normal text-secondary">{label}</th>)}</tr></thead><tbody>
      {replay?.trades.map(trade => <tr className="border-t border-border" key={trade.signal_id}><td className="px-2 py-3 align-top"><button className="font-medium hover:underline" onClick={() => onOpenStock(trade.symbol, trade.name || trade.symbol)}>{trade.name || trade.symbol}</button><span className="mt-1 block text-secondary">{trade.symbol}<br />{stamp(trade.signal_at)}</span></td><td className="px-2 py-3 align-top">{tradeLabels[trade.status]}</td><td className="px-2 py-3 align-top tabular-nums">{money(trade.status === 'closed' ? trade.net_pnl : null)}</td><td className="px-2 py-3 align-top tabular-nums">{pct(trade.status === 'closed' ? trade.net_return : null)}</td><td className="px-2 py-3 align-top">{trade.status === 'open_position' ? trade.qty ?? '—' : trade.status === 'closed' || trade.status === 'no_fill' ? 0 : '—'}</td><td className="min-w-64 max-w-xl px-2 py-3 align-top"><p className="leading-relaxed">{trade.reasons.map(reasonText).join('；') || '—'}</p><details className="mt-2"><summary className="cursor-pointer">信号 → 委托 → 成交 → 退出</summary><ReplayTrace trade={trade} /></details></td></tr>)}
      {paper?.trades.map(trade => <tr className="border-t border-border" key={trade.signal_id}><td className="px-2 py-3 align-top"><button className="font-medium hover:underline" onClick={() => onOpenStock(trade.symbol, trade.symbol)}>{trade.symbol}</button><span className="mt-1 block text-secondary">{stamp(trade.signal_at)}</span></td><td className="px-2 py-3 align-top">{tradeLabels[trade.status]}</td><td className="px-2 py-3 align-top tabular-nums">{money(trade.status === 'closed' ? trade.realized_net_pnl : null)}</td><td className="px-2 py-3 align-top tabular-nums">{pct(trade.status === 'closed' ? trade.closed_net_return : null)}</td><td className="px-2 py-3 align-top">{trade.remaining_qty ?? '—'}</td><td className="min-w-64 max-w-xl px-2 py-3 align-top"><p className="leading-relaxed">{trade.reason || '—'}</p><details className="mt-2"><summary className="cursor-pointer">信号 → 委托 → 成交 → 退出</summary><PaperTrace trade={trade} /></details></td></tr>)}
    </tbody></table></div>}
    {(replay?.trades_truncated || paper?.coverage.truncated) && <p className="text-xs text-secondary">明细展示 {result.trades.length} / {replay?.total_trades ?? paper?.coverage.signal_count}，汇总仍基于全部信号。</p>}
    <details className="text-xs"><summary className="cursor-pointer py-2">费用参数与验证边界</summary><div className="space-y-3 pt-2">{replay ? <><p>{replay.model.label}：使用信号之后完整分钟的价格代理，并限制成交量参与率。成交价已包含滑点，费用另计；不等于真实队列成交。</p><OptionsReadout options={replay.model.options} /></> : <p>采用模拟盘实际成交价和记录费用，成交价已包含滑点，不按当前账户设置重算历史费用。FIFO 先重放完整账户，再归属首板批次。</p>}<ul className="ml-4 list-disc space-y-2 text-secondary">{result.limitations.map((item, index) => <li key={index}>{item}</li>)}</ul><p className="break-all text-secondary">报告 ID：{result.id} · 证据指纹：{result.fingerprint ?? '不可用'}</p></div></details>
  </section>
}

export function TradeValidationPanel({ config, versions, onOpenStock }: { config: FirstBoardConfig; versions: FirstBoardConfig[]; onOpenStock: (symbol: string, name: string) => void }) {
  const client = useQueryClient()
  const [mode, setMode] = useState<'replay' | 'paper'>('replay')
  const [start, setStart] = useState(() => beijingDay(-30))
  const [end, setEnd] = useState(() => beijingDay(-1))
  const [revision, setRevision] = useState('current')
  const [options, setOptions] = useState<FirstBoardExecutionOptions>({ ...defaults })
  const [result, setResult] = useState<FirstBoardTradeValidation | null>(null)
  const version = revision === 'current' ? config : versions.find(item => String(item.revision) === revision)
  const request: FirstBoardTradeValidationRequest = { mode, start, end, revision: version?.revision, ...(mode === 'replay' ? { options } : version?.paper_account_id ? { account_id: version.paper_account_id } : {}) }
  const currentRequest = useRef('')
  currentRequest.current = JSON.stringify(request)
  const run = useMutation({ mutationFn: api.firstBoardTradeValidation, onSuccess: (value, submitted) => {
    if (JSON.stringify(submitted) === currentRequest.current) setResult(value)
    void client.invalidateQueries({ queryKey: QK.firstBoardTradeValidationRuns })
  } })
  const reset = run.reset
  useEffect(() => { setResult(null); reset() }, [version?.revision, version?.paper_account_id, reset])
  const runs = useQuery({ queryKey: QK.firstBoardTradeValidationRuns, queryFn: api.firstBoardTradeValidationRuns })
  const validDates = !!start && !!end && start <= end && end <= beijingDay(mode === 'replay' ? -1 : 0) && (Date.parse(end) - Date.parse(start)) / 86400000 < 92
  const optionsValid = mode === 'paper' || validOptions(options)
  function clear() { setResult(null); run.reset() }
  return <div className="min-w-0 space-y-4">
    <Notice>日线形态观察、前向记录和交易净收益是不同证据。这里使用已记录信号，保留无法验证与未成交的分母；不会用当前规则重建历史买点。</Notice>
    <section className={`${panelClass} min-w-0 space-y-4`}>
      <h2 className="text-sm font-semibold">完整交易链验证</h2>
      <div className="flex flex-wrap gap-2" aria-label="交易验证方式">{(['replay', 'paper'] as const).map(value => <button key={value} className={`${buttonClass} aria-pressed:border-accent aria-pressed:bg-accent/10 aria-pressed:text-accent`} aria-pressed={mode === value} disabled={run.isPending} onClick={() => { setMode(value); clear() }}>{modeLabels[value]}</button>)}</div>
      <p className="text-xs leading-relaxed text-secondary">{mode === 'replay' ? '回放前向采样时已确认的信号，以保守分钟模型估计成交。每笔独立，不是资金组合回测。' : '读取所选版本关联模拟账户的委托与成交，按 FIFO 归属首板批次。人工选择和操作会产生选择偏差。'}</p>
      <div className="flex flex-wrap items-end gap-3">
        <label className="min-w-40 flex-1 space-y-1.5 text-xs text-secondary"><span>开始日期</span><input aria-label="交易验证开始日期" type="date" className={inputClass} max={end} value={start} disabled={run.isPending} onChange={event => { setStart(event.target.value); clear() }} /></label>
        <label className="min-w-40 flex-1 space-y-1.5 text-xs text-secondary"><span>结束日期</span><input aria-label="交易验证结束日期" type="date" className={inputClass} min={start} max={beijingDay(mode === 'replay' ? -1 : 0)} value={end} disabled={run.isPending} onChange={event => { setEnd(event.target.value); clear() }} /></label>
        <label className="min-w-40 flex-1 space-y-1.5 text-xs text-secondary"><span>已记录规则版本</span><select aria-label="交易验证规则版本" className={inputClass} value={revision} disabled={run.isPending} onChange={event => { setRevision(event.target.value); clear() }}><option value="current">当前版本 {config.revision}</option>{versions.filter(item => item.revision !== config.revision).map(item => <option key={item.revision} value={item.revision}>历史版本 {item.revision}</option>)}</select></label>
        <button className={buttonClass} disabled={run.isPending || !validDates || !optionsValid || !version} onClick={() => { setResult(null); run.mutate(request) }}>{run.isPending ? '正在验证…' : '运行交易验证'}</button>
      </div>
      <p className="text-xs text-secondary">每次最多 92 个自然日。回放仅选已完成日期；模拟盘归因可截止今天。</p>
      {!validDates && <Notice error>请选择有效日期区间，跨度最多 92 天；回放结束日期须早于北京时间今天。</Notice>}
      {mode === 'paper' && (version?.paper_account_id ? <p className="break-all text-xs text-secondary">所选规则版本关联账户：{version.paper_account_id}</p> : <Notice>所选规则版本未关联模拟账户，归因结果将明确显示不可用。可切换到已关联账户的规则版本。</Notice>)}
      {mode === 'replay' && <details><summary className="cursor-pointer text-sm">回放费用与成交约束</summary><div className="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">{optionFields.map(field => <label key={field.key} className="space-y-1.5 text-xs text-secondary"><span>{field.label}</span><input type="number" aria-label={field.label} className={inputClass} value={Number.isFinite(options[field.key]) ? Number((options[field.key] * field.scale).toFixed(6)) : ''} min={field.min} max={field.max} step={field.step} disabled={run.isPending} onChange={event => { setOptions({ ...options, [field.key]: event.target.value === '' ? NaN : Number(event.target.value) / field.scale }); clear() }} /></label>)}</div><p className="mt-3 text-xs leading-relaxed text-secondary">1 基点 = 0.01%。成交量参与率最多 10%，入场窗口 3–30 分钟，退出观察上限 1–10 个交易日。涨跌停与分钟缺口可能导致未成交或无法验证。</p></details>}
      {!optionsValid && <Notice error>请填写有效的费用和成交约束，范围见各输入框及说明。</Notice>}
    </section>
    <ErrorNotice error={run.error} />
    {run.isPending && <LoadingRows />}
    {result && <ValidationResult result={result} onOpenStock={onOpenStock} />}
    <section className={panelClass}><h2 className="mb-3 text-sm font-semibold">已保存的交易验证</h2><ErrorNotice error={runs.error} retry={() => void runs.refetch()} />{runs.isLoading ? <LoadingRows /> : runs.data?.runs.length ? <ul className="divide-y divide-border">{runs.data.runs.map(saved => <li className="flex flex-wrap items-center justify-between gap-3 py-3 text-xs" key={saved.id}><span>{modeLabels[saved.kind]} · {stamp(saved.created_at)} · 版本 {saved.result.request.revision}</span><button className={buttonClass} disabled={run.isPending} onClick={() => { run.reset(); setResult(saved.result) }}>查看交易验证结果</button></li>)}</ul> : <p className="text-sm text-secondary">验证结果自动保存，显示最近 10 次。查看历史记录不会重新运行。</p>}</section>
  </div>
}
