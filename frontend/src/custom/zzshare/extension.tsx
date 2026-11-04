import { lazy } from 'react'
import { Radar } from 'lucide-react'
import type { FrontendExtension, FrontendSlotContextMap } from '@/extensions/types'
import { useZzshareReasons } from '@/lib/useZzshareReasons'

export function StockLimitReason({ symbol, tradeDate }: FrontendSlotContextMap['stock-preview.footer']) {
  const snapshot = useZzshareReasons(tradeDate)
  const reason = tradeDate ? snapshot.data?.get(symbol) : undefined
  let text = '该交易日暂无本股的归因记录。'
  if (!tradeDate) text = '暂无可核对的交易日。'
  else if (snapshot.isError) text = '本地归因快照读取失败。'
  else if (snapshot.isLoading) text = '正在读取本地归因快照…'
  else if (reason) text = reason
  else if (!snapshot.data?.size) text = '本地暂无该交易日的归因快照。可在「设置 → 数据源 → zzshare」同步该日。'

  return (
    <section aria-label="当日涨停归因" className="border-t border-border/60 bg-surface px-4 py-2.5">
      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
        <h3 className="text-xs font-semibold text-foreground">当日涨停归因</h3>
        {tradeDate && <time dateTime={tradeDate} className="font-mono text-[11px] text-secondary">{tradeDate}</time>}
        <span className="text-[11px] text-secondary">ZZShare · 盘后快照</span>
        {tradeDate && snapshot.isError && (
          <button type="button" className="text-xs text-accent" disabled={snapshot.isFetching} onClick={() => void snapshot.refetch()}>重试</button>
        )}
      </div>
      <p className="mt-1 max-h-24 overflow-auto whitespace-pre-wrap break-words text-xs leading-relaxed text-secondary">{text}</p>
    </section>
  )
}

const ThemeRadar = lazy(() => import('./ThemeRadar').then(module => ({ default: module.ThemeRadar })))

const extension: FrontendExtension = {
  id: 'official.zzshare',
  apiVersion: 1,
  routes: [{ id: 'zzshare-radar', path: '/zzshare-radar', component: ThemeRadar }],
  navigation: [{ id: 'zzshare-radar', routeId: 'zzshare-radar', label: '题材雷达', icon: Radar, order: 112 }],
  slots: [{ id: 'zzshare-stock-limit-reason', name: 'stock-preview.footer', component: StockLimitReason }],
}

export default extension
