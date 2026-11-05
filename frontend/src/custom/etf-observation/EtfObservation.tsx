import { Fragment, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { CalendarDays, ChartNoAxesColumn, ChevronDown, ChevronRight, FileText, Info } from 'lucide-react'
import { EmptyState } from '@/components/EmptyState'
import { ErrorState } from '@/components/ErrorState'
import { Skeleton } from '@/components/data/Skeleton'
import { api, type EtfFlowWindow, type EtfObservationDay, type EtfObservationFund, type EtfObservationSnapshot } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { cn } from '@/lib/cn'
import { Disclosures } from './Disclosures'
import { number, SourceLink } from './display'
import './observation.css'

const scopes = { fund_total_verified: '全基金', listed_only: '仅场内', unverified: '全基金待核' }
const windows = [[1, '单日'], [20, '近20日'], [60, '近60日']] as const

function billions(value: number | null) {
  return number(value == null ? null : value / 1e8, 2, true)
}

function direction(value: number | null) {
  if (value == null || value === 0) return ''
  return value > 0 ? 'text-red-700 dark:text-bull' : 'text-emerald-700 dark:text-bear'
}

function Loading() {
  return <div role="status" aria-label="正在读取 ETF 数据" className="space-y-4 py-8">
    <Skeleton w="w-48" h="h-5" /><Skeleton h="h-16" /><Skeleton h="h-32" />
  </div>
}

function Metric({ label, count, amount }: { label: string; count: number; amount: number | null }) {
  return <div className="etf-metric">
    <dt className="flex flex-wrap items-baseline gap-x-2 text-sm font-medium">
      {label}<span className="text-xs font-normal text-secondary">{count} 只 ETF</span>
    </dt>
    <dd className="mt-2 flex flex-wrap items-baseline gap-x-2">
      {amount === null ? <span className="text-xl font-medium text-secondary">数据未齐</span> :
        <><span className={cn('num etf-metric-value', direction(amount))}>{billions(amount)}</span>
          <span className="text-xs text-secondary">亿元</span></>}
    </dd>
  </div>
}

function DayEvidence({ day }: { day: EtfObservationDay }) {
  const facts = [
    ['前一日场内份额', number(day.previous_listed_units), '份', day.previous_shares_source, day.previous_date],
    ['当日场内份额', number(day.listed_units), '份', day.shares_source, day.date],
    ['当日单位净值', number(day.unit_nav_cny, 4), '元', day.nav_source, day.date],
  ] as const
  return <div>
    {day.status !== 'ok' && <p role="status" className="mb-2 text-xs text-amber-700 dark:text-warning">当日数据待补，请以已核实的字段为准。</p>}
    <dl className="grid gap-3 sm:grid-cols-3">
      {facts.map(([label, value, unit, source, date]) => <div key={label}>
        <dt className="text-xs text-secondary">{label}</dt>
        <dd className="mt-1 flex flex-wrap items-baseline gap-1">
          <span className="num text-sm font-medium">{value}</span><span className="text-xs text-secondary">{unit}</span>
        </dd>
        <dd className="mt-1 flex flex-wrap gap-x-2 text-xs text-secondary">
          {date && <span>{date}</span>}<SourceLink href={source}>{label === '当日单位净值' ? '净值来源' : '份额来源'}</SourceLink>
        </dd>
      </div>)}
    </dl>
    <p className="mt-3 border-t border-border pt-2 text-xs leading-6 text-secondary">
      场内份额变动 <strong className="num font-medium text-foreground">{number(day.listed_change_units, 0, true)}</strong> 份
      <span className="mx-2" aria-hidden>·</span>按当日净值折合 <strong className="num font-medium text-foreground">{number(day.listed_change_value_cny, 2, true)}</strong> 元
    </p>
  </div>
}

function FundDetail({ fund, window, asOf }: { fund: EtfObservationFund; window: EtfFlowWindow; asOf: string }) {
  const [picked, setPicked] = useState('')
  const detail = useQuery({
    queryKey: QK.etfObservationDetail(fund.symbol, window, asOf),
    queryFn: () => api.etfObservationDetail(fund.symbol, window, asOf),
    enabled: window !== 1, staleTime: 60_000,
  })
  const rows = window === 1 ? (fund.latest ? [fund.latest] : []) : detail.data?.rows ?? []
  const day = rows.find(row => row.date === picked) ?? rows.at(-1)
  return <section aria-label={fund.name + '计算明细'} className="etf-detail">
    <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
      <h3 className="text-sm font-semibold">{fund.name} · 计算明细</h3>
      {rows.length > 1 && <label className="flex items-center gap-2 text-xs text-secondary">核对交易日
        <select className="etf-select" value={day?.date ?? ''} onChange={event => setPicked(event.target.value)}>
          {[...rows].reverse().map(row => <option key={row.date}>{row.date}</option>)}
        </select>
      </label>}
    </div>
    {window !== 1 && detail.isError ? <div role="alert" className="text-sm text-secondary">逐日明细读取失败。 <button type="button" className="etf-link" onClick={() => void detail.refetch()}>重试</button></div>
      : window !== 1 && detail.isPending ? <Skeleton h="h-16" />
        : day ? <DayEvidence day={day} /> : <p className="text-sm text-secondary">该交易日尚无可核对的份额和净值。</p>}
    <p className="mt-2 max-w-prose text-xs leading-relaxed text-secondary">{fund.reason} <SourceLink href={fund.source_url}>口径依据</SourceLink></p>
  </section>
}

function FundTable({ data }: { data: EtfObservationSnapshot }) {
  const [opened, setOpened] = useState<string | null>(null)
  const maxAmount = Math.max(...data.funds.map(fund => Math.abs(fund.period_listed_change_value_cny ?? 0)), 1)
  return <section aria-label="逐只 ETF 份额与等值" className="mt-6">
    <div className="mb-3 flex flex-wrap items-baseline justify-between gap-2">
      <h2 className="text-[16px] font-semibold">逐只 ETF</h2>
      <span className="text-xs text-secondary">金额单位 · 亿元</span>
    </div>
    <div className="overflow-hidden rounded-card border border-border bg-surface">
      <table className="etf-table etf-funds">
        <thead><tr><th scope="col">基金</th><th scope="col" className="etf-wide">份额口径</th>
          <th scope="col" className="etf-wide etf-right">期末场内份额<br /><span>亿份</span></th>
          <th scope="col" className="etf-wide etf-right">单位净值<br /><span>元</span></th>
          <th scope="col" className="etf-right">全基金<br className="sm:hidden" />净申购估算</th>
          <th scope="col" className="etf-right">场内份额<br className="sm:hidden" />变动等值</th></tr></thead>
        <tbody>{data.funds.map(fund => {
          const open = opened === fund.symbol
          const val = fund.period_listed_change_value_cny
          return <Fragment key={fund.symbol}><tr className={open ? 'etf-active-row' : undefined}>
            <th scope="row"><button type="button" className="etf-fund-button" aria-expanded={open} onClick={() => setOpened(open ? null : fund.symbol)}>
              {open ? <ChevronDown size={14} aria-hidden /> : <ChevronRight size={14} aria-hidden />}
              <span><strong>{fund.name}</strong><small className="num">{fund.symbol}</small>
                <small className="etf-narrow">{scopes[fund.scope]}</small></span>
            </button></th>
            <td className={cn('etf-wide text-xs', fund.scope === 'unverified' ? 'text-amber-700 dark:text-warning' : 'text-secondary')}>{scopes[fund.scope]}</td>
            <td className="etf-wide etf-right num">{number(fund.latest?.listed_units == null ? null : fund.latest.listed_units / 1e8, 2)}</td>
            <td className="etf-wide etf-right num">{number(fund.latest?.unit_nav_cny, 4)}</td>
            <td className={cn('etf-right num', direction(fund.period_fund_net_estimate_cny))}>
              {billions(fund.period_fund_net_estimate_cny)}
              {fund.scope === 'fund_total_verified' && fund.period_fund_net_estimate_cny == null && <small className="etf-missing">数据待补</small>}
            </td>
            <td className="etf-right"><div className="etf-distribution">
              {val != null && <span className="etf-track" aria-hidden><i className={val < 0 ? 'etf-negative-bar' : 'etf-positive-bar'}
                style={{ width: (Math.abs(val) / maxAmount * 50) + '%' }} /></span>}
              <span className={cn('num', direction(val))}>{billions(val)}</span>
            </div>{val == null && <small className="etf-missing">已覆盖 {fund.days_covered} 日</small>}</td>
          </tr>{open && data.as_of && <tr><td colSpan={6} className="etf-expanded"><FundDetail fund={fund} window={data.window} asOf={data.as_of} /></td></tr>}</Fragment>
        })}</tbody>
      </table>
    </div>
  </section>
}

function FlowSummary({ data }: { data: EtfObservationSnapshot }) {
  return <>
    <div className="etf-summary rounded-card border border-border bg-surface">
      <dl className="grid sm:grid-cols-2">
        <Metric label="全基金净申购估算" count={data.fund_pool_size} amount={data.totals.fund_net_estimate_cny} />
        <Metric label="场内份额变动等值" count={data.listed_pool_size} amount={data.totals.listed_change_value_cny} />
      </dl>
      <p className="flex items-start gap-2 border-t border-border px-4 py-2.5 text-xs leading-relaxed text-secondary">
        <Info size={14} aria-hidden className="mt-px shrink-0" />
        <span>两项覆盖的基金有重叠，请分别阅读。等值按每日份额变化乘当日单位净值计算，涵盖所有投资者。</span>
      </p>
    </div>
    <FundTable data={data} />
    {data.trend.length > 1 && <details className="mt-4 rounded-card border border-border bg-surface">
      <summary className="cursor-pointer px-4 py-3 text-sm font-medium">查看区间每日合计</summary>
      <div className="max-h-72 overflow-y-auto border-t border-border"><table className="etf-table">
        <thead><tr><th>交易日</th><th className="etf-right">全基金估算（亿元）</th><th className="etf-right">场内等值（亿元）</th></tr></thead>
        <tbody>{[...data.trend].reverse().map(day => <tr key={day.date}><th className="num">{day.date}</th>
          <td className="etf-right num">{billions(day.fund_net_estimate_cny)}</td><td className="etf-right num">{billions(day.listed_change_value_cny)}</td></tr>)}</tbody>
      </table></div>
    </details>}
  </>
}

export default function EtfObservation() {
  const [view, setView] = useState<'flow' | 'disclosure'>('flow')
  const [period, setPeriod] = useState<EtfFlowWindow>(1)
  const [date, setDate] = useState<string>()
  // Named holdings have their own publication dates, independent of the flow window.
  const window = view === 'flow' ? period : 1
  const asOf = view === 'flow' ? date : undefined
  const result = useQuery({
    queryKey: QK.etfObservation(window, asOf), queryFn: () => api.etfObservation(window, asOf), staleTime: 60_000,
  })
  const data = result.data
  return <div className="etf-page min-h-full bg-base text-foreground">
    <header className="etf-header border-b border-border bg-surface">
      <div className="etf-width">
        <h1 className="text-xl font-semibold">宽基 ETF 份额与汇金披露</h1>
        <nav aria-label="观察视图" className="mt-4 flex gap-6">
          <button type="button" aria-pressed={view === 'flow'} onClick={() => setView('flow')} className="etf-tab"><ChartNoAxesColumn aria-hidden size={16} />份额观察</button>
          <button type="button" aria-pressed={view === 'disclosure'} onClick={() => setView('disclosure')} className="etf-tab"><FileText aria-hidden size={15} />汇金披露</button>
        </nav>
      </div>
    </header>
    <div className="etf-width pb-7 pt-5">
      {view === 'flow' && <div className="mb-4 flex flex-wrap items-end justify-between gap-3">
        <div><h2 className="text-[16px] font-semibold">{period === 1 ? '单日份额变动' : '区间份额变动'}</h2>
          {data?.as_of && <p className="mt-1 text-xs text-secondary">{period > 1 && data.start_date ? data.start_date + ' 至 ' : ''}{data.as_of}{period > 1 && ' · ' + data.session_count + ' 个交易日'}</p>}
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <div className="etf-segments" role="group" aria-label="观察区间">
            {windows.map(([value, label]) => <button key={value} type="button" aria-pressed={period === value} onClick={() => setPeriod(value)}>{label}</button>)}
          </div>
          <label className="etf-date"><CalendarDays aria-hidden size={14} /><select aria-label="截止交易日" value={date ?? ''} onChange={event => setDate(event.target.value || undefined)}>
            <option value="">{data && !data.latest_complete_date && data.latest_observed_date ? '最近可查日' : '最近完整日'}</option>{(data?.dates ?? (date ? [date] : [])).slice().reverse().map(day => <option key={day}>{day}</option>)}
          </select></label>
        </div>
      </div>}
      {result.isPending ? <Loading /> : result.isError && !data ? <ErrorState title="ETF 数据读取失败" hint={result.error.message} onRetry={() => void result.refetch()} retrying={result.isFetching} /> :
        data && <>
          {result.isError && <p role="status" className="mb-3 text-sm text-amber-700 dark:text-warning">更新失败，当前显示上一次成功读取的数据。 <button className="etf-link" onClick={() => void result.refetch()}>重试</button></p>}
          {view === 'disclosure' ? <Disclosures data={data} /> : <>
            {!date && data.latest_observed_date && data.latest_observed_date !== data.latest_complete_date && <p role="status" className="mb-3 text-sm text-amber-700 dark:text-warning">{data.latest_observed_date} 的数据尚未齐备。</p>}
            {data.as_of && data.session_count < period && <p role="status" className="mb-3 text-sm text-secondary">已覆盖 {data.session_count} / {period} 个交易日，区间合计待补。</p>}
            {data.as_of && data.funds.length ? <FlowSummary key={data.as_of + data.window} data={data} /> :
              <EmptyState icon={ChartNoAxesColumn} title="暂无可汇总的份额数据" hint="等待交易日份额与净值入库后，可在此查看逐只变动。" />}
          </>}
        </>}
    </div>
  </div>
}
