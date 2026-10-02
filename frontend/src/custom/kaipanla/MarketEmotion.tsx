import { useEffect, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import ReactECharts from 'echarts-for-react'
import { Activity, RefreshCw } from 'lucide-react'
import { api, type KaipanlaLiveStock, type KaipanlaMarketColumn, type KaipanlaMarketDataset, type KaipanlaMarketEmotion } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { cn } from '@/lib/cn'
import { cnDateTimeFromUtc, fmtPrice, fmtVolume } from '@/lib/format'
import { useChartTheme } from '@/lib/theme'
import { PageHeader } from '@/components/PageHeader'
import { DatePicker } from '@/components/DatePicker'
import { IndexTicker } from '@/components/dashboard/widgets'
import { StockPreviewDialog } from '@/components/StockPreviewDialog'
import type { NavItem } from '@/lib/listNav'
import type { IntradayAnnotation } from '@/components/EChartsIntraday'
import { LiveIndexChart } from './LiveIndexChart'
import { extractLiveHighlights } from './liveAnnotationText'
import { previewItems, previewStock, type OpenStock, type StockFields } from './stockPreview'

const panelClass = 'rounded-card border border-border bg-surface/80 p-4'
const buttonClass = 'inline-flex items-center justify-center gap-1.5 rounded-btn border border-border bg-elevated px-3 py-1.5 text-xs text-secondary transition-colors hover:text-foreground focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent disabled:cursor-not-allowed disabled:opacity-50'
const stateLabels = { ok: '已获取', partial: '部分可用', empty: '暂无数据', error: '获取失败' }
const STOCK_COLUMNS: KaipanlaMarketColumn[] = [
  { name: 'StockID', label: '代码', unit: 'text' },
  { name: 'Name', label: '名称', unit: 'text' },
  { name: 'price', label: '价格', unit: 'number' },
  { name: 'change_pct', label: '涨跌幅', unit: 'percent' },
  { name: 'board_tag', label: '连板标签', unit: 'text' },
  { name: 'limit_up_ts', label: '涨停时间（北京时间）', unit: 'text' },
  { name: 'seal_amount', label: '封单额', unit: 'yuan' },
  { name: 'max_seal_amount', label: '最大封单额', unit: 'yuan' },
  { name: 'turnover', label: '成交额', unit: 'yuan' },
  { name: 'main_net', label: '主力净额', unit: 'yuan' },
  { name: 'turnover_pct', label: '换手率', unit: 'percent' },
  { name: 'reason', label: '涨停原因', unit: 'text' },
  { name: 'concepts', label: '所属概念', unit: 'text' },
]

function beijingToday(now = Date.now()): string {
  return new Intl.DateTimeFormat('sv-SE', { timeZone: 'Asia/Shanghai' }).format(new Date(now))
}

function latestTradingWindow(now: number): boolean {
  const date = new Date(now)
  const weekday = new Intl.DateTimeFormat('en-US', { timeZone: 'Asia/Shanghai', weekday: 'short' }).format(date)
  if (weekday === 'Sat' || weekday === 'Sun') return false
  const parts = new Intl.DateTimeFormat('en-GB', { timeZone: 'Asia/Shanghai', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }).formatToParts(date)
  const minutes = Number(parts.find(part => part.type === 'hour')?.value) * 60 + Number(parts.find(part => part.type === 'minute')?.value)
  return minutes >= 9 * 60 + 15 && minutes <= 11 * 60 + 30 || minutes >= 13 * 60 && minutes <= 15 * 60 + 10
}

function preserveSameDate(previous: KaipanlaMarketEmotion | undefined, current: KaipanlaMarketEmotion): KaipanlaMarketEmotion {
  if (!previous) return current
  if (current.state === 'error' && !current.date) return { ...previous, state: 'error' }
  if (!current.date || previous.date !== current.date) return current
  const datasets = Object.fromEntries(Object.entries(current.datasets).map(([key, dataset]) => {
    const old = previous.datasets[key]
    return [key, dataset.state === 'error' && old?.rows.length && old.date === dataset.date
      ? { ...old, state: 'error' as const, message: '当前获取失败，保留上次有效快照。' } : dataset]
  }))
  const history = current.history.state === 'error' && previous.history.rows.length
    ? { ...previous.history, state: 'error' as const } : current.history
  return { ...current, datasets, history }
}

function numberValue(value: unknown): number | null {
  if (value == null || value === '' || typeof value === 'boolean') return null
  if (typeof value !== 'number' && typeof value !== 'string') return null
  if (typeof value === 'string' && !value.trim()) return null
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed : null
}

function strengthColor(value: unknown): string {
  const score = numberValue(value)
  if (score == null || score < 0 || score > 100) return 'text-secondary'
  if (score >= 90) return 'text-red-600'
  if (score >= 70) return 'text-bull'
  if (score >= 55) return 'text-orange-600 dark:text-orange-500'
  if (score >= 45) return 'text-amber-600 dark:text-amber-500'
  if (score >= 30) return 'text-lime-600 dark:text-lime-500'
  return 'text-emerald-600 dark:text-bear'
}

function displayTime(value: unknown): string {
  if (value == null || value === '') return '—'
  const raw = String(value)
  const stamp = /^(\d{10}|\d{13})$/.test(raw)
    ? new Date(Number(raw) * (raw.length === 10 ? 1000 : 1)).toISOString() : raw
  return cnDateTimeFromUtc(stamp) || raw
}

function displayValue(value: unknown, column: KaipanlaMarketColumn): string {
  if (value == null || value === '') return '—'
  if (['Time', 'time', 'limit_up_ts', 'published_at'].includes(column.name)) return displayTime(value)
  const numeric = numberValue(value)
  if (column.name === 'price' && numeric != null) return `${numeric.toFixed(2)}元`
  if (column.name === 'direction' && value === 'SZ') return '上涨前三'
  if (column.name === 'direction' && value === 'XD') return '下跌前三'
  if (column.unit === 'percent' && numeric != null) {
    const signed = /change|increase_rate|yesterday|leader/.test(column.name)
    return `${signed && numeric > 0 ? '+' : ''}${numeric.toFixed(2)}%`
  }
  if (column.unit === 'yuan' && numeric != null) return `${fmtVolume(numeric)}元`
  if (column.unit === 'wan' && numeric != null) return `${(numeric / 10_000).toFixed(2)}亿`
  if (column.unit === 'count' && numeric != null) return numeric.toLocaleString('en-US')
  if (numeric != null && column.unit === 'number') return numeric.toLocaleString('en-US', { maximumFractionDigits: 4 })
  return typeof value === 'string' || typeof value === 'number' ? String(value) : '—'
}

function valueColor(value: unknown, column: KaipanlaMarketColumn): string {
  if (column.unit !== 'percent' || !/change|increase_rate|yesterday|leader/.test(column.name)) return 'text-foreground'
  const numeric = numberValue(value)
  return numeric == null || numeric === 0 ? 'text-secondary' : numeric > 0 ? 'text-bull' : 'text-bear'
}

function BusinessTable({ rows, columns, label, stockFields, onOpenStock, navigationRows = rows }: {
  rows: Record<string, unknown>[]; columns: KaipanlaMarketColumn[]; label: string
  stockFields?: StockFields; onOpenStock?: OpenStock; navigationRows?: Record<string, unknown>[]
}) {
  const items = stockFields ? previewItems(navigationRows, stockFields) : []
  return <div className="overflow-x-auto">
    <table aria-label={label} className={cn('w-full text-left text-xs', columns.length > 3 && 'min-w-[36rem]')}>
      <thead><tr>{columns.map(column => <th key={column.name} className="whitespace-nowrap px-3 pb-2 font-normal text-secondary first:pl-0">{column.name === 'direction' ? '榜单'
        : column.unit === 'wan' ? column.label.replace(/([（(])万([）)])/g, '$1亿$2')
          : column.unit === 'yuan' ? column.label.replace(/[（(]元[）)]/g, '') : column.label}</th>)}</tr></thead>
      <tbody>{rows.map((row, index) => {
        const stock = stockFields ? previewStock(row[stockFields.code], row[stockFields.name]) : null
        return <tr key={index} className="border-t border-border/50">
        {columns.map(column => <td key={column.name} className="px-3 py-2 align-top first:pl-0">
          <span className={cn('block', valueColor(row[column.name], column),
            column.unit !== 'text' || numberValue(row[column.name]) != null || /time|published_at/i.test(column.name)
              ? 'whitespace-nowrap font-mono tabular-nums' : 'min-w-[5rem] max-w-[30rem] break-words [overflow-wrap:anywhere]')}>
            {stock && onOpenStock && (column.name === stockFields?.code || column.name === stockFields?.name) && row[column.name]
              ? <button type="button" onClick={() => onOpenStock(stock, items)} title={`查看${stock.name ?? stock.symbol}个股详情`}
                className="rounded-sm text-left hover:text-accent hover:underline focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent">{displayValue(row[column.name], column)}</button>
              : displayValue(row[column.name], column)}
          </span>
        </td>)}
      </tr>})}</tbody>
    </table>
  </div>
}

function DatasetSection({ dataset, title, empty, columns, hint, stockFields, onOpenStock }: {
  dataset?: KaipanlaMarketDataset; title: string; empty?: string; columns?: KaipanlaMarketColumn[]; hint?: string
  stockFields?: StockFields; onOpenStock?: OpenStock
}) {
  const visibleColumns = (columns ?? dataset?.columns ?? []).filter(column => !/_json$/.test(column.name)
    && !['record_id', 'source', 'fetched_at', 'date', 'ID', 'UID', 'Image', 'Type', 'bins', 'source_time', 'color'].includes(column.name))
  return <section className={panelClass}>
    <div className="mb-3 flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
      <h2 className="text-sm font-semibold text-foreground">{title}</h2>
      <span className="text-[11px] text-secondary">{dataset ? stateLabels[dataset.state] : '暂无数据'}{dataset?.fetched_at && ` · ${displayTime(dataset.fetched_at)}`}</span>
    </div>
    {hint && <p className="mb-3 text-[11px] text-secondary">{hint}</p>}
    {dataset?.rows.length && visibleColumns.length ? <BusinessTable label={title} rows={dataset.rows} columns={visibleColumns} stockFields={stockFields} onOpenStock={onOpenStock} />
      : <p className="py-5 text-center text-xs text-secondary">{dataset?.state === 'error' ? '该项获取失败，可刷新数据重试。' : empty ?? '暂无该日数据。'}</p>}
    {dataset?.message && <p className="mt-3 text-[11px] text-secondary">{dataset.message}</p>}
  </section>
}

function IndexSnapshots({ dataset }: { dataset?: KaipanlaMarketDataset }) {
  if (!dataset?.rows.length) return <DatasetSection dataset={dataset} title="核心指数快照" empty="暂无该日指数快照。" />
  const turnoverColumn = dataset.columns.find(column => column.name === 'turnover')
  return <section aria-label="核心指数快照" className="space-y-2">
    <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
      <h2 className="text-sm font-semibold text-foreground">核心指数快照</h2>
      <span className="text-[11px] text-secondary">{stateLabels[dataset.state]}{dataset.fetched_at && ` · ${displayTime(dataset.fetched_at)}`}</span>
    </div>
    <div className="grid auto-rows-[72px] grid-cols-2 gap-2 md:grid-cols-4">
      {dataset.rows.map((row, index) => {
        const rawCode = typeof row.StockID === 'string' ? row.StockID : ''
        const symbol = rawCode.replace(/^(SH|SZ|BJ)(\d{6})$/, '$2.$1')
        const name = typeof row.prod_name === 'string' ? row.prod_name : null
        const details = `涨跌点数 ${fmtPrice(numberValue(row.increase_amount))} · 成交额 ${displayValue(row.turnover,
          turnoverColumn ?? { name: 'turnover', label: '成交额', unit: 'text' })}`
        return <article key={rawCode || index} aria-label={name || symbol || '指数快照'} className="min-w-0" title={details}>
          <IndexTicker linked={false} item={{ symbol: symbol || '—', name,
            last_price: numberValue(row.last_px), change_pct: numberValue(row.increase_rate_pct) }} />
        </article>
      })}
    </div>
    {dataset.message && <p className="text-[11px] text-secondary">{dataset.message}</p>}
  </section>
}

function MarketCapacity({ dataset }: { dataset?: KaipanlaMarketDataset }) {
  const hint = '采用来源 Type=0 口径，市场覆盖未定义。原始单位万，10,000 万换算为 1 亿；暂无分钟曲线。'
  const row = dataset?.rows[0]
  if (!dataset || !row) return <DatasetSection dataset={dataset} title="市场量能" empty="暂无该日量能数据。" hint={hint} />
  const amount = (name: string) => {
    const value = numberValue(row[name])
    return dataset.columns.find(column => column.name === name)?.unit === 'wan' && value != null && value >= 0 ? value : null
  }
  const latest = amount('last_wan')
  const yesterday = amount('s_zrcs_wan')
  const difference = latest != null && yesterday != null ? latest - yesterday : null
  const ratio = difference != null && yesterday != null && yesterday > 0 ? difference / yesterday * 100 : null
  const percentage = ratio != null && Number.isFinite(ratio) ? ratio : null
  const color = difference == null || difference === 0 ? 'text-secondary' : difference > 0 ? 'text-red-700 dark:text-bull' : 'text-emerald-700 dark:text-bear'
  const barColor = difference == null || difference === 0 ? 'bg-secondary/60' : difference > 0 ? 'bg-bull' : 'bg-bear'
  const maximum = Math.max(latest ?? 0, yesterday ?? 0, 1)
  const formatAmount = (value: number | null) => value == null ? '—' : `${(value / 10_000).toFixed(2)}亿`
  const details = [
    { label: '昨日统计量能', value: formatAmount(amount('s_zrtj_wan')) },
    { label: '三日量能', value: formatAmount(amount('s3_zrtj_wan')) },
    { label: '预测成交额（来源）', value: typeof row.yclnstr === 'string' && row.yclnstr.trim() ? row.yclnstr : '—' },
    { label: '来源时间', value: displayTime(row.time) },
  ]
  return <section aria-label="市场量能" className={panelClass}>
    <div className="mb-4 flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
      <h2 className="text-sm font-semibold text-foreground">市场量能</h2>
      <span className="text-[11px] text-secondary">{stateLabels[dataset.state]}{dataset.fetched_at && ` · ${displayTime(dataset.fetched_at)}`}</span>
    </div>
    <div className="mb-5 flex flex-wrap items-baseline gap-x-3 gap-y-1" aria-label="量能变化">
      <span className={cn('text-sm font-semibold', color)}>{difference == null ? '暂无法比较' : difference > 0 ? '较昨日增量' : difference < 0 ? '较昨日缩量' : '较昨日持平'}</span>
      {difference != null && <span className={cn('font-mono text-lg font-semibold tabular-nums', color)}>
        {difference > 0 ? '+' : ''}{formatAmount(difference)}
        {percentage != null && <span className="ml-2 text-sm">（{percentage > 0 ? '+' : ''}{percentage.toFixed(2)}%）</span>}
      </span>}
    </div>
    <dl className="space-y-4" aria-label="最新与昨日量能对比">
      {([{ label: '当日最新量能(亿)', value: latest, today: true }, { label: '昨日量能(亿)', value: yesterday, today: false }]).map(item => <div key={item.label}>
        <div className="mb-2 flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
          <dt className="text-xs text-secondary">{item.label}</dt>
          <dd className={cn('font-mono text-[16px] font-semibold leading-6 tabular-nums', item.today ? color : 'text-foreground')}>{formatAmount(item.value)}</dd>
        </div>
        <div aria-hidden="true" className="h-3 overflow-hidden rounded-sm bg-elevated">
          {item.value != null && <div className={cn('h-full rounded-sm', item.today ? barColor : 'bg-accent/70')} style={{ width: `${item.value / maximum * 100}%` }} />}
        </div>
      </div>)}
    </dl>
    {difference == null && <p className="mt-3 text-xs text-secondary">缺少有效的最新或昨日量能，暂无法比较。</p>}
    {yesterday === 0 && <p className="mt-3 text-xs text-secondary">昨日量能为 0，变化比例不可计算。</p>}
    <dl className="mt-5 grid gap-x-6 gap-y-4 border-t border-border/50 pt-4 sm:grid-cols-2 xl:grid-cols-4">
      {details.map(item => <div key={item.label} className="min-w-0">
        <dt className="text-[11px] text-secondary">{item.label}</dt>
        <dd className="mt-1 break-words font-mono text-xs tabular-nums [overflow-wrap:anywhere]">{item.value}</dd>
      </div>)}
    </dl>
    <p className="mt-4 text-[11px] text-secondary">按最新量能与昨日量能计算变化，盘中快照不等同全天总量。</p>
    <p className="mt-1 text-[11px] text-secondary">{hint}</p>
    {dataset.message && <p className="mt-3 text-[11px] text-secondary">{dataset.message}</p>}
  </section>
}

function LimitPanels({ ladder, performance }: { ladder?: KaipanlaMarketDataset; performance?: KaipanlaMarketDataset }) {
  const counts = ladder?.rows[0]
  const rates = performance?.rows[0]
  const boards = [
    ['first_board_count', '首板'], ['second_board_count', '二板'], ['third_board_count', '三板'],
    ['fourth_board_count', '四板'], ['fifth_plus_count', '五板及以上'],
  ]
  const promotions = [
    ['two_board_promotion_pct', '二板晋级率'], ['three_board_promotion_pct', '三板晋级率'],
    ['max_board_promotion_pct', '最高板晋级率'],
  ]
  return <div className="grid min-w-0 gap-4 xl:grid-cols-2">
    {counts && ladder ? <section aria-label="连板梯队" className={cn(panelClass, 'min-w-0')}>
      <div className="mb-4 flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
        <h2 className="text-sm font-semibold text-foreground">连板梯队</h2>
        <span className="text-[11px] text-secondary">{stateLabels[ladder.state]}{ladder.fetched_at && ` · ${displayTime(ladder.fetched_at)}`}</span>
      </div>
      <dl className="grid grid-cols-5 border-y border-border/60" aria-label="各板位涨停家数">
        {boards.map(([name, label]) => <div key={name} className="min-w-0 border-r border-border/50 last:border-r-0">
          <dt className="flex h-12 items-center justify-center border-b border-border/50 bg-elevated/40 px-1 py-2 text-center text-[11px] leading-4 text-secondary sm:text-xs"><span className="[text-wrap:balance]">{label}</span></dt>
          <dd className={cn('py-4 text-center font-mono text-xl font-semibold tabular-nums sm:text-2xl', numberValue(counts[name]) == null ? 'text-secondary' : 'text-foreground')}>
            {displayValue(counts[name], { name, label, unit: 'count' })}
          </dd>
        </div>)}
      </dl>
      <p className="mt-3 text-[11px] text-secondary">各格为涨停家数；五板及以上沿用来源合并分档。</p>
      {ladder.message && <p className="mt-3 text-[11px] text-secondary">{ladder.message}</p>}
    </section> : <DatasetSection dataset={ladder} title="连板梯队" empty="暂无该日连板家数。" />}
    {rates && performance ? <section aria-label="晋级与破板" className={cn(panelClass, 'min-w-0')}>
      <div className="mb-4 flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
        <h2 className="text-sm font-semibold text-foreground">晋级与破板</h2>
        <span className="text-[11px] text-secondary">{stateLabels[performance.state]}{performance.fetched_at && ` · ${displayTime(performance.fetched_at)}`}</span>
      </div>
      <dl className="grid grid-cols-3 border-y border-border/60" aria-label="各板位晋级率">
        {promotions.map(([name, label]) => <div key={name} className="min-w-0 border-r border-border/50 last:border-r-0">
          <dt className="flex h-12 items-center justify-center border-b border-border/50 bg-elevated/40 px-1 py-2 text-center text-[11px] leading-4 text-secondary sm:text-xs"><span className="[text-wrap:balance]">{label}</span></dt>
          <dd className={cn('py-4 text-center font-mono text-lg font-semibold tabular-nums sm:text-xl', numberValue(rates[name]) == null ? 'text-secondary' : 'text-blue-700 dark:text-accent')}>
            {displayValue(rates[name], { name, label, unit: 'percent' })}
          </dd>
        </div>)}
      </dl>
      <dl className="mt-4"><div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
        <dt className="text-xs text-secondary">破板率</dt>
        <dd className={cn('font-mono text-xl font-semibold tabular-nums', numberValue(rates.broken_rate_pct) == null ? 'text-secondary' : 'text-orange-700 dark:text-orange-500')}>
          {displayValue(rates.broken_rate_pct, { name: 'broken_rate_pct', label: '破板率', unit: 'percent' })}
        </dd>
      </div></dl>
      <p className="mt-3 text-[11px] text-secondary">晋级率沿用来源口径；未提供四板独立晋级率。</p>
      {performance.message && <p className="mt-3 text-[11px] text-secondary">{performance.message}</p>}
    </section> : <DatasetSection dataset={performance} title="晋级与破板" empty="暂无该日晋级与破板数据。" />}
  </div>
}

function CoreStats({ data }: { data: KaipanlaMarketEmotion }) {
  const row = (key: string) => data.datasets[key]?.rows[0] ?? {}
  const previous = data.history.rows.find(item => item.trade_date === data.previous_date)
  const stats = [
    { label: '综合强度', key: 'emotion', value: row('emotion').strong, previous: previous?.strong,
      color: strengthColor(row('emotion').strong), bold: true },
    { label: '涨停家数', key: 'limit_counts', value: row('limit_counts').SJZT, previous: row('previous_limit_counts').SJZT, color: 'text-bull' },
    { label: '跌停家数', key: 'limit_counts', value: row('limit_counts').SJDT, previous: row('previous_limit_counts').SJDT, color: 'text-bear' },
    { label: '连板高度', key: 'emotion', value: row('emotion').lbgd, previous: previous?.lbgd },
    { label: '大幅回撤家数', key: 'emotion', value: row('emotion').df_num, previous: previous?.df_num },
    { label: '破板率', key: 'limit_performance', value: row('limit_performance').broken_rate_pct, percent: true,
      previous: row('previous_limit_performance').broken_rate_pct },
  ]
  return <section className={panelClass} aria-label="核心统计">
    <dl className="grid grid-cols-2 gap-x-5 gap-y-4 sm:grid-cols-3 xl:grid-cols-6">
      {stats.map(item => <div key={item.label}>
        <dt className="text-xs text-secondary">{item.label}</dt>
        <dd className={cn('mt-1 whitespace-nowrap font-mono text-xl tabular-nums', item.color ?? 'text-foreground', item.bold && 'font-bold')}>
          {numberValue(item.value) == null ? '—' : item.percent ? `${numberValue(item.value)!.toFixed(2)}%` : String(item.value)}
        </dd>
        {data.previous_date && <p className="mt-1 text-[10px] text-secondary">前日 {numberValue(item.previous) == null ? '—' : item.percent ? `${numberValue(item.previous)!.toFixed(2)}%` : String(item.previous)}</p>}
        {data.datasets[item.key]?.state === 'error' && <p className="mt-1 text-[10px] text-warning">
          更新失败{numberValue(item.value) != null && ' · 显示上次'}<br />{displayTime(data.datasets[item.key].fetched_at)}
        </p>}
      </div>)}
    </dl>
    {data.previous_date && <p className="mt-3 text-[11px] text-secondary">前一来源交易日 {data.previous_date} · 未提供的数值保留为空</p>}
  </section>
}

function LiveMessages({ dataset, date, live, onOpenStock }: { dataset?: KaipanlaMarketDataset; date: string; live: boolean; onOpenStock: OpenStock }) {
  const [page, setPage] = useState(0)
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const selectedMessage = useRef<HTMLLIElement>(null)
  const rows = (dataset?.rows ?? []).flatMap((row, index) => {
    const raw = String(row.Time ?? '')
    if (!/^(\d{10}|\d{13})$/.test(raw)) return []
    const timestamp = Number(raw) * (raw.length === 10 ? 1000 : 1)
    if (!Number.isFinite(new Date(timestamp).getTime())) return []
    const published = displayTime(raw)
    return published.slice(0, 10) === date ? [{ row, timestamp, published, id: `${String(row.ID ?? timestamp)}-${index}` }] : []
  }).sort((left, right) => right.timestamp - left.timestamp)
  const pages = Math.max(1, Math.ceil(rows.length / 5))
  const currentPage = Math.min(page, pages - 1)
  const annotations = rows.flatMap<IntradayAnnotation & { messageId: string }>(item => {
    const text = typeof item.row.Comment === 'string' ? item.row.Comment : '盘面解读'
    const common = { time: item.published.slice(11, 16), label: text.length > 160 ? `${text.slice(0, 160)}…` : text, messageId: item.id }
    const highlights = extractLiveHighlights(item.row.Comment)
    return highlights.length ? highlights.map((highlight, index) => ({ ...common,
      id: `${item.id}:${index}`, caption: highlight.label, direction: highlight.direction,
    })) : [{ ...common, id: `${item.id}:message`, caption: undefined, direction: 'neutral' as const }]
  })
  useEffect(() => {
    if (selectedId) selectedMessage.current?.scrollIntoView?.({ block: 'nearest' })
  }, [selectedId, currentPage])
  const selectMessage = (id: string) => {
    const messageId = annotations.find(item => item.id === id)?.messageId
    const index = rows.findIndex(row => row.id === messageId)
    if (index < 0) return
    setPage(Math.floor(index / 5))
    setSelectedId(rows[index].id)
  }
  return <section aria-label="大盘直播" className={panelClass}>
    <div className="mb-2 flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
      <h2 className="text-sm font-semibold text-foreground">大盘直播</h2>
      <span className="text-[11px] text-secondary">{dataset ? stateLabels[dataset.state] : '暂无数据'}{dataset?.fetched_at && ` · ${displayTime(dataset.fetched_at)}`}</span>
    </div>
    <p className="text-[11px] leading-5 text-secondary">来源观点 · 开盘啦解读 · {!dataset || dataset.state === 'error' && !rows.length
      ? '直播消息暂不可用' : `共 ${rows.length} 条同日有效消息，每页 5 条`} · 关联个股涨跌幅由开盘啦返回，更新时间未提供</p>
    <LiveIndexChart date={date} live={live} annotations={annotations} onAnnotationClick={selectMessage} />
    {rows.length ? <ol className="mt-4 divide-y divide-border/60">
      {rows.slice(currentPage * 5, (currentPage + 1) * 5).map(({ row, timestamp, published, id }) => {
        const stocks = Array.isArray(row.stocks) ? row.stocks as KaipanlaLiveStock[] : []
        const items = previewItems(stocks.map(stock => ({ symbol: stock.symbol, name: stock.name })), { code: 'symbol', name: 'name' })
        return <li key={id} ref={selectedId === id ? selectedMessage : undefined} aria-current={selectedId === id ? 'true' : undefined}
          className={cn('grid min-w-0 grid-cols-[3.25rem_minmax(0,1fr)] gap-x-3 py-4 first:pt-0 sm:grid-cols-[4.5rem_minmax(0,1fr)]', selectedId === id && 'rounded-btn bg-accent/5 ring-1 ring-accent/30')}>
          <time dateTime={new Date(timestamp).toISOString()} title={published} className="pt-0.5 font-mono text-xs font-semibold tabular-nums text-blue-700 dark:text-accent">{published.slice(11, 16)}</time>
          <article className="min-w-0">
            <p className="max-w-[75ch] whitespace-pre-wrap break-words text-xs leading-6 text-foreground [overflow-wrap:anywhere]">{typeof row.Comment === 'string' && row.Comment.trim() ? row.Comment : '来源未提供解读正文。'}</p>
            {typeof row.UserName === 'string' && row.UserName.trim() && <p className="mt-1 text-[10px] text-secondary">{row.UserName}</p>}
            {stocks.length ? <ul aria-label="关联个股" className="mt-3 flex flex-wrap gap-2">
              {stocks.map(stock => {
                const target = previewStock(stock.symbol, stock.name)
                const change = numberValue(stock.change_pct)
                const percentage = change == null ? '—' : `${change > 0 ? '+' : ''}${change.toFixed(2)}%`
                const contents = <>
                  <span className="min-w-0"><span className="block break-words text-xs text-foreground">{stock.name}</span>
                    <span className="block font-mono text-[10px] text-secondary">{stock.symbol}</span></span>
                  <span className={cn('font-mono text-xs font-semibold tabular-nums', change == null || change === 0 ? 'text-secondary' : change > 0 ? 'text-red-700 dark:text-bull' : 'text-emerald-700 dark:text-bear')}>{percentage}</span></>
                const cardClass = 'flex h-full w-full min-w-0 flex-wrap items-center justify-between gap-x-3 gap-y-1 rounded-btn border border-border/70 bg-elevated/50 px-3 py-2 text-left'
                return <li key={stock.symbol} aria-label={`${stock.name} ${percentage}`} title={`${stock.kind === 'focus' ? '重点' : '讨论'}股票 · 来源涨跌幅，更新时间未提供`}
                  className="min-w-0 basis-[12rem] sm:basis-[13rem]">
                  {target ? <button type="button" aria-label={`查看${stock.name}个股详情`} onClick={() => onOpenStock(target, items)}
                    className={cn(cardClass, 'transition-colors hover:border-accent/50 hover:bg-accent/5 focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent')}>{contents}</button>
                    : <div className={cardClass}>{contents}</div>}
                </li>
              })}
            </ul> : <p className="mt-2 text-[11px] text-secondary">来源未提供关联个股。</p>}
          </article>
        </li>
      })}
    </ol> : <p className="py-6 text-center text-xs text-secondary">{dataset?.state === 'error' ? '该项获取失败，可刷新数据重试。' : '暂无可核验时间的同日直播消息。'}</p>}
    {dataset?.message && <p className="mt-3 text-[11px] text-secondary">{dataset.message}</p>}
    {rows.length > 5 && <div className="mt-4 flex flex-wrap justify-end gap-2 text-xs text-secondary">
      <span className="self-center font-mono tabular-nums">{currentPage + 1}/{pages} 页</span>
      <button type="button" className={buttonClass} disabled={currentPage === 0} onClick={() => setPage(currentPage - 1)}>上一页消息</button>
      <button type="button" className={buttonClass} disabled={currentPage + 1 >= pages} onClick={() => setPage(currentPage + 1)}>下一页消息</button>
    </div>}
  </section>
}

function EmotionHistory({ dataset, date }: { dataset: KaipanlaMarketDataset; date: string }) {
  const [metric, setMetric] = useState<'strong' | 'df_num'>('strong')
  const theme = useChartTheme()
  const rows = dataset.rows.filter(row => typeof row.trade_date === 'string' && row.trade_date <= date)
  const enough = rows.filter(row => numberValue(row[metric]) != null).length >= 2
  const title = metric === 'strong' ? '综合强度历史' : '大幅回撤家数历史'
  const points = rows.map(row => {
    const value = numberValue(row[metric])
    if (metric !== 'strong' || value == null || value >= 35 && value <= 70) return value
    const color = value > 70 ? '#F04438' : '#12B76A'
    return { value, symbol: 'circle', itemStyle: { color, borderColor: color } }
  })
  const option = {
    animation: false,
    grid: { left: 44, right: 16, top: 18, bottom: 32 },
    tooltip: { trigger: 'axis', backgroundColor: theme.tooltipBg, textStyle: { color: theme.tooltipText }, borderColor: theme.tooltipBorder },
    xAxis: { type: 'category', data: rows.map(row => row.trade_date), axisLabel: { color: theme.text, fontSize: 10 }, axisLine: { lineStyle: { color: theme.border } } },
    yAxis: { type: 'value', axisLabel: { color: theme.text, fontSize: 10 }, splitLine: { lineStyle: { color: theme.grid } } },
    series: [{ name: title, type: metric === 'strong' ? 'line' : 'bar', data: points, connectNulls: false,
      showAllSymbol: metric === 'strong', symbolSize: 4,
      itemStyle: { color: metric === 'strong' ? '#3B82F6' : '#F79009' }, lineStyle: { width: 2, color: '#3B82F6' } }],
  }
  return <section className={panelClass}>
    <div className="flex flex-wrap items-center justify-between gap-2">
      <h2 className="text-sm font-semibold text-foreground">{title}</h2>
      <div className="flex gap-1">{([['strong', '综合强度'], ['df_num', '大幅回撤']] as const).map(([key, label]) => <button key={key} type="button"
        className={cn('rounded-btn px-2 py-1 text-[11px]', metric === key ? 'bg-accent/10 text-accent' : 'text-secondary hover:bg-elevated')}
        onClick={() => setMetric(key)}>{label}</button>)}</div>
    </div>
    <p className="mt-1 text-[11px] text-secondary">开盘啦交易日序列 · 日级统计，缺失值保留空缺</p>
    {enough ? <ReactECharts option={option} style={{ height: 230 }} notMerge />
      : <p className="flex min-h-[160px] items-center justify-center text-xs text-secondary">暂无足够的真实历史数据绘图。</p>}
    {dataset.state === 'error' && <p className="text-[11px] text-warning">历史序列获取失败。</p>}
  </section>
}

function Breadth({ dataset }: { dataset?: KaipanlaMarketDataset }) {
  const row = dataset?.rows[0]
  const bins = Array.isArray(row?.bins) ? row.bins as { key: string; label: string; count: number | null }[] : []
  const maximum = Math.max(1, ...bins.map(bin => numberValue(bin.count) ?? 0))
  return <section className={panelClass}>
    <h2 className="text-sm font-semibold text-foreground">涨跌分布（直播快照）</h2>
    <p className="mt-1 text-[11px] text-secondary">快照时间 {displayTime(row?.published_at)} · 供应商分布键，0 不等同平盘，±10 不等同超过 10%</p>
    {!row ? <p className="py-6 text-center text-xs text-secondary">暂无该日完整、可核验的直播分布快照。</p> : <>
      <div className="mt-3 flex flex-wrap gap-x-5 gap-y-2 text-xs">
        <span className="text-bull">上涨 <span className="font-mono tabular-nums">{numberValue(row.up_count) ?? '—'}</span></span>
        <span className="text-bear">下跌 <span className="font-mono tabular-nums">{numberValue(row.down_count) ?? '—'}</span></span>
      </div>
      {bins.length > 0 && <div className="mt-4 overflow-x-auto"><div className="flex min-w-[720px] items-end gap-2" aria-label="来源涨跌分布">
        {bins.map(bin => <div key={bin.key} className="flex min-w-[26px] flex-1 flex-col items-center gap-1 text-[10px]">
          <span className="font-mono tabular-nums text-secondary">{bin.count ?? '—'}</span>
          <div className="flex h-20 w-full items-end"><div className={cn('w-full rounded-t-sm', Number(bin.key) > 0 ? 'bg-bull/70' : Number(bin.key) < 0 ? 'bg-bear/70' : 'bg-secondary/40')}
            style={{ height: `${((numberValue(bin.count) ?? 0) / maximum) * 100}%` }} /></div>
          <span className="whitespace-nowrap font-mono text-secondary">{bin.label}</span>
        </div>)}
      </div></div>}
    </>}
    {dataset?.message && <p className="mt-3 text-[11px] text-secondary">{dataset.message}</p>}
  </section>
}

function StockLists({ date, live, onOpenStock }: { date: string; live: boolean; onOpenStock: OpenStock }) {
  const client = useQueryClient()
  const [kind, setKind] = useState<'limit_up' | 'broken_limits'>('limit_up')
  const [page, setPage] = useState(0)
  const id = kind === 'limit_up' ? 'ext_kpl_limit_up' : 'ext_kpl_broken_limits'
  const parameters: Record<string, string | number> = kind === 'broken_limits' ? { PidType: 1 } : {}
  const query = useQuery({ queryKey: QK.kaipanlaStocks(kind, date), queryFn: () => api.kaipanlaQuery(id, date, parameters, false),
    staleTime: date === beijingToday() ? 60_000 : Infinity, refetchInterval: date === beijingToday() && live ? 60_000 : false, retry: false })
  const refresh = useMutation({
    mutationFn: async (target: { kind: 'limit_up' | 'broken_limits'; date: string }) => {
      const result = await api.kaipanlaQuery(`ext_kpl_${target.kind}`, target.date, target.kind === 'broken_limits' ? { PidType: 1 } : {}, true)
      if (result.data_date !== target.date) throw new Error('列表日期不匹配')
      return result
    },
    onSuccess: (result, target) => client.setQueryData(QK.kaipanlaStocks(target.kind, target.date), result),
  })
  const mismatch = !!(query.data && query.data.data_date !== date)
  const data = mismatch ? undefined : query.data
  const rows = data?.rows ?? []
  const pages = Math.max(1, Math.ceil(rows.length / 25))
  const displayPage = Math.min(page, pages - 1)
  const columns = STOCK_COLUMNS.filter(column => column.name === 'StockID' || column.name === 'Name'
    || rows.some(row => row[column.name] != null && row[column.name] !== ''))
  return <section className={panelClass}>
    <div className="flex flex-wrap items-center justify-between gap-3">
      <div className="flex flex-wrap gap-1" role="tablist" aria-label="股票类别">
        {([['limit_up', '涨停'], ['broken_limits', '炸板']] as const).map(([key, label]) => <button type="button" role="tab" aria-selected={kind === key}
          key={key} className={cn(buttonClass, kind === key && 'border-accent/30 bg-accent/10 text-accent')}
          onClick={() => { setKind(key); setPage(0); refresh.reset() }}>{label}</button>)}
        <button type="button" className={buttonClass} disabled title="文档未提供跌停个股列表接口">跌停</button>
        <button type="button" className={buttonClass} disabled title="竞价列表的认证要求待确认，暂未接入">竞价</button>
      </div>
      <button type="button" className={buttonClass} disabled={query.isFetching || refresh.isPending} onClick={() => refresh.mutate({ kind, date })}>刷新列表</button>
    </div>
    <p className="mt-3 text-[11px] text-secondary">{kind === 'broken_limits' ? '今日首板破板 · 仅所选来源交易日的首板口径' : '涨停名单 · 汇总来源的 1 至 5 档'} · 行情归属 {date} · 获取 {displayTime(data?.fetched_at)}</p>
    <p className="mt-1 text-[11px] text-secondary">跌停列表：文档未提供对应接口；竞价列表：认证要求待确认。</p>
    {(query.isError || refresh.isError || mismatch) && <p role="status" className="mt-3 text-xs text-warning">{mismatch ? '列表日期与所选日期不一致，未展示。' : rows.length ? '获取失败，保留上次有效列表。' : '列表获取失败，请刷新重试。'}</p>}
    {query.isLoading ? <p role="status" className="py-8 text-center text-xs text-secondary">读取股票列表…</p> : !rows.length ? <p className="py-8 text-center text-xs text-secondary">暂无该日{kind === 'limit_up' ? '涨停' : '首板破板'}名单。</p>
      : <div className="mt-4"><BusinessTable label={kind === 'limit_up' ? '涨停名单' : '今日首板破板名单'} rows={rows.slice(displayPage * 25, (displayPage + 1) * 25)} columns={columns}
        stockFields={{ code: 'StockID', name: 'Name' }} navigationRows={rows} onOpenStock={onOpenStock} /></div>}
    {rows.length > 0 && <div className="mt-3 flex items-center justify-end gap-2 text-xs text-secondary">
      <span className="font-mono tabular-nums">共 {rows.length} 只 · {displayPage + 1}/{pages} 页</span>
      <button type="button" className={buttonClass} disabled={displayPage === 0} onClick={() => setPage(displayPage - 1)}>上一页</button>
      <button type="button" className={buttonClass} disabled={displayPage + 1 >= pages} onClick={() => setPage(displayPage + 1)}>下一页</button>
    </div>}
  </section>
}

export function MarketEmotion() {
  const client = useQueryClient()
  const [date, setDate] = useState<string | undefined>()
  const [tab, setTab] = useState<'analysis' | 'stocks'>('analysis')
  const [preview, setPreview] = useState<{ stock: NavItem; items: NavItem[] } | null>(null)
  const openStock: OpenStock = (stock, items) => setPreview({ stock, items })
  const [now, setNow] = useState(Date.now)
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 60_000)
    return () => window.clearInterval(timer)
  }, [])
  const today = beijingToday(now)
  const live = date ? date === today : latestTradingWindow(now)
  const query = useQuery({
    queryKey: QK.kaipanlaMarketEmotion(date),
    queryFn: async () => preserveSameDate(client.getQueryData(QK.kaipanlaMarketEmotion(date)), await api.kaipanlaMarketEmotion(date, false)),
    staleTime: date && date !== today ? Infinity : 60_000, retry: false,
    refetchInterval: live ? 60_000 : false,
  })
  const refresh = useMutation({
    mutationFn: async (targetDate?: string) => {
      const result = await api.kaipanlaMarketEmotion(targetDate, true)
      if (targetDate && result.date !== targetDate) throw new Error('日期不匹配')
      return result
    },
    onSuccess: (result, targetDate) => {
      client.setQueryData(QK.kaipanlaMarketEmotion(targetDate), preserveSameDate(client.getQueryData(QK.kaipanlaMarketEmotion(targetDate)), result))
      if (result.date) void client.invalidateQueries({ queryKey: QK.indexMinute('000001.SH', result.date) })
    },
  })
  const mismatch = !!(date && query.data?.date && query.data.date !== date)
  const data = mismatch ? undefined : query.data
  useEffect(() => setPreview(null), [date, data?.date, tab])
  const refreshError = refresh.isError && refresh.variables === date
  const sameDate = (key: string) => {
    const dataset = data?.datasets[key]
    return dataset?.date === data?.date ? dataset : undefined
  }
  const performance = sameDate('limit_performance')
  const withdrawal = sameDate('withdrawal')
  const chooseDate = (value?: string) => { setDate(value || undefined); refresh.reset() }
  return <>
    <PageHeader title="市场情绪" titleExtra={<Activity className="h-4 w-4 text-accent" />} className="flex-wrap pl-14 md:pl-5" />
    <div className="space-y-4 bg-base px-4 py-4 sm:px-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-xs text-secondary">日期</span>
          <DatePicker value={date ?? data?.date ?? ''} max={today} onChange={chooseDate} />
          <button type="button" className={buttonClass} onClick={() => chooseDate()}>最新交易日</button>
        </div>
        <button type="button" className={buttonClass} disabled={query.isLoading || refresh.isPending} onClick={() => refresh.mutate(date)}>
          <RefreshCw className={cn('h-3.5 w-3.5', refresh.isPending && 'animate-spin motion-reduce:animate-none')} />{refresh.isPending ? '刷新中…' : '刷新数据'}
        </button>
      </div>
      <div className="flex flex-wrap items-baseline gap-x-4 gap-y-1 text-xs text-secondary">
        <span>情绪及直播来源：开盘啦 · 指数分时与个股详情沿用系统数据源</span><span>行情归属 {data?.date ?? date ?? '待确认'}</span>
        <span>采集时间（北京时间）{displayTime(data?.fetched_at)}</span>
        {data && <span className={cn(data.state === 'partial' || data.state === 'error' ? 'text-warning' : 'text-secondary')}>{stateLabels[data.state]}</span>}
      </div>
      {data && Object.values(data.datasets).some(dataset => dataset.date_origin === 'request_parameter')
        && <p className="text-[11px] text-secondary">部分日期按请求参数归属，来源未返回日期。</p>}
      {!date && data?.state === 'error' && <p role="status" className="text-xs text-warning">{data.date
        ? `最新来源交易日尚未确认，正在显示上次有效快照：${data.date}。`
        : '无法确认最新来源交易日，请刷新或选择日期。'}</p>}
      {data?.date && data.date !== today && <p className="text-[11px] text-secondary">{date ? '历史快照' : '最近来源交易日快照'} · 后续取得的盘后或补充数据，不能视为当时盘中已知。</p>}
      {(query.isError || refreshError || mismatch) && <p role="status" className="rounded-btn border border-warning/30 bg-warning/5 px-3 py-2 text-xs text-warning">
        {mismatch ? '数据日期与所选日期不一致，未展示。' : data ? '刷新失败，保留上次有效快照。' : '市场数据读取失败，请刷新重试。'}
      </p>}
      <div className="flex gap-1 border-b border-border pb-2" role="tablist" aria-label="市场情绪视图">
        {([['analysis', '数据分析'], ['stocks', '股票列表']] as const).map(([key, label]) => <button key={key} type="button" role="tab" aria-selected={tab === key}
          onClick={() => setTab(key)} className={cn('rounded-btn px-4 py-2 text-xs font-medium', tab === key ? 'bg-accent/10 text-accent' : 'text-secondary hover:bg-elevated')}>{label}</button>)}
      </div>
      {query.isLoading && <div role="status" className={panelClass}><p className="text-xs text-secondary">读取开盘啦市场数据…</p><div aria-hidden="true" className="mt-4 h-20 animate-pulse rounded bg-elevated motion-reduce:animate-none" /></div>}
      {!query.isLoading && (!data || !data.date) && !query.isError && !mismatch && <div className={panelClass}><p className="py-8 text-center text-xs text-secondary">暂无可核验的该日市场数据。可选择其他交易日或刷新数据。</p></div>}
      {data?.date && tab === 'analysis' && <div role="tabpanel" className="space-y-4">
        <IndexSnapshots dataset={sameDate('indices')} />
        <CoreStats data={{ ...data, datasets: Object.fromEntries(Object.entries(data.datasets).filter(([key, dataset]) => dataset.date === data.date || key.startsWith('previous_') && dataset.date === data.previous_date)) }} />
        <EmotionHistory dataset={data.history} date={data.date} />
        <MarketCapacity dataset={sameDate('capacity')} />
        <Breadth dataset={sameDate('breadth')} />
        <LimitPanels ladder={sameDate('limit_ladder_counts')} performance={performance} />
        <DatasetSection dataset={performance} title="昨日涨停溢价" columns={performance?.columns.filter(column => /^yesterday_/.test(column.name))} hint="所选日期接口中的昨日涨停、连板、破板表现。" />
        <DatasetSection dataset={withdrawal} title="大幅回撤名单" stockFields={{ code: 'StockID', name: 'Name' }} onOpenStock={openStock} hint={withdrawal && withdrawal.state !== 'error'
          ? `下表为回撤接口实际返回的 ${withdrawal.rows.length} 只名单；上方家数来自情绪统计，两者口径未声明一致。`
          : '名单暂不可用；上方家数来自情绪统计，两者口径未声明一致。'} />
        <DatasetSection dataset={sameDate('weight')} title="权重表现" stockFields={{ code: 'leader_id', name: 'leader_name' }} onOpenStock={openStock} />
        <LiveMessages key={data.date} dataset={sameDate('live')} date={data.date} live={data.date === today && latestTradingWindow(now)} onOpenStock={openStock} />
      </div>}
      {data?.date && tab === 'stocks' && <div role="tabpanel"><StockLists key={data.date} date={data.date} live={live} onOpenStock={openStock} /></div>}
      {data && <section className={panelClass}>
        <h2 className="text-sm font-semibold text-foreground">数据覆盖与缺失</h2>
        <ul className="mt-3 space-y-2 text-xs text-secondary">{data.missing.map(item => <li key={item.id}><span className="font-medium text-foreground">{item.id === 'index_intraday' ? '开盘啦指数分时接口' : item.label}</span> · {item.reason}{item.id === 'index_intraday' && ' 大盘直播分时已改用系统配置的分钟数据源，实际覆盖见图表状态。'}</li>)}</ul>
      </section>}
    </div>
    {preview && <StockPreviewDialog symbol={preview.stock.symbol} name={preview.stock.name} navList={preview.items}
      onClose={() => setPreview(null)} onNavigate={(symbol, name) => setPreview(current => current ? { ...current, stock: { symbol, name } } : null)} />}
  </>
}
