/**
 * 盘中风向标 —— 上一交易日选定的风向标, 对照当前行情给出规则结论。
 * 大盘: 情绪龙头、主线龙头与中军; 持仓/自选: 所属板块各自的龙头、中军与板块广度。
 * 名单盘中不重选; 与竞价的「盘前风向标」无关。比例字段为小数制, 展示时换算为百分数。
 */
import { useState, type ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Compass, RefreshCw } from 'lucide-react'
import {
  api,
  type BellwetherItem,
  type BellwetherPoolPerf,
  type BellwetherScope,
  type BellwetherSnapshot,
  type BellwetherState,
  type BellwetherVerdict,
  type UserBellwetherSnapshot,
} from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { cn } from '@/lib/cn'
import { priceColorClass } from '@/lib/format'

type OpenStock = ((symbol: string) => void) | undefined
type UserSector = UserBellwetherSnapshot['sectors'][number]

const SCOPES: { key: BellwetherScope; label: string }[] = [
  { key: 'market', label: '大盘' }, { key: 'holdings', label: '持仓' }, { key: 'watchlist', label: '自选' },
]
const SCOPE_STORAGE = 'review.bellwether.scope'

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
const RELATIVE_TEXT: Record<UserSector['mine'][number]['relative'], string> = {
  stronger: '跑赢', inline: '同步', weaker: '跑输', unknown: '',
}

function pct(v: number | null | undefined, digits = 2): string {
  if (v == null) return '—'
  return `${v > 0 ? '+' : ''}${(v * 100).toFixed(digits)}%`
}

function readScope(): BellwetherScope {
  try {
    const saved = localStorage.getItem(SCOPE_STORAGE)
    return SCOPES.some(s => s.key === saved) ? (saved as BellwetherScope) : 'market'
  } catch {
    return 'market'
  }
}

function boardsText(item: BellwetherItem): string | null {
  if (item.boards_prev == null || item.boards_prev < 1) return null
  if (item.boards_today == null) return `昨 ${item.boards_prev} 板`
  // 盘中未封板不等于晋级失败, 只陈述当前事实
  return `昨 ${item.boards_prev} 板 → ${item.boards_today > item.boards_prev ? `今 ${item.boards_today} 板` : '今未封板'}`
}

function StateTag({ state }: { state: BellwetherState }) {
  return <span className={cn('shrink-0 rounded px-1.5 text-[10px] leading-4', STATE_CLS[state])}>{STATE_TEXT[state]}</span>
}

function VerdictTag({ verdict }: { verdict: BellwetherVerdict }) {
  return (
    <span className={cn('shrink-0 rounded border px-1.5 text-[11px] font-medium leading-5', VERDICT_CLS[verdict.key])}>
      {verdict.label}
    </span>
  )
}

function Row({ item, onOpenStock, detail = true }: { item: BellwetherItem; onOpenStock: OpenStock; detail?: boolean }) {
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
      {detail && extra && <span className="ml-auto hidden min-w-0 shrink truncate text-[11px] text-muted sm:block">{extra}</span>}
    </button>
  )
}

function Perf({ label, perf }: { label: string; perf: BellwetherPoolPerf | null }) {
  if (!perf) return null
  return (
    <span>
      {label} <b className={cn('font-mono', priceColorClass(perf.avg_pct))}>{pct(perf.avg_pct)}</b>
      {' '}· 红盘 <b className="font-mono text-foreground">{(perf.up_ratio * 100).toFixed(0)}%</b>
      <span className="text-muted"> ({perf.count} 只)</span>
    </span>
  )
}

function Notes({ items }: { items: string[] }) {
  return (
    <details className="text-[11px] text-muted">
      <summary className="cursor-pointer select-none hover:text-secondary">口径说明</summary>
      <ul className="mt-1 list-disc space-y-0.5 pl-4">
        {items.map(note => <li key={note}>{note}</li>)}
      </ul>
    </details>
  )
}

function MarketView({ data, onOpenStock }: { data: BellwetherSnapshot; onOpenStock: OpenStock }) {
  const { ladder, feedback } = data
  return (
    <>
      {data.items.length ? (
        <div className="grid grid-cols-1 gap-x-4 lg:grid-cols-2">
          {data.items.map(item => <Row key={item.symbol} item={item} onOpenStock={onOpenStock} />)}
        </div>
      ) : (
        <div className="text-[11px] text-muted">上一交易日没有 2 板以上的连板股, 也没有可用的主线龙头</div>
      )}
      <div className="flex flex-wrap gap-x-3 gap-y-0.5 border-t border-border/40 pt-1.5 text-[11px] text-secondary">
        <Perf label="昨日涨停今日" perf={feedback.limit_up} />
        <Perf label="昨日炸板今日" perf={feedback.broken} />
        {ladder?.seal_rate != null && <span>封板率 <b className="font-mono text-foreground">{(ladder.seal_rate * 100).toFixed(0)}%</b></span>}
        {ladder?.promo_rate != null && <span>晋级率 <b className="font-mono text-foreground">{(ladder.promo_rate * 100).toFixed(0)}%</b></span>}
      </div>
      <Notes items={[
        `名单只用 ${data.reference_date?.slice(5)} 收盘数据选出, 盘中不重选; 今天新走强的股票不会顶替。`,
        `情绪 = 昨日最高连板(同高度取成交额最大); 龙头/中军来自昨日涨停主线前 ${data.mainlines.length || 3} 名。`,
        '涨跌幅达到 ±3% 归为强势/走弱; 两类以上走弱为退潮, 一类走弱为分歧, 无走弱且两类以上走强为进攻, 无走弱但走强不足两类为震荡。中军须昨日红盘。阈值为实验门槛。',
        ...data.notes,
      ]} />
    </>
  )
}

function SectorBlock({ sector, onOpenStock }: { sector: UserSector; onOpenStock: OpenStock }) {
  const b = sector.breadth
  return (
    <div className="space-y-0.5 border-t border-border/40 pt-1.5 first:border-t-0 first:pt-0">
      <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-0.5">
        <span className="shrink-0 rounded bg-elevated px-1 text-[10px] leading-4 text-muted">{sector.kind === 'concept' ? '概念' : '行业'}</span>
        <span className="min-w-0 truncate font-medium text-foreground" title={sector.name}>{sector.name}</span>
        {sector.mainline_rank != null && <span className="shrink-0 text-[10px] text-accent">主线第 {sector.mainline_rank}</span>}
        <VerdictTag verdict={sector.verdict} />
        <span className="hidden min-w-0 flex-1 truncate text-[11px] text-secondary md:block" title={sector.verdict.reason}>{sector.verdict.reason}</span>
        <span className="ml-auto shrink-0 text-[11px] text-secondary">
          {b ? (
            <>
              <span title={b.basis === 'active' ? `近 5 日涨停过的 ${b.size} 只活跃成员` : `全体 ${b.size} 只成员`}>
                {b.basis === 'active' ? '活跃均涨' : '均涨'}
              </span>{' '}
              <b className={cn('font-mono', priceColorClass(b.avg_pct))}>{pct(b.avg_pct)}</b>
              {' '}· 红盘 <b className="font-mono text-foreground">{(b.up_ratio * 100).toFixed(0)}%</b>
              {b.limit_up > 0 && <> · 涨停 <b className="font-mono text-bull">{b.limit_up}</b></>}
              {b.coverage < 0.8 && <span className="text-warning"> · 覆盖不足</span>}
            </>
          ) : <span className="text-muted">板块行情待确认</span>}
        </span>
      </div>
      <div className="flex flex-wrap items-center gap-x-3">
        {sector.items.map(item => <Row key={item.symbol} item={item} onOpenStock={onOpenStock} detail={false} />)}
        {sector.mine.length > 0 && (
          <span className="flex min-w-0 flex-wrap items-center gap-x-2 text-[11px] text-muted">
            我的
            {sector.mine.map(m => (
              <button key={m.symbol} type="button" onClick={() => onOpenStock?.(m.symbol)}
                className="inline-flex items-center gap-1 rounded px-1 py-0.5 hover:bg-elevated">
                <span className="text-foreground">{m.name}</span>
                <span className={cn('font-mono', priceColorClass(m.change_pct))}>{pct(m.change_pct)}</span>
                {RELATIVE_TEXT[m.relative] && (
                  <span className={m.relative === 'stronger' ? 'text-bull' : m.relative === 'weaker' ? 'text-bear' : 'text-muted'}>
                    {RELATIVE_TEXT[m.relative]}
                  </span>
                )}
              </button>
            ))}
          </span>
        )}
      </div>
    </div>
  )
}

function UserView({ data, onOpenStock }: { data: UserBellwetherSnapshot; onOpenStock: OpenStock }) {
  const uncovered = data.uncovered
  return (
    <>
      <div className="space-y-1.5">
        {data.sectors.map(sector => <SectorBlock key={`${sector.kind}-${sector.name}`} sector={sector} onOpenStock={onOpenStock} />)}
      </div>
      {uncovered.length > 0 && (
        <div className="truncate border-t border-border/40 pt-1.5 text-[11px] text-muted" title={uncovered.map(u => u.name).join('、')}>
          {uncovered.length}/{data.total} 只未覆盖: {uncovered.slice(0, 8).map(u => u.name).join('、')}{uncovered.length > 8 ? ' 等' : ''}
        </div>
      )}
      <Notes items={[
        `板块与名单只用 ${data.reference_date?.slice(5)} 收盘数据选出, 盘中不重选。`,
        '每只股票取所属概念 2 个、行业 1 个, 按昨日主线名次、涨停家数、均涨排序, 合计最多 8 个板块; 已过滤宽基标签。',
        '龙头 = 昨日板块内连板最高; 昨日无涨停时只给弱龙头, 不参与结论。中军 = 昨日板块内红盘股中成交额最大。',
        '板块广度优先只算近 5 日涨停过的活跃成员(不足 5 只时用全体成员, 显示为「均涨」): 均涨 ≥1% 且红盘 ≥60% 为走强, 均涨为负或红盘 <40% 为走弱; 有效行情覆盖不足 80% 时不判定。本股相对同一口径的板块均涨 ±1% 以内为同步。四档结论同大盘。阈值为实验门槛。',
        ...data.notes,
      ]} />
    </>
  )
}

function timeText(data: BellwetherSnapshot | UserBellwetherSnapshot): string {
  if (data.live && data.quote_time) return `行情 ${data.quote_time.slice(11, 16)}`
  return data.as_of ? `${data.as_of.slice(5)} ${data.live ? '盘中' : '非交易时段'}` : ''
}

export function BellwetherCard({ onOpenStock }: { onOpenStock?: (symbol: string) => void }) {
  const [scope, setScopeState] = useState<BellwetherScope>(readScope)
  const [groupId, setGroupId] = useState<string | null>(null)
  const setScope = (next: BellwetherScope) => {
    setScopeState(next)
    try { localStorage.setItem(SCOPE_STORAGE, next) } catch { /* 隐私模式下不记忆 */ }
  }
  const groups = useQuery({
    queryKey: QK.watchlistGroups,
    queryFn: api.watchlistGroups,
    enabled: scope === 'watchlist',
    staleTime: 60_000,
  })
  const activeGroup = scope === 'watchlist' ? groupId : null
  const query = useQuery({
    queryKey: QK.reviewBellwethers(scope, activeGroup),
    queryFn: () => api.reviewBellwethers(scope, activeGroup),
    // 盘中跟随行情刷新; 非交易时段数据静止, 只需低频确认是否开盘
    refetchInterval: (q) => (q.state.data?.live ? 15_000 : 60_000),
    staleTime: 10_000,
  })
  // 切换范围时不展示上一范围的结果
  const data = query.data && query.data.scope === scope ? query.data : undefined

  const header = (
    <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
      <Compass className="h-3.5 w-3.5 shrink-0 text-accent" />
      <span className="shrink-0 text-muted">盘中风向标</span>
      <div className="flex shrink-0 rounded-btn border border-border p-px text-[11px]" role="tablist">
        {SCOPES.map(s => (
          <button key={s.key} type="button" role="tab" aria-selected={scope === s.key} onClick={() => setScope(s.key)}
            className={cn('rounded px-1.5 leading-5 transition-colors',
              scope === s.key ? 'bg-accent/15 text-accent' : 'text-secondary hover:text-foreground')}>
            {s.label}
          </button>
        ))}
      </div>
      {scope === 'watchlist' && (groups.data?.groups.length ?? 0) > 0 && (
        <select value={groupId ?? ''} onChange={e => setGroupId(e.target.value || null)} aria-label="自选分组"
          className="max-w-[8rem] shrink-0 rounded-btn border border-border bg-elevated px-1 text-[11px] text-secondary outline-none">
          <option value="">全部自选</option>
          {groups.data!.groups.map(g => <option key={g.id} value={g.id}>{g.name}</option>)}
        </select>
      )}
      {data?.status === 'ready' && data.scope === 'market' && (
        <>
          <VerdictTag verdict={data.verdict} />
          <span className="min-w-0 flex-1 truncate text-secondary" title={data.verdict.reason}>{data.verdict.reason}</span>
        </>
      )}
      {data?.status === 'ready' && (
        <span className={cn('ml-auto shrink-0 text-[11px]', data.stale ? 'text-warning' : 'text-muted')}>{timeText(data)}</span>
      )}
    </div>
  )

  let body: ReactNode
  if (query.isLoading || (!data && query.isFetching)) {
    body = <div className="text-[11px] text-muted">加载中…</div>
  } else if (!data) {
    body = (
      <div className="flex items-center gap-2 text-[11px] text-muted">
        加载失败
        <button
          type="button"
          onClick={() => query.refetch()}
          disabled={query.isFetching}
          className="inline-flex items-center gap-1 rounded-btn border border-border bg-elevated px-2 py-0.5 text-secondary transition-colors hover:text-foreground disabled:opacity-50"
        >
          <RefreshCw className={cn('h-3 w-3', query.isFetching && 'animate-spin')} />重试
        </button>
      </div>
    )
  } else if (data.status !== 'ready') {
    body = <div className="text-[11px] text-muted">{data.reason}</div>
  } else if (data.scope === 'market') {
    body = <MarketView data={data} onOpenStock={onOpenStock} />
  } else {
    body = <UserView data={data} onOpenStock={onOpenStock} />
  }

  return (
    <div className="space-y-1.5 rounded-card border border-border bg-surface/80 px-3.5 py-2 text-[12px]">
      {header}
      {body}
    </div>
  )
}
