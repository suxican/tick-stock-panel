import type { ReactNode } from 'react'
import type { FirstBoardEvent, FirstBoardState } from '@/lib/api'
import { cn } from '@/lib/cn'

export const buttonClass = 'inline-flex min-h-9 items-center justify-center gap-1.5 rounded-btn border border-border bg-elevated px-3 py-1.5 text-xs text-secondary transition-colors hover:text-foreground focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent disabled:cursor-not-allowed disabled:opacity-50'
export const inputClass = 'min-h-9 w-full min-w-0 rounded-btn border border-border bg-base px-2.5 py-1.5 text-sm text-foreground focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent disabled:opacity-50'
export const panelClass = 'rounded-card border border-border bg-surface p-4'
export const patternLabels = { platform: '平台突破', trend: '趋势加速', oversold: '超跌反弹' }
export const universeLabels = { hs_a_non_st: '沪深主板、创业板、科创板（非 ST）', main_board_non_st: '沪深主板（非 ST）' }
export const stateLabels: Record<FirstBoardState, string> = { watch: '观察中', approaching: '临近涨停', sealed: '涨停价观察', broken: '已开板', invalid: '已失效' }
export const marketStates = [{ value: 'strong', label: '强势' }, { value: 'lean_strong', label: '偏强' }, { value: 'range', label: '震荡' }, { value: 'lean_weak', label: '偏弱' }, { value: 'weak', label: '弱势' }]

const badgeClass = 'inline-flex shrink-0 items-center whitespace-nowrap border px-2 py-0.5 text-xs font-medium'
const neutralBadgeClass = 'border-border bg-elevated text-secondary'
const stateColors: Record<FirstBoardState, string> = {
  watch: 'border-blue-500/25 bg-blue-500/10 text-blue-800 dark:text-blue-300',
  approaching: 'border-amber-500/25 bg-amber-500/10 text-amber-800 dark:text-amber-300',
  sealed: 'border-rose-500/25 bg-rose-500/10 text-rose-800 dark:text-rose-300',
  broken: 'border-orange-500/25 bg-orange-500/10 text-orange-800 dark:text-orange-300',
  invalid: 'border-slate-500/25 bg-slate-500/10 text-slate-700 dark:text-slate-300',
}
const eventLabels: Record<string, string> = { buy_candidate: '买入候选', approaching: '临近涨停', sealed: '涨停价观察', broken: '开板观察', invalid: '失效', exit_candidate: '退出提示', watch: '进入观察' }
const eventColors: Record<string, string> = {
  ...stateColors,
  buy_candidate: 'border-emerald-500/25 bg-emerald-500/10 text-emerald-800 dark:text-emerald-300',
  exit_candidate: 'border-red-500/25 bg-red-500/10 text-red-800 dark:text-red-300',
}
const patternColors: Record<FirstBoardEvent['pattern'], string> = {
  platform: 'border-cyan-500/40 text-cyan-800 dark:text-cyan-300',
  trend: 'border-indigo-500/40 text-indigo-800 dark:text-indigo-300',
  oversold: 'border-violet-500/40 text-violet-800 dark:text-violet-300',
  exit: neutralBadgeClass,
}

export function beijingDay(offset = 0, now = Date.now()) {
  const parts = new Intl.DateTimeFormat('sv-SE', {
    timeZone: 'Asia/Shanghai', calendar: 'gregory', numberingSystem: 'latn',
    year: 'numeric', month: '2-digit', day: '2-digit',
  }).formatToParts(new Date(now + offset * 86400000))
  // API 和日期输入框要求 ISO 日期，不能依赖浏览器区域格式的顺序与分隔符。
  const pick = (type: string) => parts.find(part => part.type === type)?.value ?? ''
  return `${pick('year')}-${pick('month')}-${pick('day')}`
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
  return <span className={cn(badgeClass, 'rounded', stateColors[state] ?? neutralBadgeClass)}>{stateLabels[state] ?? state}</span>
}
export function EventBadge({ type }: { type: string }) {
  return <span className={cn(badgeClass, 'rounded', eventColors[type] ?? neutralBadgeClass)}>{eventLabels[type] ?? '状态变化'}</span>
}
export function PatternBadge({ pattern, label }: { pattern: FirstBoardEvent['pattern']; label: string }) {
  return <span className={cn(badgeClass, 'rounded-full', patternColors[pattern] ?? neutralBadgeClass)}>{label}</span>
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
