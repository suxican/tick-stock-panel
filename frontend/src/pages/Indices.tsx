import { useEffect, useMemo, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Activity, Loader2, RefreshCw } from 'lucide-react'
import { api, type CatalogIndex, type IndexQuote, type KlineRow, type MinuteKlineRow } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { useCapabilityMatrix } from '@/lib/useSharedQueries'
import type { OHLC } from '@/lib/chart-types'
import { LightweightDaily } from '@/components/charts/LightweightDaily'
import { LightweightIntraday } from '@/components/charts/LightweightIntraday'

function defaultRange() {
  const end = new Date().toLocaleDateString('en-CA', { timeZone: 'Asia/Shanghai' })
  const s = new Date(`${end}T00:00:00Z`)
  s.setUTCMonth(s.getUTCMonth() - 6)
  return { start: s.toISOString().slice(0, 10), end }
}

function toOHLC(rows: KlineRow[]): OHLC[] {
  return rows
    .filter(r => r?.date != null && r.open != null && r.high != null && r.low != null && r.close != null)
    .map(r => ({
      date: typeof r.date === 'string' ? r.date.slice(0, 10) : String(r.date),
      open: Number(r.open),
      high: Number(r.high),
      low: Number(r.low),
      close: Number(r.close),
      volume: r.volume != null ? Number(r.volume) : undefined,
      ma5: r.ma5 != null ? Number(r.ma5) : null,
      ma10: r.ma10 != null ? Number(r.ma10) : null,
      ma20: r.ma20 != null ? Number(r.ma20) : null,
      ma60: r.ma60 != null ? Number(r.ma60) : null,
      macd_dif: r.macd_dif != null ? Number(r.macd_dif) : null,
      macd_dea: r.macd_dea != null ? Number(r.macd_dea) : null,
      macd_hist: r.macd_hist != null ? Number(r.macd_hist) : null,
      rsi_6: r.rsi_6 != null ? Number(r.rsi_6) : null,
      rsi_14: r.rsi_14 != null ? Number(r.rsi_14) : null,
      rsi_24: r.rsi_24 != null ? Number(r.rsi_24) : null,
      kdj_k: r.kdj_k != null ? Number(r.kdj_k) : null,
      kdj_d: r.kdj_d != null ? Number(r.kdj_d) : null,
      kdj_j: r.kdj_j != null ? Number(r.kdj_j) : null,
      boll_upper: r.boll_upper != null ? Number(r.boll_upper) : null,
      boll_lower: r.boll_lower != null ? Number(r.boll_lower) : null,
    }))
}

function fmtPct(v: number | null | undefined) {
  if (v == null || Number.isNaN(Number(v))) return '--'
  return `${Number(v).toFixed(2)}%`
}

function fmtNum(v: number | null | undefined, digits = 2) {
  if (v == null || Number.isNaN(Number(v))) return '--'
  return Number(v).toFixed(digits)
}

const MARKET_NAMES = { CN: 'A股', US: '美股', JP: '日本', KR: '韩国' } as const

function pctColor(value?: number | null) {
  if (!value) return 'text-muted'
  return value > 0 ? 'text-bull' : 'text-bear'
}

function quoteTime(quote: IndexQuote | undefined, overseas: boolean) {
  if (!quote) return ''
  if (overseas) return quote.date ? `当地交易日 ${quote.date}` : ''
  if (quote.snapshot_time) return new Date(quote.snapshot_time).toLocaleString('zh-CN', {
    timeZone: 'Asia/Shanghai', hour12: false,
  })
  if (quote.source === 'index_daily') return `${quote.date ?? ''} 收盘`.trim()
  if (quote.timestamp) return new Date(quote.timestamp).toLocaleString('zh-CN', {
    timeZone: 'Asia/Shanghai', hour12: false,
  })
  return ''
}

export function Indices() {
  const qc = useQueryClient()
  const [searchParams, setSearchParams] = useSearchParams()
  const symbolParam = searchParams.get('symbol') ?? ''
  const [range, setRange] = useState(defaultRange)
  const [selectedDate, setSelectedDate] = useState<string | null>(null)
  const [linkedPrice, setLinkedPrice] = useState<number | null>(null)

  const catalog = useQuery({ queryKey: QK.indexCatalog, queryFn: api.indexCatalog, staleTime: Infinity })
  const items = catalog.data?.rows ?? []
  const selectedSymbol = symbolParam || items[0]?.symbol || ''
  const selectedItem = items.find(item => item.symbol === selectedSymbol)
  const isOverseas = !!selectedItem && selectedItem.market !== 'CN'
  const isThsIndex = selectedItem?.capability === 'ths_index'
  const matrix = useCapabilityMatrix()
  const hasMinuteCap = !!matrix.data?.capabilities.find(cap => cap.id === 'minute')?.usable
  const overseasRoute = matrix.data?.capabilities.find(cap => cap.id === 'overseas')
  const thsRoute = matrix.data?.capabilities.find(cap => cap.id === 'ths_index')
  const selectIndex = (symbol: string) => setSearchParams({ symbol })
  const groups = (Object.keys(MARKET_NAMES) as Array<keyof typeof MARKET_NAMES>).map(market => ({
    market, label: MARKET_NAMES[market], items: items.filter(item => item.market === market),
  })).filter(group => group.items.length > 0)
  const domesticSymbols = items.filter(item => item.market === 'CN' && !item.capability).map(item => item.symbol)
  // 监控页可深链到目录外的指数。
  if (catalog.data && selectedSymbol && !selectedItem) domesticSymbols.push(selectedSymbol)

  const quotes = useQuery({
    queryKey: QK.indexCatalogQuotes(domesticSymbols),
    queryFn: () => api.indexQuotes(domesticSymbols),
    enabled: domesticSymbols.length > 0,
  })
  const overseasQuotes = useQuery({
    queryKey: QK.overseasIndexQuotes(overseasRoute?.effective),
    queryFn: api.overseasIndexQuotes,
    enabled: !!overseasRoute?.usable && items.some(item => item.market !== 'CN'),
    staleTime: 60_000,
    refetchInterval: 60_000,
  })
  const thsQuotes = useQuery({
    queryKey: QK.thsIndexQuotes(thsRoute?.effective),
    queryFn: async () => {
      const result = await api.thsIndexQuotes()
      if (!result.rows.length) throw new Error(result.reason || '本次未取得指数报价')
      return result
    },
    enabled: !!thsRoute?.usable && items.some(item => item.capability === 'ths_index'),
    staleTime: 10_000,
    refetchInterval: 10_000,
    retry: false,
  })

  const daily = useQuery({
    queryKey: isThsIndex
      ? QK.thsIndexDaily(thsRoute?.effective, selectedSymbol, range.start, range.end)
      : isOverseas
        ? QK.overseasIndexDaily(overseasRoute?.effective, selectedSymbol, range.start, range.end)
        : QK.indexDaily(selectedSymbol, range.start, range.end),
    queryFn: async () => {
      const result = await api.indexDaily(selectedSymbol, 180, range)
      if (isThsIndex && result.unavailable) throw new Error(result.reason || '本次未取得指数日K')
      return result
    },
    enabled: !!catalog.data && !!selectedSymbol && (!isOverseas || !!overseasRoute?.usable)
      && (!isThsIndex || !!thsRoute?.usable),
    refetchInterval: isThsIndex ? 60_000 : false,
  })

  const minute = useQuery({
    queryKey: QK.indexMinute(selectedSymbol, selectedDate ?? ''),
    queryFn: () => api.indexMinute(selectedSymbol, selectedDate ?? undefined),
    enabled: !isOverseas && !isThsIndex && daily.data?.symbol === selectedSymbol && !!selectedDate && hasMinuteCap,
  })

  const syncDaily = useMutation({
    mutationFn: () => api.syncIndexDaily(365),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: QK.indexQuotes })
      qc.invalidateQueries({ queryKey: ['index-daily'] })
    },
  })

  const quoteBySymbol = useMemo(() => {
    const m = new Map<string, IndexQuote>()
    for (const q of quotes.data?.rows ?? []) m.set(q.symbol, q)
    for (const q of overseasQuotes.data?.rows ?? []) m.set(q.symbol, q)
    for (const q of thsQuotes.data?.rows ?? []) m.set(q.symbol, q)
    return m
  }, [quotes.data?.rows, overseasQuotes.data?.rows, thsQuotes.data?.rows])
  const selectedQuote = quoteBySymbol.get(selectedSymbol)
  const selectedQuoteValue = selectedQuote?.last_price ?? selectedQuote?.price ?? selectedQuote?.close
  const selectedQuotePct = selectedQuote?.change_pct ?? selectedQuote?.pct

  const chartRows = useMemo(() => toOHLC(daily.data?.rows ?? []), [daily.data?.rows])
  const activeIndicators = useMemo(() => {
    if (!isOverseas && !isThsIndex) return ['vol', 'macd']
    return chartRows.every(row => row.volume != null) && chartRows.some(row => (row.volume ?? 0) > 0) ? ['vol'] : []
  }, [chartRows, isOverseas, isThsIndex])
  const selectedInfo = selectedItem || daily.data?.index_info
  const minuteRows: MinuteKlineRow[] = minute.data?.symbol === selectedSymbol
    && minute.data?.date === selectedDate && daily.data?.symbol === selectedSymbol
    ? minute.data.rows : []
  const selectedIdx = selectedDate ? chartRows.findIndex(r => r.date === selectedDate) : -1
  const prevClose = selectedIdx > 0 ? chartRows[selectedIdx - 1].close : undefined

  useEffect(() => {
    setSelectedDate(null)
    setLinkedPrice(null)
  }, [selectedSymbol])

  useEffect(() => {
    if ((!selectedDate || !chartRows.some(r => r.date === selectedDate)) && chartRows.length > 0 && daily.data?.symbol === selectedSymbol) {
      setSelectedDate(chartRows[chartRows.length - 1].date)
    }
  }, [chartRows, daily.data?.symbol, selectedDate, selectedSymbol])
  const renderIndexItem = (item: CatalogIndex) => {
    const q = quoteBySymbol.get(item.symbol)
    const pct = q?.change_pct ?? q?.pct
    const current = q?.last_price ?? q?.price ?? q?.close
    const active = item.symbol === selectedSymbol
    return (
      <button
        key={item.symbol}
        aria-pressed={active}
        title={quoteTime(q, item.market !== 'CN') || undefined}
        onClick={() => selectIndex(item.symbol)}
        className={`w-full rounded-btn px-2 py-2 text-left transition-colors ${active ? 'bg-accent/15 text-foreground' : 'hover:bg-elevated text-secondary'}`}
      >
        <div className="flex items-center justify-between gap-2">
          <span className="truncate text-xs font-medium">{item.name || item.symbol}</span>
          <span className={`text-[10px] font-mono ${pctColor(pct)}`}>{fmtPct(pct)}</span>
        </div>
        <div className="mt-0.5 flex items-center justify-between text-[10px] font-mono text-muted">
          <span>{item.symbol}</span>
          <span>{fmtNum(current)}</span>
        </div>
      </button>
    )
  }

  return (
    <div className="h-full overflow-auto bg-base p-4">
      <div className="mb-4 flex items-center justify-between gap-3">
        <h1 className="pl-10 text-lg font-semibold text-foreground md:pl-0">指数</h1>
        {catalog.data && !isOverseas && !isThsIndex && <div className="flex flex-wrap items-center justify-end gap-2">
          {syncDaily.isError && <span role="alert" className="text-xs text-danger">同步失败，请检查数据源后重试</span>}
          <button
            onClick={() => syncDaily.mutate()}
            disabled={syncDaily.isPending}
            className="inline-flex items-center gap-1.5 rounded-btn bg-accent px-3 py-1.5 text-xs font-medium text-white hover:bg-accent/90 disabled:opacity-50"
          >
            {syncDaily.isPending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RefreshCw className="h-3.5 w-3.5" />}
            同步指数日K
          </button>
        </div>}
      </div>

      {catalog.isLoading && <p role="status" className="text-sm text-muted">指数列表加载中…</p>}
      {catalog.isError && <p role="alert" className="text-sm text-secondary">
        指数列表加载失败 <button className="text-accent hover:underline" onClick={() => void catalog.refetch()}>重试</button>
      </p>}
      {catalog.data && !selectedSymbol && <p className="text-sm text-muted">暂无可选指数</p>}
      {catalog.data && selectedSymbol && <div className="grid min-w-0 grid-cols-1 gap-3 lg:grid-cols-[15rem_minmax(0,1fr)] lg:gap-4">
        <select aria-label="选择指数" value={selectedSymbol} onChange={e => selectIndex(e.target.value)}
          className="w-full rounded-btn border border-border bg-surface px-3 py-2 text-sm text-foreground focus:border-accent lg:hidden">
          {!selectedItem && <option value={selectedSymbol}>{selectedInfo?.name || selectedSymbol}</option>}
          {groups.map(group => <optgroup key={group.market} label={group.label}>
            {group.items.map(item => <option key={item.symbol} value={item.symbol}>{item.name} · {item.symbol}</option>)}
          </optgroup>)}
        </select>
        <aside aria-label="指数列表" className="hidden max-h-[80vh] self-start overflow-y-auto rounded-card border border-border bg-surface p-3 lg:block">
          {groups.map(group => <section key={group.market} aria-label={group.label} className="mb-3 last:mb-0">
            <h3 className="mb-1 px-2 text-xs font-medium text-muted">{group.label}</h3>
            <div className="space-y-0.5">{group.items.map(renderIndexItem)}</div>
          </section>)}
        </aside>

        <main className="min-w-0 rounded-card border border-border bg-surface p-3">
          <div className="mb-3 flex flex-wrap items-start justify-between gap-3">
            <div className="min-w-0">
              <div className="flex flex-wrap items-center gap-2">
                <Activity className="h-4 w-4 text-accent" />
                <h2 className="truncate text-sm font-semibold text-foreground">
                  {selectedInfo?.name || selectedSymbol || '未选择指数'}
                </h2>
                <span className="font-mono text-xs text-muted">{selectedSymbol}</span>
                <span className="font-mono text-xs text-foreground">{fmtNum(selectedQuoteValue)}</span>
                <span className={`font-mono text-xs ${pctColor(selectedQuotePct)}`}>{fmtPct(selectedQuotePct)}</span>
              </div>
              <div className="mt-1 text-xs text-muted">
                {isThsIndex ? <>
                  {thsRoute?.effective_display || thsRoute?.effective || '同花顺指数行情'}
                  {selectedQuote && ` · 快照获取时间 ${quoteTime(selectedQuote, false) || '未提供'}`}
                  {thsQuotes.isLoading && ' · 行情加载中…'}
                  {thsQuotes.isError && (selectedQuote ? ' · 更新失败，显示上次报价' : ` · ${thsQuotes.error.message}`)}
                  {!thsQuotes.isLoading && !thsQuotes.isError && !selectedQuote && ' · 暂无报价'}
                  {!matrix.isLoading && !thsRoute?.usable && <Link to="/settings?tab=data-sources" className="ml-1 text-accent hover:underline">配置同花顺指数数据源</Link>}
                </> : selectedQuote ? [isOverseas ? '海外行情（可能延迟）' : '', quoteTime(selectedQuote, isOverseas)].filter(Boolean).join(' · ')
                  : (isOverseas ? overseasQuotes : quotes).isLoading ? '行情加载中…' : '暂无报价'}
              </div>
            </div>
            <div className="flex max-w-full flex-wrap items-center gap-2 text-xs">
              <input
                type="date"
                aria-label="开始日期"
                value={range.start}
                onChange={e => setRange(r => ({ ...r, start: e.target.value }))}
                className="rounded-btn border border-border bg-base px-2 py-1 text-secondary outline-none focus:border-accent"
              />
              <span className="text-muted">至</span>
              <input
                type="date"
                aria-label="结束日期"
                value={range.end}
                onChange={e => setRange(r => ({ ...r, end: e.target.value }))}
                className="rounded-btn border border-border bg-base px-2 py-1 text-secondary outline-none focus:border-accent"
              />
            </div>
          </div>

          {(isOverseas || isThsIndex) && matrix.isLoading && <div className="py-10 text-center text-sm text-muted">行情数据源加载中…</div>}
          {daily.isLoading && <div className="py-10 text-center text-sm text-muted">日K加载中…</div>}
          {daily.isError && <div role="status" className="py-4 text-sm text-danger">
            {isThsIndex && chartRows.length > 0 ? '日K更新失败，显示上次数据' : '指数日K加载失败'}
          </div>}
          {!daily.isLoading && !daily.isError && !((isOverseas || isThsIndex) && matrix.isLoading) && chartRows.length === 0 && (
            <div className="rounded-card bg-elevated p-6 text-center text-sm text-muted">
              {isThsIndex ? <>
                {daily.data?.reason || '暂无日K数据。'}
                <Link to="/settings?tab=data-sources" className="ml-1 text-accent hover:underline">检查同花顺指数数据源</Link>
              </> : isOverseas ? <>
                {daily.data?.reason || '暂无日K数据。'}
                <Link to="/settings?tab=data-sources" className="ml-1 text-accent hover:underline">检查海外行情数据源</Link>
              </> : '暂无日K数据。可以先同步指数日K，或选择其他指数。'}
            </div>
          )}
          {chartRows.length > 0 && (
            <div className={`grid min-w-0 gap-3 ${!isOverseas && !isThsIndex && hasMinuteCap ? 'xl:grid-cols-2' : ''}`}>
              <div className="min-w-0">
                <LightweightDaily
                  data={chartRows}
                  height={620}
                  showMA={true}
                  showInfoBar={true}
                  symbol={selectedSymbol}
                  linkedPrice={linkedPrice}
                  onDateClick={(date) => {
                    setSelectedDate(date)
                    setLinkedPrice(null)
                  }}
                  visibleBars={48}
                  activeIndicators={activeIndicators}
                />
              </div>
              {!isOverseas && !isThsIndex && hasMinuteCap && <div className="min-w-0 border-t border-border pt-3 xl:border-l xl:border-t-0 xl:pl-3 xl:pt-0">
                {minute.isLoading && <div className="py-2 text-xs text-muted">分时加载中…</div>}
                {!minute.isLoading && minuteRows.length === 0 && (
                  <div className="flex h-full items-center justify-center text-xs text-muted">暂无分时数据</div>
                )}
                {minuteRows.length > 0 && (
                  <LightweightIntraday
                    key={`${selectedSymbol}:${selectedDate}`}
                    symbol={selectedSymbol}
                    assetType="index"
                    data={minuteRows}
                    height={620}
                    prevClose={prevClose}
                    date={selectedDate ?? undefined}
                    showLimitLines={false}
                    showAvgLine={false}
                    onPriceHover={setLinkedPrice}
                  />
                )}
              </div>}
              {!isOverseas && !isThsIndex && !hasMinuteCap && !matrix.isLoading && <p className="text-xs text-muted">
                指数分时数据不可用。<Link to="/settings?tab=data-sources" className="text-accent hover:underline">检查分钟行情数据源</Link>
              </p>}
            </div>
          )}
        </main>
      </div>}
    </div>
  )
}
