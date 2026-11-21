import { useMemo, useRef } from 'react'
import type { FirstBoardCandidate } from '@/lib/api'
import { cn } from '@/lib/cn'
import { fmtPct, fmtPrice, priceColorClass } from '@/lib/format'
import { AttentionSummary } from './AttentionCell'
import { BuyAction, Conclusion, MainlineTag, RoleTags } from './CandidateTable'
import { pickImminent } from './pickImminent'
import { PrioritySummary } from './PriorityCell'
import { panelClass, pct } from './shared'

/**
 * 临近涨停焦点区：盘中最需要秒级决策的候选单独成卡，不受表格筛选与排序冻结影响。
 * 卡片位置稳定、区域高度固定，卡片出现或消失不会让下面的表格位移；快照无效时整块不渲染。
 */
export function ImminentCards({ rows, current, globalReasons, canOrder, onOpen, onBuy }: {
  rows: FirstBoardCandidate[] | undefined; current: boolean; globalReasons: string[]; canOrder: boolean
  onOpen: (symbol: string, name: string) => void; onBuy: (eventId: string, amount: number) => void
}) {
  const shown = useRef<string[]>([])
  const picked = useMemo(() => {
    const result = pickImminent(current ? rows ?? [] : [], current ? shown.current : [])
    shown.current = result.keys
    return result
  }, [rows, current])
  const globalSet = useMemo(() => new Set(globalReasons), [globalReasons])
  if (!current) return null
  const { cards, total } = picked
  return <section aria-label="临近涨停" className={cn(panelClass, 'space-y-3')}>
    <h2 className="text-sm font-semibold">临近涨停 <span className="font-normal text-secondary">{total} 只{total > cards.length && ` · 另有 ${total - cards.length} 只，见下表`}</span></h2>
    <div data-imminent-body className="flex h-64 gap-3 overflow-auto">
      {cards.length ? cards.map(row => {
        const stockReasons = row.blocked_reasons.filter(reason => !globalSet.has(reason))
        const globalOnly = row.blocked_reasons.some(reason => globalSet.has(reason))
        return <article key={`${row.symbol}-${row.pattern}`} data-imminent-card aria-label={`${row.name} ${row.symbol} 临近涨停`}
          className={cn('flex min-w-[17rem] flex-1 flex-col gap-2 rounded-card border p-3 text-xs', row.can_buy ? 'border-accent/50 bg-accent/5' : 'border-border opacity-80')}>
          <div className="flex flex-wrap items-center gap-1">
            <button type="button" className="text-left text-sm font-medium hover:underline focus-visible:outline focus-visible:outline-accent" onClick={() => onOpen(row.symbol, row.name)}>{row.name}</button>
            <RoleTags row={row} /><MainlineTag row={row} />
            <span className="text-secondary">{row.symbol}</span>
          </div>
          <div className="flex flex-wrap items-baseline gap-x-4 gap-y-1 tabular-nums">
            <span className={cn('text-base font-medium', priceColorClass(row.price ? row.change_pct : null))}>{fmtPrice(row.price)}</span>
            <span className={priceColorClass(row.change_pct)}>{fmtPct(row.change_pct)}</span>
            <span><span className="text-secondary">距涨停 </span>{pct(row.distance_to_limit_pct)}</span>
          </div>
          <div className="flex flex-wrap items-start gap-x-4 gap-y-1"><PrioritySummary row={row} current={current} /><AttentionSummary context={row.attention_context} snapshotCurrent={current} /></div>
          <div className="leading-snug"><Conclusion row={row} current={current} stockReasons={stockReasons} globalOnly={globalOnly} /></div>
          {row.can_buy && <div className="mt-auto"><BuyAction row={row} canOrder={canOrder} onBuy={onBuy} /></div>}
        </article>
      }) : <p className="self-center text-sm text-secondary">当前没有临近涨停的候选</p>}
    </div>
  </section>
}
