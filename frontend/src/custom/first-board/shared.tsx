import type { ReactNode } from 'react'
import type { FirstBoardState } from '@/lib/api'
import { cn } from '@/lib/cn'

export const buttonClass = 'inline-flex min-h-9 items-center justify-center gap-1.5 rounded-btn border border-border bg-elevated px-3 py-1.5 text-xs text-secondary transition-colors hover:text-foreground focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent disabled:cursor-not-allowed disabled:opacity-50'
export const inputClass = 'min-h-9 w-full min-w-0 rounded-btn border border-border bg-base px-2.5 py-1.5 text-sm text-foreground focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent disabled:opacity-50'
export const panelClass = 'rounded-card border border-border bg-surface p-4'
export const patternLabels = { platform: '平台突破', trend: '趋势加速', oversold: '超跌反弹' }
export const stateLabels: Record<FirstBoardState, string> = { watch: '观察中', approaching: '临近涨停', sealed: '涨停价观察', broken: '已开板', invalid: '已失效' }
export const marketStates = [{ value: 'strong', label: '强势' }, { value: 'lean_strong', label: '偏强' }, { value: 'range', label: '震荡' }, { value: 'lean_weak', label: '偏弱' }, { value: 'weak', label: '弱势' }]

export function beijingDay(offset = 0, now = Date.now()) {
  return new Intl.DateTimeFormat('sv-SE', { timeZone: 'Asia/Shanghai' }).format(new Date(now + offset * 86400000))
}
export function pct(value: number | null | undefined, scale = 100) {
  return value == null || !Number.isFinite(value) ? '—' : `${(value * scale).toFixed(2)}%`
}
export function money(value: number | null | undefined) {
  return value == null || !Number.isFinite(value) ? '—' : value.toLocaleString('zh-CN', { maximumFractionDigits: 2, minimumFractionDigits: 2 })
}
export function stamp(value: string | number | null | undefined) {
  if (value == null) return '—'
  const date = new Date(value)
  if (!Number.isFinite(date.getTime())) return String(value)
  return new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }).format(date)
}
export function StateBadge({ state }: { state: FirstBoardState }) {
  return <span className={cn('inline-flex whitespace-nowrap rounded px-2 py-0.5 text-xs',
    state === 'approaching' ? 'bg-accent/15 text-foreground' : state === 'sealed' ? 'bg-bull/10 text-foreground' : 'bg-elevated text-secondary')}>{stateLabels[state] ?? state}</span>
}
export function Notice({ children, error = false }: { children: ReactNode; error?: boolean }) {
  return <div role={error ? 'alert' : 'status'} className={cn('rounded-btn border px-3 py-2.5 text-sm leading-relaxed', error ? 'border-danger/40 bg-danger/5' : 'border-border bg-elevated/50')}>{children}</div>
}
export function LoadingRows() {
  return <div role="status" aria-label="正在加载首板数据" className="space-y-3 py-4">{[0, 1, 2].map(i => <div key={i} className="h-9 rounded bg-elevated motion-safe:animate-pulse" />)}</div>
}
export function ErrorNotice({ error, retry }: { error: Error | null; retry?: () => void }) {
  return error ? <Notice error><div className="flex flex-wrap items-center gap-3"><span>{error.message}</span>{retry && <button className={buttonClass} onClick={retry}>重试</button>}</div></Notice> : null
}
