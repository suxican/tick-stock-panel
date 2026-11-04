import { lazy } from 'react'
import { useZzshareReasons } from '@/lib/useZzshareReasons'
import type { FrontendExtension } from '@/extensions/types'
import { IconLadder } from '@/components/BrandIcons'

export const LimitUpLadder = lazy(() => import('./Page').then(m => ({ default: m.LimitUpLadder })))

const StockCard = React.memo(function StockCard({ stock, reason, extFields, direction, sealMode, monitored, monitorRule, onMonitorChange, hasDepth, onClick, onDimensionClick, active }: {
  reason?: string
    <div className="relative group w-full min-w-0 has-[details[open]]:col-span-full">
    {reason && (
      <details className="group/reason mt-1 rounded bg-elevated/40 px-2 py-1 text-xs">
        <summary className="cursor-pointer truncate text-secondary focus-visible:outline-accent">
          涨停归因<span className="ml-2 text-muted group-open/reason:hidden">{reason}</span>
        </summary>
        <p className="mt-1.5 max-w-prose whitespace-pre-wrap break-words leading-relaxed text-foreground">{reason}</p>
      </details>
    )}
function TierGroup({ tier, reasons, defaultOpen, extFields, filterKeys, bf, onStockClick, selectedTag, onSelectTag, onDimensionClick, direction, sealMode, monitoredSymbols, ladderRules, onMonitorChange, hasDepth, activeSymbol }: {
  reasons?: ReadonlyMap<string, string>
                  reason={reasons?.get(s.symbol)}
  const [previewDate, setPreviewDate] = useState<string | null>(null)
  const reasonDate = direction === 'up' && data?.tiers.length ? data.as_of : undefined
  const reasons = useZzshareReasons(reasonDate)
    setPreviewDate(data?.as_of ?? null)
  }, [data?.as_of])
      {reasonDate && (
        <div role="status" className="px-5 pb-1 text-[11px] text-secondary">
          ZZShare · {reasonDate} · {reasons.isError
            ? '归因暂不可用'
            : reasons.isPending ? '正在读取盘后归因…'
              : reasons.data?.size ? '点击「涨停归因」展开全文；未标注的股票无当日归因记录。'
                : '暂无涨停归因，可在设置 → 数据源 → zzshare 中同步该日。'}
          {reasons.isError && <button type="button" className="ml-2 text-accent" disabled={reasons.isFetching} onClick={() => void reasons.refetch()}>重试</button>}
        </div>
      )}
            reasons={reasonDate ? reasons.data : undefined}
        tradeDate={previewDate}
const extension: FrontendExtension = {
  id: 'official.limit-ladder',
  apiVersion: 1,
  routes: [
    { id: 'limit-ladder', path: '/limit-ladder', component: LimitUpLadder },
  ],
  navigation: [
    { id: 'limit-ladder', routeId: 'limit-ladder', label: '连板梯队', icon: IconLadder },
  ],
}

export default extension
