import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api, type HuichunResearchObservation, type HuichunResearchRecord } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { buttonClass, ErrorNotice, inputClass, LoadingRows, Pagination, panelClass, pct, price, stamp } from './shared'

type Row = HuichunResearchRecord
const boards = { main: '沪深主板', gem: '创业板', star: '科创板' }
const cohortLabels = { confirmed: 'A0 确认', zero_control: '零轴对照' }
const statuses: Record<string, string> = {
  ok: '已计算', pending: '未到期', no_calendar: '交易日历缺失', unknown_gap: '行情缺失',
  invalid_factor: '复权异常', baseline_changed: '基准已变化', unknown_rules: '历史交易规则待核验',
  entry_blocked: '开盘无法买入', exit_blocked: '开盘无法卖出', unverified_action: '除权结算待核验',
  insufficient_budget: '模拟资金不足',
  unavailable: '基准样本不足', refresh_required: '待刷新',
}
const statusLabel = (status: string) => statuses[status] ?? '待核验'
const finite = (value: number | null): value is number => value != null && Number.isFinite(value)
function mean(values: number[]) {
  return values.length ? values.reduce((sum, value) => sum + value, 0) / values.length : null
}
type Horizon = HuichunResearchObservation['horizon']
function observation(row: Row, horizon: Horizon) {
  return row.observations?.find(item => item.horizon === horizon)
}
function shape(row: Row, horizon: Horizon) {
  return observation(row, horizon) ?? (horizon === 5 ? row.shape_t5 : { status: 'refresh_required', return_pct: null })
}
function excessOutcome(row: Row, horizon: Horizon) {
  const item = observation(row, horizon)
  if (!item) return '待刷新'
  if (item.status !== 'ok') return statusLabel(item.status)
  return item.excess_return_pct == null ? statusLabel(item.benchmark_status) : pointDifference(item.excess_return_pct)
}
function cohortStats(records: Row[], horizon: Horizon) {
  return (['confirmed', 'zero_control'] as const).map(key => {
    const rows = records.filter(row => row.cohort === key)
    const values = (pick: (row: Row) => number | null) => {
      const valid = rows.map(pick).filter(finite)
      return { value: mean(valid), count: valid.length }
    }
    return { key, count: rows.length, metrics: [values(r => shape(r, horizon).return_pct),
      values(r => observation(r, horizon)?.excess_return_pct ?? null),
      values(r => r.execution.gross_return_pct), values(r => r.execution.net_return_pct)] }
  })
}
// Match using only signal-day information, before looking at either fill outcome.
function nearestVolatility(row: Row, controls: Row[]) {
  const vol = row.volatility_20
  if (vol == null || !Number.isFinite(vol) || vol < 0) return undefined
  let closest: Row | undefined
  let minimumRatio = 1.2
  for (const control of controls) {
    const other = control.volatility_20
    if (other == null || !Number.isFinite(other) || other < 0) continue
    const ratio = vol === other ? 1 : Math.max(vol, other) / Math.min(vol, other)
    if (ratio > minimumRatio || (closest && ratio === minimumRatio && closest.symbol <= control.symbol)) continue
    closest = control
    minimumRatio = ratio
  }
  return closest
}
type Metric = 'net' | 'gross' | 'shape'
function pairedComparison(records: Row[], metric: Metric, horizon: Horizon) {
  const getValue = (row: Row) => metric === 'net' ? row.execution.net_return_pct
    : metric === 'gross' ? row.execution.gross_return_pct : shape(row, horizon).return_pct
  const days = new Map<string, Record<Row['cohort'], Row[]>>()
  for (const row of records) {
    const groups = days.get(row.signal_date) ?? { confirmed: [], zero_control: [] }
    groups[row.cohort].push(row)
    days.set(row.signal_date, groups)
  }
  const differences: number[] = []
  const matchedDays: number[] = []
  let matchedCount = 0, peakCandidates = 0
  for (const groups of days.values()) {
    peakCandidates = Math.max(peakCandidates, groups.confirmed.length)
    const confirmed = mean(groups.confirmed.map(getValue).filter(finite))
    const control = mean(groups.zero_control.map(getValue).filter(finite))
    if (confirmed != null && control != null) differences.push(confirmed - control)
    const pairs: number[] = []
    for (const row of groups.confirmed) {
      const matched = nearestVolatility(row, groups.zero_control)
      const value = getValue(row), matchedValue = matched ? getValue(matched) : null
      if (finite(value) && finite(matchedValue)) pairs.push(value - matchedValue)
    }
    const matched = mean(pairs)
    if (matched != null) matchedDays.push(matched)
    matchedCount += pairs.length
  }
  return { count: differences.length, diff: mean(differences), matchedDiff: mean(matchedDays),
    matchedCount, matchedDays: matchedDays.length, peakCandidates }
}
function pointDifference(value: number | null) {
  return value == null ? '—' : `${value > 0 ? '+' : ''}${(value * 100).toFixed(2)} 个百分点`
}
function outcome(value: number | null, status: string) {
  return finite(value) ? pct(value, true) : status === 'ok' ? '—' : statusLabel(status)
}

export function ResearchPanel({ currentRevision, busy, onRefresh, refreshError }: {
  currentRevision?: number; busy: boolean; onRefresh: () => void; refreshError: Error | null
}) {
  const research = useQuery({ queryKey: QK.huichunResearch, queryFn: api.huichunResearch, staleTime: 10_000 })
  const [origin, setOrigin] = useState<Row['origin']>('forward')
  const [metric, setMetric] = useState<Metric>('net')
  const [horizon, setHorizon] = useState<Horizon>(5)
  const metricLabels = { net: '净收益', gross: '开盘报价毛收益', shape: `T+${horizon} 形态收益` }
  const [board, setBoard] = useState<Row['board']>('main')
  const [selectedRevision, setRevision] = useState<number | null>(null)
  const [page, setPage] = useState(0)
  const revisions = useMemo(() => [...new Set([...(research.data?.records.map(row => row.rule_revision) ?? []),
    ...(currentRevision != null ? [currentRevision] : [])])].sort((a, b) => b - a), [research.data, currentRevision])
  const revision = selectedRevision ?? currentRevision ?? revisions[0]
  const rows = useMemo(() => (research.data?.records ?? []).filter(row =>
    row.rule_revision === revision && row.origin === origin && row.board === board
  ).sort((a, b) => b.signal_date.localeCompare(a.signal_date) || a.symbol.localeCompare(b.symbol)), [research.data, revision, origin, board])
  const groups = useMemo(() => cohortStats(rows, horizon), [rows, horizon])
  const paired = useMemo(() => pairedComparison(rows, metric, horizon), [rows, metric, horizon])
  const pages = Math.ceil(rows.length / 50)
  const visiblePage = Math.min(page, Math.max(0, pages - 1))
  const rules = rows[0]?.rules
  return <section className={panelClass} aria-labelledby="huichun-research-heading">
    <div className="flex flex-wrap items-start justify-between gap-3">
      <div><h2 id="huichun-research-heading" className="text-sm font-semibold">全候选与零轴对照</h2>
        <p className="mt-1 text-xs text-secondary">按扫描自动收录 · 更新于 {stamp(research.data?.updated_at)}</p></div>
      <button className={buttonClass} disabled={busy || !research.data?.records.length} onClick={onRefresh}>刷新研究收益</button>
    </div>
    <ErrorNotice error={research.error} retry={() => void research.refetch()} /><ErrorNotice error={refreshError} />
    {research.isPending ? <LoadingRows /> : research.data && <>
      <div className="my-4 grid gap-3 text-xs sm:grid-cols-2 lg:grid-cols-4">
        <label className="space-y-1.5">样本来源<select className={inputClass} value={origin} onChange={e => {
          setOrigin(e.target.value as Row['origin']); setMetric(e.target.value === 'forward' ? 'net' : 'shape'); setPage(0)
        }}>
          <option value="forward">收盘当日首次记录</option><option value="backfill">历史回扫</option></select></label>
        <label className="space-y-1.5">股票板块<select className={inputClass} value={board} onChange={e => { setBoard(e.target.value as Row['board']); setPage(0) }}>
          {Object.entries(boards).map(([key, label]) => <option value={key} key={key}>{label}</option>)}</select></label>
        <label className="space-y-1.5">规则版本<select className={inputClass} value={revision ?? ''} onChange={e => { setRevision(Number(e.target.value)); setPage(0) }}>
          {revisions.map(n => <option value={n} key={n}>版本 {n}</option>)}</select></label>
        <label className="space-y-1.5">观察窗口<select aria-label="研究观察窗口" className={inputClass} value={horizon} onChange={e => { setHorizon(Number(e.target.value) as Horizon); setPage(0) }}>
          {[5, 10, 20].map(n => <option value={n} key={n}>{n} 个交易日</option>)}</select></label>
      </div>
      <p className="text-xs leading-relaxed text-secondary">形态收益：信号收盘至 T+{horizon}；成交模拟：T+1 开盘至 T+2 开盘。</p>
      {rows.length === 0 ? <p className="py-4 text-sm text-secondary">{research.data.records.length
        ? '此来源、板块和规则版本暂无样本，可切换条件查看。'
        : '暂无研究样本。运行回春筛选后，确认形态和仅超出零轴门槛的对照会自动收录。'}</p> : <>
        <p className="text-xs leading-relaxed text-secondary">每列展示均值，括号为有结果的样本数。</p>
        <div className="mt-2 overflow-x-auto"><table className="w-full min-w-[600px] text-left text-xs tabular-nums" aria-label="研究分组统计">
          <thead><tr>{['分组', '全部样本', `信号收盘至 T+${horizon}`, '同日市场超额', '开盘报价毛收益', '模拟成交净收益'].map(label => <th className="py-2 pr-4 font-normal text-secondary" key={label}>{label}</th>)}</tr></thead>
          <tbody>{groups.map(group => <tr key={group.key} className="border-t border-border">
            <th className="py-2 pr-4 font-medium">{cohortLabels[group.key]}</th><td>{group.count}</td>
            {group.metrics.map((metric, i) => <td key={i}>{i === 1 ? pointDifference(metric.value) : pct(metric.value, true)} <span className="text-secondary">（{metric.count}）</span></td>)}
          </tr>)}</tbody>
        </table></div>
        <div className="mt-3 flex flex-wrap items-center gap-3 text-xs">
          <label className="flex items-center gap-2 whitespace-nowrap">配对指标<select className={inputClass} value={metric} onChange={e => setMetric(e.target.value as Metric)}>
            {Object.entries(metricLabels).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></label>
        <p aria-label={`同日${metricLabels[metric]}对照`}>同日{metricLabels[metric]}差
          <strong className="mx-2 tabular-nums">{pointDifference(paired.diff)}</strong>
          <span className="text-secondary">{paired.count} 个共同信号日</span>
        </p></div>
        <div className="mt-4 overflow-x-auto"><table className="w-full min-w-[850px] text-left text-xs tabular-nums" aria-label="研究样本明细">
          <thead><tr>{['信号日', '股票', '分组', '零轴距离', `T+${horizon} 形态收益`, '同日市场超额', '开盘报价毛收益', '模拟成交净收益'].map(label => <th key={label} className="py-2 pr-3 font-normal text-secondary">{label}</th>)}</tr></thead>
          <tbody>{rows.slice(visiblePage * 50, (visiblePage + 1) * 50).map(row => <tr key={row.id} className="border-t border-border">
            <td className="py-2 pr-3">{row.signal_date}</td><td>{row.name} <span className="text-secondary">{row.symbol}</span></td><td>{cohortLabels[row.cohort]}</td>
            <td>{pct(row.zero_distance)}</td><td><ShapeOutcome row={row} horizon={horizon} /></td>
            <td>{excessOutcome(row, horizon)}</td>
            <td>{pct(row.execution.gross_return_pct, true)}</td>
            <td>{row.execution.entry_cost == null && row.execution.net_return_pct == null ? statusLabel(row.execution.status) : <details>
              <summary className="cursor-pointer">{outcome(row.execution.net_return_pct, row.execution.status)}</summary>
              <div className="whitespace-nowrap py-1 text-secondary"><p>{row.execution.entry_date} 买入 {row.execution.shares ?? '—'} 股 × {price(row.execution.entry_price)} 元</p>
                {row.execution.exit_price != null && <p>{row.execution.exit_date} 卖出 · {price(row.execution.exit_price)} 元</p>}
                <p>占用资金 {price(row.execution.entry_cost)} 元</p><p>买入费 {price(row.execution.buy_fee)} 元 · 卖出税费 {price(row.execution.sell_fee)} 元</p></div>
            </details>}</td>
          </tr>)}</tbody>
        </table></div><Pagination page={visiblePage} pages={pages} onPage={setPage} />
      </>}
      {(rows.length > 0 || research.data.limitations.length > 0) && <details className="mt-4 border-t border-border pt-3 text-xs text-secondary">
        <summary className="cursor-pointer rounded py-1 focus-visible:outline focus-visible:outline-accent">研究口径与执行假设</summary>
        <div className="mt-2 max-w-prose space-y-3 leading-relaxed">
          {rows.length > 0 && <>
            <p>对照组仅超出零轴距离门槛{rules && `（大于 ${pct(rules.zero_threshold)}，至多 ${pct(rules.zero_threshold + .01)}）`}，其余入选条件相同。</p>
            <p>同日{metricLabels[metric]}差：各日先算确认组均值减对照组均值，再按日等权。</p>
            <div><p className="font-medium text-foreground">相近波动率对照 · {paired.matchedCount} 对可比样本</p>
              <p className="mt-1">配对{metricLabels[metric]}差 {pointDifference(paired.matchedDiff)}，覆盖 {paired.matchedDays} 个日期。每只确认股在同日同板块选择近 20 日波动率最接近、较高值至多为较低值 1.2 倍的对照。允许复用对照，先匹配再剔除收益缺失的配对，按日等权。</p>
            </div>
            <p>开盘报价取 T+1 开盘至 T+2 开盘。同日最多出现 {paired.peakCandidates} 只确认股，净收益按每只独立分配 1 万元模拟。展开净收益可核对资金占用和费用。</p>
          </>}
          {research.data.limitations.length > 0 && <ul className="list-disc space-y-1 pl-4">{research.data.limitations.map(line => <li key={line}>{line}</li>)}</ul>}
        </div>
      </details>}
    </>}
  </section>
}

function ShapeOutcome({ row, horizon }: { row: Row; horizon: Horizon }) {
  const value = shape(row, horizon)
  const details = observation(row, horizon)
  const label = outcome(value.return_pct, value.status)
  if (!details) return <>{label}</>
  return <details><summary className="cursor-pointer whitespace-nowrap">{label}</summary>
    <div className="min-w-48 py-1 text-secondary">
      <p>最大浮盈 {pct(details.mfe_pct, true)} · 最大浮亏 {pct(details.mae_pct, true)}</p>
      <p>市场等权收益 {pct(details.benchmark_return_pct, true)}</p>
      <p>基准有效 / 当日样本 {details.benchmark_sample_count} / {details.benchmark_universe_count}</p>
      <p className="mt-1 max-w-64 whitespace-normal">浮盈浮亏取信号收盘后窗口内的复权最高、最低价，属于价格观察，不代表可成交收益。</p>
    </div>
  </details>
}
