import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Activity, Info, RefreshCw } from 'lucide-react'
import { EmptyState } from '@/components/EmptyState'
import { ErrorState } from '@/components/ErrorState'
import { Skeleton } from '@/components/data/Skeleton'
import { api, type EtfTurnoverFund, type EtfTurnoverLevel, type EtfTurnoverSnapshot } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { cn } from '@/lib/cn'
import { direction, number } from './display'

const verdicts: Record<EtfTurnoverLevel, string> = { strong: '成交显著放大', mild: '成交温和放大', calm: '成交未见放大' }
const sorts = {
  huijin: ['汇金持仓', (f: EtfTurnoverFund) => f.huijin_units],
  size: ['规模', (f: EtfTurnoverFund) => f.size_cny],
  amount: ['成交额', (f: EtfTurnoverFund) => f.amount_cny],
  ratio: ['倍数', (f: EtfTurnoverFund) => f.ratio],
  change: ['涨跌', (f: EtfTurnoverFund) => f.change_pct],
} as const
type SortKey = keyof typeof sorts

function sortFunds(funds: EtfTurnoverFund[], key: SortKey) {
  const value = sorts[key][1]
  // Missing values sink to the bottom; the backend basket order breaks ties.
  return [...funds].sort((a, b) => (value(b) ?? -Infinity) - (value(a) ?? -Infinity))
}

function billions(value: number | null | undefined, digits = 1) {
  return number(value == null ? null : value / 1e8, digits)
}

function times(value: number | null | undefined) {
  return value == null ? '—' : number(value, 2) + '×'
}

function percent(value: number | null | undefined) {
  return value == null ? '—' : number(value * 100, 2, true) + '%'
}

function Summary({ data }: { data: EtfTurnoverSnapshot }) {
  const totals = data.totals
  const live = Boolean(data.live)
  const ratio = totals?.ratio ?? null
  const verdict = data.conclusion ? verdicts[data.conclusion] : '暂无法判断'
  return <section aria-label="成交结论" className="rounded-card border border-border bg-surface">
    <div className="flex flex-wrap items-end justify-between gap-4 px-5 py-4">
      <div>
        <p className="text-xs text-secondary">{live ? '盘中 · 截至 ' + (data.data_time ?? '—') + ' · 与近 20 日同一时点比' : data.as_of + ' 收盘 · 与近 20 日均额比'}</p>
        <h2 className={cn('mt-1 text-lg font-semibold', data.conclusion === 'strong' && 'text-red-700 dark:text-bull')}>{verdict}</h2>
      </div>
      <div className="text-right">
        <span className="num etf-metric-value">{times(ratio)}</span>
        <p className="text-xs text-secondary">强信号阈值 {data.thresholds.strong}×</p>
      </div>
    </div>
    <dl className="etf-turnover-facts border-t border-border">
      <div><dt>合计成交额</dt><dd><span className="num">{billions(totals?.amount_cny)}</span> 亿</dd></div>
      <div><dt>20 日均额</dt><dd><span className="num">{billions(totals?.avg_amount_cny)}</span> 亿</dd></div>
      {live && <div><dt>已达成日均</dt><dd className="num">{totals?.progress == null ? '—' : number(totals.progress * 100, 0) + '%'}</dd></div>}
      <div><dt>规模加权涨跌</dt><dd className={cn('num', direction(totals?.weighted_change_pct))}>{percent(totals?.weighted_change_pct)}</dd></div>
      {totals?.tail_share != null && <div><dt>尾盘 30 分钟占比</dt><dd><span className="num">{number(totals.tail_share * 100, 1)}%</span>
        {totals.tail_share_avg != null && <span className="ml-1 text-xs font-normal text-secondary">均值 {number(totals.tail_share_avg * 100, 1)}%</span>}</dd></div>}
    </dl>
  </section>
}

function Spark({ values, dates }: { values: (number | null)[]; dates: string[] }) {
  const known = values.filter((v): v is number => v != null)
  if (known.length < 2) return <span className="text-secondary">—</span>
  const max = Math.max(...known), min = Math.min(...known), span = Math.max(max - min, 1)
  const step = 80 / Math.max(values.length - 1, 1)
  const points = values.map((v, i) => v == null ? null : `${(i * step).toFixed(1)},${(22 - (v - min) / span * 20).toFixed(1)}`).filter(Boolean).join(' ')
  return <svg className="etf-spark" viewBox="0 0 80 24" role="img" aria-label={`${dates[0]} 至 ${dates.at(-1)} 成交额，最高 ${billions(max, 2)} 亿元`}>
    <polyline points={points} />
  </svg>
}

function units(value: number | null | undefined, signed = false) {
  return number(value == null ? null : value / 1e8, signed ? 2 : 1, signed)
}

function FundRows({ data }: { data: EtfTurnoverSnapshot }) {
  const [sort, setSort] = useState<SortKey>('huijin')
  const dates = data.recent_dates ?? []
  return <section aria-label="逐只成交" className="mt-6">
    <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
      <h2 className="text-[16px] font-semibold">逐只 ETF</h2>
      <div className="etf-segments" role="group" aria-label="排序">
        {(Object.keys(sorts) as SortKey[]).map(key => <button key={key} type="button" aria-pressed={sort === key} onClick={() => setSort(key)}>{sorts[key][0]}</button>)}
      </div>
    </div>
    <div className="overflow-x-auto rounded-card border border-border bg-surface">
      <table className="etf-table">
        <thead><tr><th scope="col">基金</th>
          <th scope="col" className="etf-wide etf-right">规模<br /><span>亿元</span></th>
          <th scope="col" className="etf-right">成交额<br /><span>亿元</span></th>
          <th scope="col" className="etf-right">{data.live ? '同时点倍数' : '较 20 日均'}</th>
          <th scope="col" className="etf-right">涨跌</th>
          <th scope="col" className="etf-wide etf-right">场内份额<br /><span>亿份 · 较前日</span></th>
          <th scope="col" className="etf-wide etf-right">近 {dates.length} 日<br /><span>成交额</span></th>
          <th scope="col" className="etf-wide etf-right">汇金持仓<br /><span>{data.huijin_period_end ? data.huijin_period_end.slice(0, 4) + ' 年报 · ' : ''}亿份</span></th></tr></thead>
        <tbody>{sortFunds(data.funds, sort).map(fund => <tr key={fund.symbol}>
          <th scope="row"><strong className="text-sm font-semibold">{fund.name}</strong><small className="num block text-[11px] text-secondary">{fund.symbol}</small></th>
          <td className="etf-wide etf-right num">{billions(fund.size_cny)}</td>
          <td className="etf-right num">{billions(fund.amount_cny, 2)}
            {fund.avg_amount_cny != null && <small className="etf-missing">均 {billions(fund.avg_amount_cny, 2)}</small>}</td>
          <td className={cn('etf-right num', fund.ratio != null && fund.ratio >= data.thresholds.strong && 'font-semibold text-red-700 dark:text-bull')}>
            {times(fund.ratio)}
            {fund.ratio == null && fund.avg_amount_cny == null && <small className="etf-missing">缺 20 日成交额</small>}
          </td>
          <td className={cn('etf-right num', direction(fund.change_pct))}>{percent(fund.change_pct)}</td>
          <td className="etf-wide etf-right num">{units(fund.listed_units)}
            {fund.listed_change_units != null && <small className={cn('etf-missing', direction(fund.listed_change_units))}>{units(fund.listed_change_units, true)}</small>}</td>
          <td className="etf-wide etf-right"><Spark values={fund.recent_amounts_cny ?? []} dates={dates} /></td>
          <td className="etf-wide etf-right num">{fund.huijin_units === 0 ? <span className="text-xs text-secondary">未列名</span> : units(fund.huijin_units)}</td>
        </tr>)}</tbody>
      </table>
    </div>
  </section>
}

function ShareChart({ shares }: { shares: EtfTurnoverSnapshot['shares'] }) {
  const series = shares.series
  const indexed = series.filter(point => point.index != null)
  if (!indexed.length) return null
  const max = Math.max(...series.map(p => Math.abs(p.listed_change_value_cny ?? 0)), 1)
  const low = Math.min(...indexed.map(p => p.index!)), high = Math.max(...indexed.map(p => p.index!))
  const span = Math.max(high - low, 1)
  const width = series.length * 10
  const line = series.map((p, i) => p.index == null ? null : `${i * 10 + 5},${(4 + (high - p.index) / span * 92).toFixed(2)}`).filter(Boolean).join(' ')
  const last = indexed.at(-1)!
  return <section aria-label="场内份额变动" className="mt-6">
    <div className="mb-3 flex flex-wrap items-baseline justify-between gap-2">
      <h2 className="text-[16px] font-semibold">场内份额变动</h2>
      <span className="text-xs text-secondary">截至 {shares.units_as_of}</span>
    </div>
    <div className="rounded-card border border-border bg-surface px-5 py-4">
      <div className="flex flex-wrap items-baseline gap-x-6 gap-y-1">
        <p className="text-sm">份额指数 <strong className="num text-xl font-semibold">{number(last.index, 1)}</strong>
          <span className="ml-1 text-xs text-secondary">{series[0].date} = 100</span></p>
        <p className="flex items-center gap-3 text-xs text-secondary">
          <span className="inline-flex items-center gap-1"><i className="etf-legend-bar" aria-hidden />每日变动等值（亿元）</span>
          <span className="inline-flex items-center gap-1"><i className="etf-legend-line" aria-hidden />份额指数</span>
        </p>
      </div>
      <svg role="img" aria-label={`份额指数从 100 变为 ${number(last.index, 1)}`} className="etf-share-chart mt-3" viewBox={`0 0 ${width} 100`} preserveAspectRatio="none">
        <line x1="0" x2={width} y1="50" y2="50" className="etf-axis" />
        {series.map((p, i) => {
          const v = p.listed_change_value_cny
          if (v == null) return null
          const h = Math.abs(v) / max * 46
          return <rect key={p.date} x={i * 10 + 1.5} width="7" y={v >= 0 ? 50 - h : 50} height={Math.max(h, 0.5)}
            className={v >= 0 ? 'etf-bar-up' : 'etf-bar-down'}><title>{p.date} {billions(v, 2)} 亿元</title></rect>
        })}
        <polyline points={line} className="etf-index-line" />
      </svg>
      <div className="mt-1 flex justify-between text-[11px] text-secondary num"><span>{series[0].date}</span><span>{series.at(-1)!.date}</span></div>
    </div>
  </section>
}

function Notes({ data }: { data: EtfTurnoverSnapshot }) {
  return <details className="mt-4 rounded-card border border-border bg-surface">
    <summary className="flex cursor-pointer items-center gap-2 px-4 py-3 text-sm font-medium"><Info size={14} aria-hidden />口径说明</summary>
    <ul className="list-disc space-y-1.5 border-t border-border py-3 pl-8 pr-4 text-xs leading-relaxed text-secondary">
      <li>倍数 = 合计成交额 ÷ 近 20 个交易日平均成交额（{data.baseline_start} 至 {data.baseline_end}）。≥{data.thresholds.strong}× 为显著放大，≥{data.thresholds.mild}× 为温和放大。</li>
      <li>盘中按近 20 日同一时点已成交的平均比例折算分母；缺少足够的 ETF 分钟数据时只给已达成日均的进度，不给倍数。</li>
      <li>规模 = 最近核实的场内份额（{data.shares.units_as_of ?? '—'}）× 现价，规模加权涨跌按此加权；份额“较前日”为同一核实日的场内份额变动。</li>
      <li>汇金持仓为 {data.huijin_period_end ?? '最近'} 年报前十大持有人中中央汇金投资与汇金资管的合计份额，不含其他具名资管计划。此后定期报告不再列示持有人，不代表当前持仓；“未列名”指该年报前十名中没有汇金。</li>
      <li>尾盘占比在收盘后、当日分钟数据完整时计算，与近 20 日同时段的平均占比比较。</li>
      <li>份额指数以首个完整交易日为 100，各基金份额固定按首日净值折算后加总，只反映净申赎，不受价格涨跌影响。份额变化来自所有投资者，不能据此认定任何机构的交易。</li>
    </ul>
  </details>
}

export function Turnover() {
  const [auto, setAuto] = useState(true)
  const result = useQuery({
    queryKey: QK.etfTurnover, queryFn: api.etfTurnover, staleTime: 15_000,
    refetchInterval: query => auto && query.state.data?.live && !query.state.data.stale ? 30_000 : false,
  })
  const data = result.data
  if (result.isPending) return <div role="status" aria-label="正在读取成交数据" className="space-y-4 py-8"><Skeleton h="h-24" /><Skeleton h="h-40" /></div>
  if (result.isError && !data) return <ErrorState title="成交数据读取失败" hint={result.error.message} onRetry={() => void result.refetch()} retrying={result.isFetching} />
  if (!data?.as_of) return <EmptyState icon={Activity} title="暂无 ETF 日 K 数据" hint="需在数据页开启 ETF 日 K 拉取并完成同步后查看成交倍数。" />
  return <>
    <div className="mb-3 flex flex-wrap items-center justify-end gap-3 text-xs text-secondary">
      <button type="button" className="etf-refresh" disabled={result.isFetching} onClick={() => void result.refetch()}>
        <RefreshCw aria-hidden size={13} className={cn(result.isFetching && 'animate-spin')} />立即刷新</button>
      <label className="inline-flex cursor-pointer items-center gap-1.5"><input type="checkbox" checked={auto} onChange={event => setAuto(event.target.checked)} />盘中自动刷新</label>
    </div>
    {result.isError && <p role="status" className="mb-3 text-sm text-amber-700 dark:text-warning">更新失败，当前显示上一次成功读取的数据。 <button className="etf-link" onClick={() => void result.refetch()}>重试</button></p>}
    {data.stale && <p role="status" className="mb-3 text-sm text-amber-700 dark:text-warning">ETF 实时行情自 {data.data_time} 起未再更新，以下是当日不完整的数据。</p>}
    {data.live && !data.stale && data.totals?.ratio == null && data.totals?.progress != null && <p role="status" className="mb-3 text-sm text-secondary">缺少近 20 日 ETF 分钟数据，盘中暂不计算同时点倍数。</p>}
    <Summary data={data} />
    <FundRows data={data} />
    <ShareChart shares={data.shares} />
    <Notes data={data} />
  </>
}
