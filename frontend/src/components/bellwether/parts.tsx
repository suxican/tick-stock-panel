/**
 * 盘中风向标共用界面 —— 复盘页卡片与看板组件共用的标签、单行展示与数据查询。
 * 比例字段为小数制, 展示时换算为百分数。
 */
import { useQuery } from '@tanstack/react-query'
import {
  api,
  type BellwetherItem,
  type BellwetherNewcomer,
  type BellwetherPoolPerf,
  type BellwetherScope,
  type BellwetherSnapshot,
  type BellwetherState,
  type BellwetherTimeline,
  type BellwetherVerdict,
  type BellwetherVerdictChange,
  type UserBellwetherSnapshot,
} from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { cn } from '@/lib/cn'
import { priceColorClass } from '@/lib/format'

export type OpenStock = ((symbol: string) => void) | undefined
export type UserSector = UserBellwetherSnapshot['sectors'][number]

export const SCOPES: { key: BellwetherScope; label: string }[] = [
  { key: 'market', label: '大盘' }, { key: 'holdings', label: '持仓' }, { key: 'watchlist', label: '自选' },
]

export function isBellwetherScope(value: unknown): value is BellwetherScope {
  return SCOPES.some(s => s.key === value)
}

const VERDICT_CLS: Record<BellwetherVerdict['key'], string> = {
  attack: 'border-bull/30 bg-bull/10 text-bull',
  neutral: 'border-border bg-elevated text-secondary',
  divergence: 'border-warning/30 bg-warning/10 text-warning',
  retreat: 'border-bear/30 bg-bear/10 text-bear',
  unknown: 'border-border bg-elevated text-muted',
}

const STATE_TEXT: Record<BellwetherState, string> = {
  sealed: '封板', broken: '炸板', strong: '强势', flat: '震荡', weak: '走弱', limit_down: '跌停', unknown: '待确认',
}
const STATE_CLS: Record<BellwetherState, string> = {
  sealed: 'bg-bull/15 text-bull', strong: 'bg-bull/10 text-bull', flat: 'bg-elevated text-secondary',
  broken: 'bg-warning/15 text-warning', weak: 'bg-bear/10 text-bear', limit_down: 'bg-bear/15 text-bear',
  unknown: 'bg-elevated text-muted',
}
export const RELATIVE_TEXT: Record<UserSector['mine'][number]['relative'], string> = {
  stronger: '跑赢', inline: '同步', weaker: '跑输', unknown: '',
}
const VERDICT_TEXT: Record<BellwetherVerdict['key'], string> = {
  attack: '进攻', neutral: '震荡', divergence: '分歧', retreat: '退潮', unknown: '待确认',
}

export function pct(v: number | null | undefined, digits = 2): string {
  if (v == null) return '—'
  return `${v > 0 ? '+' : ''}${(v * 100).toFixed(digits)}%`
}

export function relativeClass(relative: UserSector['mine'][number]['relative']): string {
  return relative === 'stronger' ? 'text-bull' : relative === 'weaker' ? 'text-bear' : 'text-muted'
}

export function boardsText(item: BellwetherItem): string | null {
  if (item.boards_prev == null || item.boards_prev < 1) return null
  if (item.boards_today == null) return `昨 ${item.boards_prev} 板`
  // 盘中未封板不等于晋级失败, 只陈述当前事实
  return `昨 ${item.boards_prev} 板 → ${item.boards_today > item.boards_prev ? `今 ${item.boards_today} 板` : '今未封板'}`
}

export function timeText(data: BellwetherSnapshot | UserBellwetherSnapshot): string {
  if (data.live && data.quote_time) return `行情 ${data.quote_time.slice(11, 16)}`
  return data.as_of ? `${data.as_of.slice(5)} ${data.live ? '盘中' : '非交易时段'}` : ''
}

/** 风向标快照查询; 与复盘页共用查询键, 两处同时打开时共享缓存。 */
export function useBellwetherSnapshot(scope: BellwetherScope, groupId: string | null = null, enabled = true) {
  const activeGroup = scope === 'watchlist' ? groupId : null
  const query = useQuery({
    queryKey: QK.reviewBellwethers(scope, activeGroup),
    queryFn: () => api.reviewBellwethers(scope, activeGroup),
    // 盘中跟随行情刷新; 非交易时段数据静止, 只需低频确认是否开盘
    refetchInterval: (q) => (q.state.data?.live ? 15_000 : 60_000),
    staleTime: 10_000,
    enabled,
  })
  // 切换范围时不展示上一范围的结果
  const data = query.data && query.data.scope === scope ? query.data : undefined
  return { query, data }
}

export function ScopeTabs({ scope, onChange }: { scope: BellwetherScope; onChange: (scope: BellwetherScope) => void }) {
  return (
    <div className="flex shrink-0 rounded-btn border border-border p-px text-[11px]" role="tablist">
      {SCOPES.map(s => (
        <button key={s.key} type="button" role="tab" aria-selected={scope === s.key} onClick={() => onChange(s.key)}
          className={cn('rounded px-1.5 leading-5 transition-colors',
            scope === s.key ? 'bg-accent/15 text-accent' : 'text-secondary hover:text-foreground')}>
          {s.label}
        </button>
      ))}
    </div>
  )
}

export function StateTag({ state }: { state: BellwetherState }) {
  return <span className={cn('shrink-0 rounded px-1.5 text-[10px] leading-4', STATE_CLS[state])}>{STATE_TEXT[state]}</span>
}

export function VerdictTag({ verdict }: { verdict: BellwetherVerdict }) {
  return (
    <span title={verdict.reason}
      className={cn('shrink-0 rounded border px-1.5 text-[11px] font-medium leading-5', VERDICT_CLS[verdict.key])}>
      {verdict.label}
    </span>
  )
}

export function TimelineHint({ t }: { t?: BellwetherTimeline | null }) {
  if (!t) return null
  const parts = [
    t.first_sealed_at ? `首封 ${t.first_sealed_at.slice(0, 5)}` : null,
    t.broken_count > 0 ? `炸 ${t.broken_count}` : null,
  ].filter(Boolean).join(' · ')
  const history = t.events.map(e => `${e.t.slice(0, 5)} ${e.from ? `${STATE_TEXT[e.from]}→` : '开始 '}${STATE_TEXT[e.to]}`).join('\n')
  if (!parts && !t.trend) return null
  return (
    <span className="shrink-0 text-[10px] text-muted" title={history}>
      {parts}
      {t.trend && (
        <span className={cn(parts && 'ml-1', t.trend === 'up' ? 'text-bull' : 'text-bear')}>
          {t.trend === 'up' ? '弱转强' : '强转弱'}
        </span>
      )}
    </span>
  )
}

export function ChangeHint({ change }: { change?: BellwetherVerdictChange | null }) {
  if (!change) return null
  return (
    <span className="shrink-0 text-[10px] text-muted">
      {change.t.slice(0, 5)} {VERDICT_TEXT[change.from]}→{VERDICT_TEXT[change.to]}
    </span>
  )
}

export function Newcomers({ label, items, onOpenStock }: { label: string; items?: BellwetherNewcomer[]; onOpenStock: OpenStock }) {
  if (!items?.length) return null
  return (
    <span className="flex min-w-0 flex-wrap items-center gap-x-2 text-[11px] text-muted">
      {label}
      {items.map(n => (
        <button key={n.symbol} type="button" onClick={() => onOpenStock?.(n.symbol)}
          className="inline-flex items-center gap-1 rounded px-1 py-0.5 hover:bg-elevated">
          <span className="text-foreground">{n.name}</span>
          {n.boards_today > 1 && <span className="font-mono text-bull">{n.boards_today}板</span>}
          {n.change_pct != null && <span className={cn('font-mono', priceColorClass(n.change_pct))}>{pct(n.change_pct)}</span>}
        </button>
      ))}
    </span>
  )
}

/** 风向标单行: 角色、名称、涨跌幅、状态、时间线摘要; detail 时右侧补充连板与封单。 */
export function BellwetherRow({ item, onOpenStock, detail = true }: { item: BellwetherItem; onOpenStock: OpenStock; detail?: boolean }) {
  const boards = boardsText(item)
  const extra = [boards, item.seal_vol != null ? `封单 ${item.seal_vol.toLocaleString()} 手` : null].filter(Boolean).join(' · ')
  return (
    <button
      type="button"
      onClick={() => onOpenStock?.(item.symbol)}
      title={item.roles.map(r => r.detail).join('；')}
      className="flex min-w-0 items-center gap-2 rounded px-1.5 py-1 text-left transition-colors hover:bg-elevated"
    >
      <span className="flex shrink-0 gap-1">
        {item.roles.map(r => (
          <span key={`${r.key}-${r.detail}`} className={cn('rounded border border-border px-1 text-[10px] leading-4',
            r.key === 'weak_leader' ? 'text-muted' : 'text-secondary')}>{r.label}</span>
        ))}
      </span>
      <span className="min-w-0 truncate text-foreground">{item.name}</span>
      <span className={cn('shrink-0 font-mono tabular-nums', priceColorClass(item.change_pct))}>{pct(item.change_pct)}</span>
      <StateTag state={item.state} />
      <TimelineHint t={item.timeline} />
      {detail && extra && <span className="ml-auto hidden min-w-0 shrink truncate text-[11px] text-muted sm:block">{extra}</span>}
    </button>
  )
}

export function PoolPerf({ label, perf }: { label: string; perf: BellwetherPoolPerf | null }) {
  if (!perf) return null
  return (
    <span>
      {label} <b className={cn('font-mono', priceColorClass(perf.avg_pct))}>{pct(perf.avg_pct)}</b>
      {' '}· 红盘 <b className="font-mono text-foreground">{(perf.up_ratio * 100).toFixed(0)}%</b>
      <span className="text-muted"> ({perf.count} 只)</span>
    </span>
  )
}
