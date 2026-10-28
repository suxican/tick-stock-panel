import { useRef, useState } from 'react'

export type StockMarks = Record<string, { focused: boolean; pinned: boolean }>
export type MarkKind = keyof StockMarks[string]
type MarkStore = { get: (fallback: StockMarks) => StockMarks; set: (value: StockMarks) => void }

function loadMarks(store: MarkStore): StockMarks {
  const saved = store.get({})
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

export function useStockMarks(store: MarkStore) {
  const [marks, setMarks] = useState(() => loadMarks(store))
  const current = useRef(marks)
  const [error, setError] = useState<Error | null>(null)

  function save(next: StockMarks) {
    store.set(next)
    // 共享存储会吞掉浏览器写入异常，读回确认后才能承诺刷新后保留。
    const saved = store.get({})
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

/** 保留原字段排序，再按股票标记稳定分组；调用方随后分页。 */
export function markedFirst<T extends { symbol: string }>(rows: T[], marks: StockMarks, kind: MarkKind = 'pinned'): T[] {
  const marked: T[] = []
  const others: T[] = []
  for (const row of rows) (marks[row.symbol]?.[kind] ? marked : others).push(row)
  return [...marked, ...others]
}
