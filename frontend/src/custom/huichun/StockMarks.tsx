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
  const buttonClass = 'inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-btn transition-colors focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent'
  return <div className="flex items-center gap-0.5">
    <button type="button" aria-pressed={focused} aria-label={`${focused ? '取消重点关注' : '重点关注'} ${name} ${symbol}`} title={focused ? '取消重点关注' : '标为重点关注'}
      className={cn(buttonClass, focused ? 'bg-accent/10 text-blue-700 dark:text-blue-400' : 'text-secondary hover:bg-elevated hover:text-foreground')}
      onClick={() => onToggle(symbol, 'focused')}><Star className="h-3.5 w-3.5" fill={focused ? 'currentColor' : 'none'} aria-hidden="true" /></button>
    <button type="button" aria-pressed={pinned} aria-label={`${pinned ? '取消置顶' : '置顶'} ${name} ${symbol}`} title={pinned ? '取消置顶' : '置顶优先显示'}
      className={cn(buttonClass, pinned ? 'bg-accent/10 text-blue-700 dark:text-blue-400' : 'text-secondary hover:bg-elevated hover:text-foreground')}
      onClick={() => onToggle(symbol, 'pinned')}><Pin className="h-3.5 w-3.5" fill={pinned ? 'currentColor' : 'none'} aria-hidden="true" /></button>
  </div>
}
