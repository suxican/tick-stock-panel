import { Pin, Star } from 'lucide-react'
import { cn } from '@/lib/cn'
import { storage } from '@/lib/storage'
import { useStockMarks as useStoredStockMarks, type StockMarks, type MarkKind } from '@/lib/useStockMarks'

export { markedFirst as pinnedFirst, type MarkKind } from '@/lib/useStockMarks'
type Mark = StockMarks[string]

export function useStockMarks() {
  return useStoredStockMarks(storage.huichunStockMarks)
}

export function StockMarkButtons({ symbol, name, mark, onToggle }: {
  symbol: string; name: string; mark?: Mark; onToggle: (symbol: string, kind: MarkKind) => void
}) {
  const focused = !!mark?.focused
  const pinned = !!mark?.pinned
  const buttonClass = 'inline-flex min-h-8 items-center justify-center gap-1 whitespace-nowrap rounded border px-1.5 text-[11px] transition-colors focus-visible:outline focus-visible:outline-accent'
  return <div className="mt-1.5 flex items-center gap-1">
    <button type="button" aria-pressed={focused} aria-label={`${focused ? '取消重点关注' : '重点关注'} ${name} ${symbol}`} title={focused ? '取消重点关注' : '标为重点关注'}
      className={cn(buttonClass, 'min-w-[84px]', focused ? 'border-amber-400/40 bg-amber-400/10 text-amber-400' : 'border-border text-secondary hover:text-foreground')}
      onClick={() => onToggle(symbol, 'focused')}><Star className="h-3.5 w-3.5" fill={focused ? 'currentColor' : 'none'} aria-hidden="true" />{focused ? '已关注' : '重点关注'}</button>
    <button type="button" aria-pressed={pinned} aria-label={`${pinned ? '取消置顶' : '置顶'} ${name} ${symbol}`} title={pinned ? '取消置顶' : '置顶优先显示'}
      className={cn(buttonClass, 'min-w-[68px]', pinned ? 'border-accent/40 bg-accent/10 text-accent' : 'border-border text-secondary hover:text-foreground')}
      onClick={() => onToggle(symbol, 'pinned')}><Pin className="h-3.5 w-3.5" fill={pinned ? 'currentColor' : 'none'} aria-hidden="true" />{pinned ? '已置顶' : '置顶'}</button>
  </div>
}
