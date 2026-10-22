import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { RefreshCw } from 'lucide-react'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { useCapabilityMatrix } from '@/lib/useSharedQueries'
import { FULL_DAY_TIMES, formatMinuteTime } from '@/lib/intraday-chart'
import { cn } from '@/lib/cn'
import { EChartsIntraday, type IntradayAnnotation } from '@/components/EChartsIntraday'

const SYMBOL = '000001.SH'
const sessionTimes = new Set(FULL_DAY_TIMES)
const validPrice = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value) && value > 0
function offsetDay(date: string, offset: number): string {
  const day = new Date(`${date}T00:00:00Z`)
  day.setUTCDate(day.getUTCDate() + offset)
  return day.toISOString().slice(0, 10)
}

export function LiveIndexChart({ date, live, annotations, onAnnotationClick }: {
  date: string
  live: boolean
  annotations: IntradayAnnotation[]
  onAnnotationClick?: (id: string) => void
}) {
  const [showCaptions, setShowCaptions] = useState(true)
  const matrix = useCapabilityMatrix()
  const route = matrix.data?.capabilities.find(item => item.id === 'minute')
  const usable = route?.usable === true
  const range = { start: offsetDay(date, -60), end: offsetDay(date, -1) }
  const minute = useQuery({
    queryKey: QK.indexMinute(SYMBOL, date, route?.effective ?? ''),
    queryFn: async () => {
      const result = await api.indexMinute(SYMBOL, date)
      if (result.symbol !== SYMBOL || result.date !== date) throw new Error('指数或日期不匹配')
      return { ...result, rows: result.rows.filter(row =>
        row.datetime.slice(0, 10) === date
        && /^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(\.\d+)?$/.test(row.datetime)
        && sessionTimes.has(formatMinuteTime(row.datetime))
        && validPrice(row.close) && validPrice(row.high) && validPrice(row.low)
        && Number.isFinite(row.volume) && row.volume >= 0,
      ).sort((a, b) => a.datetime.localeCompare(b.datetime)) }
    },
    enabled: usable, retry: false,
    staleTime: live ? 30_000 : Infinity,
    // 指数分钟接口不落盘，也不在行情 SSE 的刷新范围内。
    refetchInterval: live && usable ? 60_000 : false,
  })
  const daily = useQuery({
    queryKey: QK.indexDaily(SYMBOL, range.start, range.end),
    queryFn: () => api.indexDaily(SYMBOL, 120, range),
    enabled: usable, retry: false, staleTime: Infinity,
  })
  const prior = daily.data?.symbol === SYMBOL ? daily.data.rows
    .filter(row => /^\d{4}-\d{2}-\d{2}$/.test(row.date) && row.date < date)
    .sort((a, b) => b.date.localeCompare(a.date))[0] : undefined
  const prevClose = validPrice(prior?.close) ? prior.close : undefined
  const rows = usable ? minute.data?.rows ?? [] : []
  const latest = rows.at(-1)
  const change = latest && prevClose != null ? (latest.close / prevClose - 1) * 100 : null
  const matched = annotations.filter(item => rows.some(row => formatMinuteTime(row.datetime) === item.time))
  const highlights = matched.filter(item => item.caption && (item.direction === 'up' || item.direction === 'down'))
  const chartAnnotations = showCaptions ? annotations : annotations.map(item => ({ ...item, caption: undefined }))
  return <div aria-label="大盘直播指数分时" className="mt-4 min-w-0 border-y border-border/60 py-4">
    <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
      <div className="flex min-w-0 flex-wrap items-baseline gap-x-3 gap-y-1">
        <h3 className="text-xs font-semibold text-foreground">上证指数 <span className="ml-1 font-mono text-[10px] font-normal text-secondary">000001.SH</span></h3>
        {latest && <span className={cn('font-mono text-lg font-semibold tabular-nums', change == null || change === 0 ? 'text-foreground' : change > 0 ? 'text-red-700 dark:text-bull' : 'text-emerald-700 dark:text-bear')}>
          {latest.close.toFixed(2)}{change != null && <span className="ml-2 text-xs">{change > 0 ? '+' : ''}{change.toFixed(2)}%</span>}
        </span>}
      </div>
      <button type="button" disabled={minute.isFetching || daily.isFetching || matrix.isFetching} onClick={() => {
        void matrix.refetch()
        if (usable) { void minute.refetch(); void daily.refetch() }
      }} className="inline-flex items-center gap-1.5 rounded-btn border border-border px-2 py-1 text-[11px] text-secondary hover:text-foreground focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent disabled:opacity-50">
        <RefreshCw className={cn('h-3 w-3', minute.isFetching && 'animate-spin motion-reduce:animate-none')} />刷新分时
      </button>
    </div>
    <p className="mb-3 text-[11px] leading-5 text-secondary">
      {date} · {live ? '盘中每分钟更新' : '历史分时 / 收盘数据'} · 分钟源：{route?.effective_display ?? '待确认'}
      {latest && ` · 最新点 ${formatMinuteTime(latest.datetime)}`} · 成交量单位：手，成交额单位：元
    </p>
    {matrix.isLoading ? <p role="status" className="py-8 text-center text-xs text-secondary">正在确认分钟数据源…</p>
      : !route || matrix.isError && !matrix.data ? <p role="status" className="py-8 text-center text-xs text-secondary">分钟数据源状态读取失败，请刷新重试。</p>
        : !usable ? <p className="py-8 text-center text-xs text-secondary">当前数据源不支持分钟数据或尚未就绪。<a href="/settings?tab=data-sources" className="ml-1 text-accent underline">配置数据源</a></p>
          : rows.length ? <>
            <div className="mb-3 flex flex-wrap items-center justify-between gap-2 text-[11px]">
              <div className="flex flex-wrap gap-x-3 gap-y-1" aria-label="直播标注图例">
                <span className="text-red-700 dark:text-bull">● 走强 / 修复</span>
                <span className="text-emerald-700 dark:text-bear">● 走弱 / 回落</span>
                <span className="text-secondary">● 其他消息</span>
              </div>
              {highlights.length > 0 && <button type="button" aria-pressed={showCaptions} onClick={() => setShowCaptions(value => !value)}
                className="rounded-btn border border-border px-2 py-1 text-secondary hover:text-foreground focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent">
                {showCaptions ? '隐藏文字标注' : '显示文字标注'}
              </button>}
            </div>
            <EChartsIntraday data={rows} date={date} prevClose={prevClose} height={400}
              showLimitLines={false} showAvgLine={false} connectNulls={false} annotations={chartAnnotations} onAnnotationClick={onAnnotationClick} />
            <p className="mt-2 text-[11px] leading-5 text-secondary">{matched.length ? '标注摘自开盘啦直播原文，悬停查看摘要，点击定位解读与关联个股。密集文字自动避让，全部标注可在下方查看。' : '暂无与有效分钟点匹配的直播消息。'} 缺失分钟保留空缺。</p>
            {highlights.length > 0 && <details className="mt-3 text-[11px]">
              <summary className="w-fit cursor-pointer rounded-btn text-secondary focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent">全部强弱标注（{highlights.length}）</summary>
              <ul className="mt-2 flex flex-wrap gap-2" aria-label="全部强弱标注">
                {highlights.map(item => <li key={item.id} className="min-w-0 max-w-full">
                  <button type="button" onClick={() => onAnnotationClick?.(item.id)} className={cn('max-w-full rounded-btn border border-border/70 px-2 py-1.5 text-left leading-5 hover:bg-elevated focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent',
                    item.direction === 'up' ? 'text-red-700 dark:text-bull' : 'text-emerald-700 dark:text-bear')}>
                    <span className="mr-2 font-mono tabular-nums">{item.time}</span><span className="break-words [overflow-wrap:anywhere]">{item.caption}</span>
                  </button>
                </li>)}
              </ul>
            </details>}
            {prevClose == null && <p className="mt-1 text-[11px] text-secondary">昨收暂不可用，仅显示指数点位。</p>}
          </> : <p role="status" className="py-8 text-center text-xs text-secondary">{minute.isLoading ? '正在读取指数分时…' : minute.isError ? '分时读取失败，可点击刷新分时重试。' : '暂无该日指数分钟数据，数据源可能不覆盖该日期。'}</p>}
    {usable && minute.isError && rows.length > 0 && <p role="status" className="mt-2 text-xs text-warning">刷新失败，保留上次有效分时。</p>}
  </div>
}
