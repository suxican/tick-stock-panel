import { useRef, useState } from 'react'
import { Pin, Star } from 'lucide-react'
import { cn } from '@/lib/cn'
import { storage } from '@/lib/storage'

type StockMarks = ReturnType<typeof storage.huichunStockMarks.get>
type Mark = StockMarks[string]
export type MarkKind = keyof Mark

function loadMarks(): StockMarks {
  const saved = storage.huichunStockMarks.get({})
  if (!saved || typeof saved !== 'object' || Array.isArray(saved)) return {}
  const marks: StockMarks = {}
  for (const [symbol, value] of Object.entries(saved)) {
    if (!/^\d{6}\.(SH|SZ|BJ)$/.test(symbol) || !value || typeof value !== 'object' || Array.isArray(value)) continue
    const focused = value.focused === true
    const pinned = value.pinned === true
    if (focused || pinned) marks[symbol] = { focused, pinned }
  }
  return marks
}

export function useStockMarks() {
  const [marks, setMarks] = useState(loadMarks)
  const current = useRef(marks)
  const [error, setError] = useState<Error | null>(null)

  function save(next: StockMarks) {
    storage.huichunStockMarks.set(next)
    // 共享存储会吞掉浏览器写入异常，读回确认后才能承诺刷新后保留。
    const saved = storage.huichunStockMarks.get({})
    setError(JSON.stringify(saved) === JSON.stringify(next) ? null
      : new Error('标记未能保存，当前页面仍有效，刷新后可能丢失。请重试。'))
  }

  function toggle(symbol: string, kind: MarkKind) {
    const next = { ...current.current }
    const value = { ...(next[symbol] ?? { focused: false, pinned: false }) }
    value[kind] = !value[kind]
    if (value.focused || value.pinned) next[symbol] = value
    else delete next[symbol]
    current.current = next
    setMarks(next)
    save(next)
  }

  return { marks, toggle, error, retry: () => save(current.current) }
}

/** 先保留原字段排序，再把置顶股票稳定分组；调用方随后分页。 */
export function pinnedFirst<T extends { symbol: string }>(rows: T[], marks: StockMarks): T[] {
  const pinned: T[] = []
  const others: T[] = []
  for (const row of rows) (marks[row.symbol]?.pinned ? pinned : others).push(row)
  return [...pinned, ...others]
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
