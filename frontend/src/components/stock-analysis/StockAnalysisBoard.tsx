import { useQuery } from '@tanstack/react-query'
import { AlertTriangle, LineChart, Loader2 } from 'lucide-react'
import { EmptyState } from '@/components/EmptyState'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { AnalysisKChart } from './AnalysisKChart'

export function StockAnalysisBoard({ symbol, height = 480 }: { symbol: string; height?: number }) {
  const kline = useQuery({
    // Keep the shared, unbounded daily key so existing quote updates still apply.
    queryKey: QK.kline(symbol, '', ''),
    queryFn: () => api.klineDaily(symbol, 250),
    enabled: !!symbol,
    staleTime: 60_000,
  })
  const levelsQ = useQuery({
    queryKey: QK.stockLevels(symbol, 250),
    queryFn: () => api.stockAnalysisLevels(symbol, 250),
    enabled: !!symbol,
    staleTime: 60_000,
  })

  if (kline.isLoading) {
    return <div role="status" className="flex items-center justify-center gap-2 py-20 text-sm text-muted">
      <Loader2 className="h-4 w-4 animate-spin" />正在加载日 K 数据…
    </div>
  }

  if (kline.isError && !kline.data) {
    return <div className="pb-6 text-center">
      <EmptyState icon={AlertTriangle} title="日 K 数据加载失败" hint="请检查网络或数据源配置后重试。" />
      <RetryButton onClick={() => kline.refetch()} disabled={kline.isFetching} />
    </div>
  }

  const rows = kline.data?.rows ?? []
  if (rows.length === 0) {
    return <EmptyState icon={LineChart} title="暂无日 K 数据" hint="该标的尚未同步日 K，请先在数据页或自选页同步。" />
  }

  const levels = levelsQ.data?.levels
  const hasLevels = levels && Object.values(levels).some(points => points.length > 0)
  const last = rows[rows.length - 1]
  const prev = rows[rows.length - 2]
  const curClose = last.close
  const isUp = prev ? last.close >= prev.close : last.close >= last.open

  return (
    <div className="rounded-card border border-border/60 bg-surface/40 overflow-hidden">
      <div className="px-4 py-3 border-b border-border/40 flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2 min-w-0">
          <LineChart className="h-4 w-4 text-sky-400 shrink-0" />
          <span className="text-sm font-medium text-foreground">关键价位分析</span>
        </div>
        <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1 text-[10px] text-muted">
          <span>{last.date} · {rows.length} 个交易日</span>
          <span>当前价</span>
          <span className={`text-[16px] leading-6 font-mono font-bold ${isUp ? 'text-bull' : 'text-bear'}`}>
            {curClose.toFixed(2)}
          </span>
        </div>
      </div>
      <div className="p-3">
        {kline.isError && <div role="status" className="mb-3 flex flex-wrap items-center gap-2 text-xs text-warning">
          日 K 更新失败，当前显示上次数据。
          <RetryButton onClick={() => kline.refetch()} disabled={kline.isFetching} />
        </div>}
        {levelsQ.isLoading ? (
          <div role="status" className="mb-3 flex items-center gap-2 text-xs text-muted">
            <Loader2 className="h-3.5 w-3.5 animate-spin" />正在加载关键价位…
          </div>
        ) : levelsQ.isError ? (
          <div role="status" className="mb-3 flex flex-wrap items-center gap-2 text-xs text-warning">
            {levelsQ.data ? '关键价位更新失败，当前显示上次数据。' : '关键价位加载失败，请检查网络或数据源配置。'}
            <RetryButton onClick={() => levelsQ.refetch()} disabled={levelsQ.isFetching} />
          </div>
        ) : !hasLevels ? (
          <p className="mb-3 text-xs text-muted">暂无可用关键价位，日 K 数据仍可查看。</p>
        ) : null}
        <AnalysisKChart
          rows={rows}
          levels={levels}
          series={levelsQ.data?.series}
          seriesDates={levelsQ.data?.dates}
          defaultLevelTypes={['sr', 'pivot', 'keltner_s']}
          scopeKey={symbol}
          height={height}
        />
      </div>
    </div>
  )
}

function RetryButton({ onClick, disabled }: { onClick: () => void; disabled: boolean }) {
  return <button type="button" onClick={onClick} disabled={disabled}
    className="rounded-btn border border-border px-2 py-1 text-xs text-secondary hover:bg-elevated hover:text-foreground focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent disabled:opacity-50">
    重试
  </button>
}
