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
/*
 * 颜色只表达一类含义：红绿留给涨跌数值；蓝色表示正向信号或可操作；
 * 琥珀表示接近或触及涨停价（描边→实心逐级加强）；分类与数据状态一律用灰色。
 */
export const positiveBadgeClass = 'border-accent/40 bg-accent/10 text-blue-700 dark:text-blue-300'
const stateColors: Record<FirstBoardState, string> = {
  watch: neutralBadgeClass,
  approaching: 'border-amber-500/50 bg-transparent text-amber-800 dark:text-amber-300',
  sealed: 'border-amber-500/40 bg-amber-500/15 text-amber-800 dark:text-amber-300',
  broken: 'border-dashed border-border bg-transparent text-secondary',
  invalid: 'border-transparent bg-transparent font-normal text-muted',
}
export const eventLabels = { buy_candidate: '买入候选', approaching: '临近涨停', sealed: '涨停价观察', broken: '开板观察', invalid: '失效', exit_candidate: '退出提示', watch: '进入观察' }
const eventColors: Record<string, string> = {
  ...stateColors,
  buy_candidate: positiveBadgeClass,
  exit_candidate: 'border-accent/50 bg-transparent text-blue-700 dark:text-blue-300',
}
// 形态只是分类，用中性描边；圆角与状态徽章区分。
const patternColors: Record<FirstBoardEvent['pattern'], string> = {
  platform: 'border-border text-secondary',
  trend: 'border-border text-secondary',
  oversold: 'border-border text-secondary',
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
  return <span className={cn(badgeClass, 'rounded', eventColors[type] ?? neutralBadgeClass)}>{eventLabels[type as keyof typeof eventLabels] ?? '状态变化'}</span>
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
