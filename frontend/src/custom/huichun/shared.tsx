import type { ReactNode } from 'react'
import { cn } from '@/lib/cn'

export const buttonClass = 'inline-flex min-h-9 items-center justify-center gap-1.5 rounded-btn border border-border bg-elevated px-3 py-1.5 text-xs text-secondary transition-colors hover:text-foreground focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent disabled:cursor-not-allowed disabled:opacity-50'
export const primaryClass = `${buttonClass} border-accent/50 bg-accent/10 text-foreground hover:bg-accent/20`
export const inputClass = 'min-h-9 w-full min-w-0 rounded-btn border border-border bg-base px-2.5 py-1.5 text-sm text-foreground focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent disabled:opacity-50'
export const panelClass = 'rounded-card border border-border bg-surface p-4'

export function pct(value: number | null | undefined, signed = false) {
  return value == null || !Number.isFinite(value) ? '—' : `${signed && value > 0 ? '+' : ''}${(value * 100).toFixed(2)}%`
}
export function price(value: number | null | undefined) {
  return value == null || !Number.isFinite(value) ? '—' : value.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
}
export function stamp(value: string | null | undefined) {
  if (!value) return '—'
  const date = new Date(value)
  if (!Number.isFinite(date.getTime())) return value
  return new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }).format(date)
}
export function Notice({ children, error = false }: { children: ReactNode; error?: boolean }) {
  return <div role={error ? 'alert' : 'status'} className={cn('rounded-btn border px-3 py-2.5 text-sm leading-relaxed', error ? 'border-danger/40 bg-danger/5' : 'border-border bg-elevated/50')}>{children}</div>
}
export function ErrorNotice({ error, retry }: { error: Error | null; retry?: () => void }) {
  return error ? <Notice error><div className="flex flex-wrap items-center gap-3"><span>{error.message}</span>{retry && <button className={buttonClass} onClick={retry}>重试</button>}</div></Notice> : null
}
export function LoadingRows() {
  return <div role="status" aria-label="正在加载回春数据" className="space-y-3 py-4">{[0, 1, 2].map(i => <div key={i} className="h-9 rounded bg-elevated motion-safe:animate-pulse" />)}</div>
}
export function Pagination({ page, pages, onPage }: { page: number; pages: number; onPage: (page: number) => void }) {
  return pages > 1 ? <div className="mt-4 flex items-center justify-end gap-3 text-xs"><button className={buttonClass} disabled={page === 0} onClick={() => onPage(page - 1)}>上一页</button><span className="tabular-nums">{page + 1} / {pages}</span><button className={buttonClass} disabled={page + 1 >= pages} onClick={() => onPage(page + 1)}>下一页</button></div> : null
}
