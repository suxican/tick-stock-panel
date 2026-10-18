import type { ReactNode } from 'react'
import type { PositionTDirection } from '@/lib/api'
import { cn } from '@/lib/cn'

export const inputClass = 'h-9 w-full min-w-0 rounded-btn border border-border bg-base px-3 text-xs text-foreground placeholder:text-secondary focus:outline-none focus:border-accent focus:ring-1 focus:ring-accent disabled:opacity-50'
export const buttonClass = 'inline-flex min-h-9 items-center justify-center gap-1.5 rounded-btn border border-border px-3 text-xs font-medium text-foreground transition-colors hover:bg-elevated focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-50 disabled:cursor-not-allowed'
export const primaryClass = `${buttonClass} border-accent/50 bg-accent/10 text-accent hover:bg-accent/20`
export const directionLabel = (direction: PositionTDirection) => direction === 'buy_sell' ? '先买后卖' : '先卖后买'
export const beijingDate = () => new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit' }).format(new Date())
export const beijingInputTime = () => new Intl.DateTimeFormat('sv-SE', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }).format(new Date()).replace(' ', 'T')
export function formatTime(value?: string | null) {
  if (!value) return '未提供'
  const parsed = new Date(value)
  return Number.isNaN(parsed.getTime()) ? value : new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }).format(parsed)
}
export const money = (n?: number | null) => n == null || !Number.isFinite(n) ? '待核对' : `¥${n.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
export const priceRange = (p: [number, number] | null | undefined) => p?.every(Number.isFinite) ? `${p[0].toFixed(2)} — ${p[1].toFixed(2)}` : '数据不足'
export const newKey = () => crypto.randomUUID()
export function numberValue(value: string, label: string, options: { optional?: boolean; integer?: boolean; positive?: boolean } = {}): number | null {
  if (!value.trim() && options.optional) return null
  const n = Number(value)
  if (!value.trim() || !Number.isFinite(n) || n < 0 || (options.integer && !Number.isInteger(n)) || (options.positive && n <= 0)) throw new Error(`${label}请填写${options.positive ? '正' : '非负'}${options.integer ? '整数' : '数值'}`)
  return n
}
export function signalCanPlan(state: string, expiresAt: string, quantity: number, now = Date.now()) {
  return state === 'triggered' && quantity > 0 && Number.isFinite(Date.parse(expiresAt)) && Date.parse(expiresAt) > now
}
export const planDone = (state: string) => state === 'completed' || state === 'cancelled'
export const statusLabels: Record<string, string> = {
  none: '无机会', watching: '观察中', triggered: '条件触发', blocked: '受约束阻断', paused: '数据不足／暂停', expired: '已过期',
  first_pending: '第一笔待执行', first_partial: '第一笔部分成交', second_pending: '第二笔待执行', second_partial: '第二笔部分成交', completed: '全部匹配完成', exposure: '未完成敞口待处理', cancelled: '零成交计划已结束',
  submitted: '已提交', partially_filled: '部分成交', filled: '全部成交', cancel_pending: '撤单待确认', rejected: '已拒绝',
}
export function Status({ state }: { state: string }) {
  return <span className={cn('inline-flex rounded border px-2 py-0.5 text-xs', ['triggered', 'completed'].includes(state) ? 'border-accent/30 bg-accent/10 text-accent' : ['blocked', 'paused', 'exposure', 'expired'].includes(state) ? 'border-border bg-elevated text-foreground' : 'border-border text-secondary')}>{statusLabels[state] ?? '状态待核对'}</span>
}
export function Field({ label, children, hint }: { label: string; children: ReactNode; hint?: string }) {
  return <label className="block min-w-0 space-y-1.5 text-xs text-secondary"><span>{label}</span>{children}{hint && <span className="block text-xs leading-relaxed text-secondary">{hint}</span>}</label>
}
export function Notice({ children, error = false }: { children: ReactNode; error?: boolean }) {
  return <div role={error ? 'alert' : 'status'} className={cn('rounded-lg border px-3 py-2.5 text-xs leading-relaxed', error ? 'border-danger/40 text-foreground bg-danger/5' : 'border-border bg-elevated/50 text-secondary')}>{children}</div>
}
export function Empty({ title, children }: { title: string; children: ReactNode }) {
  return <div className="rounded-lg border border-dashed border-border px-5 py-10 text-center"><p className="text-sm font-medium text-foreground">{title}</p><div className="mx-auto mt-2 max-w-xl text-xs leading-relaxed text-secondary">{children}</div></div>
}
