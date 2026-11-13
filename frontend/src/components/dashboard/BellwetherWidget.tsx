/**
 * 看板「风向标」组件 —— 复盘页风向标的紧凑版。
 * 自取数据(与复盘页共用查询键); 范围选择存在组件实例参数里, 随布局保存。
 * 口径说明、分组筛选、封单等细节留在复盘页, 由右上角入口跳转。
 */
import type { ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { ArrowUpRight, Compass, RefreshCw } from 'lucide-react'
import type { BellwetherScope, BellwetherSnapshot, UserBellwetherSnapshot } from '@/lib/api'
import { cn } from '@/lib/cn'
import { priceColorClass } from '@/lib/format'
import {
  BellwetherRow,
  ChangeHint,
  isBellwetherScope,
  Newcomers,
  pct,
  RELATIVE_TEXT,
  relativeClass,
  ScopeTabs,
  TimelineHint,
  timeText,
  useBellwetherSnapshot,
  VerdictTag,
  type UserSector,
} from '@/components/bellwether/parts'

type Open = (symbol: string) => void

function MarketBody({ data, open }: { data: BellwetherSnapshot; open: Open }) {
  const { feedback, ladder } = data
  const tail = [
    feedback.limit_up ? `昨涨停今日 ${pct(feedback.limit_up.avg_pct)} 红盘${(feedback.limit_up.up_ratio * 100).toFixed(0)}%` : null,
    ladder?.seal_rate != null ? `封板率 ${(ladder.seal_rate * 100).toFixed(0)}%` : null,
    ladder?.promo_rate != null ? `晋级率 ${(ladder.promo_rate * 100).toFixed(0)}%` : null,
  ].filter(Boolean).join(' · ')
  return (
    <>
      <div className="flex min-w-0 items-center gap-1.5">
        <VerdictTag verdict={data.verdict} />
        <span className="min-w-0 flex-1 truncate text-[11px] text-secondary" title={data.verdict.reason}>{data.verdict.reason}</span>
        <ChangeHint change={data.verdict_change} />
      </div>
      <div className="flex flex-col">
        {data.items.length
          ? data.items.map(item => <BellwetherRow key={item.symbol} item={item} onOpenStock={open} detail={false} />)
          : <div className="py-1 text-[11px] text-muted">上一交易日没有 2 板以上的连板股, 也没有可用的主线龙头</div>}
      </div>
      <Newcomers label="新晋" items={data.newcomers} onOpenStock={open} />
      {tail && <div className="truncate border-t border-border/40 pt-1 text-[11px] text-secondary" title={tail}>{tail}</div>}
    </>
  )
}

function SectorRows({ sector, open }: { sector: UserSector; open: Open }) {
  const b = sector.breadth
  return (
    <div className="border-t border-border/40 pt-1 first:border-t-0 first:pt-0">
      {/* 组件可被缩到很窄, 标题行允许换行而不是溢出 */}
      <div className="flex min-w-0 flex-wrap items-center gap-x-1.5 gap-y-0.5">
        <span className="min-w-0 truncate font-medium text-foreground" title={sector.name}>{sector.name}</span>
        {sector.mainline_rank != null && <span className="shrink-0 text-[10px] text-accent">主线{sector.mainline_rank}</span>}
        <VerdictTag verdict={sector.verdict} />
        <ChangeHint change={sector.verdict_change} />
        <span className="ml-auto shrink-0 text-[11px] text-secondary">
          {b ? (
            <>
              {b.basis === 'active' ? '活跃 ' : ''}
              <span className={cn('font-mono', priceColorClass(b.avg_pct))}>{pct(b.avg_pct)}</span>
              {' '}红{(b.up_ratio * 100).toFixed(0)}%
            </>
          ) : <span className="text-muted">待确认</span>}
        </span>
      </div>
      <div className="flex flex-col">
        {sector.items.map(item => <BellwetherRow key={item.symbol} item={item} onOpenStock={open} detail={false} />)}
      </div>
      {sector.mine.length > 0 && (
        <div className="flex min-w-0 flex-wrap items-center gap-x-2 px-1.5 text-[11px] text-muted">
          我的
          {sector.mine.map(m => (
            <button key={m.symbol} type="button" onClick={() => open(m.symbol)}
              className="inline-flex items-center gap-1 rounded px-0.5 hover:bg-elevated">
              <span className="text-foreground">{m.name}</span>
              <span className={cn('font-mono', priceColorClass(m.change_pct))}>{pct(m.change_pct)}</span>
              {RELATIVE_TEXT[m.relative] && <span className={relativeClass(m.relative)}>{RELATIVE_TEXT[m.relative]}</span>}
              <TimelineHint t={m.timeline} />
            </button>
          ))}
        </div>
      )}
    </div>
  )
}

function UserBody({ data, open }: { data: UserBellwetherSnapshot; open: Open }) {
  const covered = data.total - data.uncovered.length
  return (
    <>
      <div className="flex flex-col gap-1">
        {data.sectors.map(sector => <SectorRows key={`${sector.kind}-${sector.name}`} sector={sector} open={open} />)}
      </div>
      <div className="truncate border-t border-border/40 pt-1 text-[11px] text-muted"
        title={data.uncovered.map(u => u.name).join('、')}>
        {covered} 只覆盖 {data.sectors.length} 个板块{data.uncovered.length > 0 ? ` · ${data.uncovered.length} 只未覆盖` : ''}
      </div>
    </>
  )
}

/** 当前视图里所有可点的股票, 供个股详情弹窗上下切换。 */
function navList(data: BellwetherSnapshot | UserBellwetherSnapshot | undefined): { symbol: string; name?: string }[] {
  if (!data || data.status !== 'ready') return []
  const entries = data.scope === 'market'
    ? [...data.items, ...(data.newcomers ?? [])]
    : data.sectors.flatMap(s => [...s.items, ...s.mine])
  const seen = new Set<string>()
  return entries.filter(e => !seen.has(e.symbol) && seen.add(e.symbol)).map(e => ({ symbol: e.symbol, name: e.name }))
}

export function BellwetherWidget({ scope: rawScope, isLatestDate, onScopeChange, onOpenStock }: {
  scope?: string
  /** 看板选中的是最新交易日; 风向标只提供当日数据 */
  isLatestDate: boolean
  onScopeChange: (scope: BellwetherScope) => void
  onOpenStock: (symbol: string, name: string | undefined, navList: { symbol: string; name?: string }[]) => void
}) {
  const scope: BellwetherScope = isBellwetherScope(rawScope) ? rawScope : 'market'
  const { query, data } = useBellwetherSnapshot(scope, null, isLatestDate)
  const nav = navList(data)
  const open: Open = symbol => onOpenStock(symbol, nav.find(n => n.symbol === symbol)?.name, nav)

  let body: ReactNode
  if (!isLatestDate) {
    body = <div className="text-[11px] text-muted">风向标仅提供当日数据, 切回最新交易日后查看</div>
  } else if (query.isLoading || (!data && query.isFetching)) {
    body = <div className="text-[11px] text-muted">加载中…</div>
  } else if (!data) {
    body = (
      <div className="flex items-center gap-2 text-[11px] text-muted">
        加载失败
        <button type="button" onClick={() => query.refetch()} disabled={query.isFetching}
          className="inline-flex items-center gap-1 rounded-btn border border-border bg-elevated px-2 py-0.5 text-secondary hover:text-foreground disabled:opacity-50">
          <RefreshCw className={cn('h-3 w-3', query.isFetching && 'animate-spin')} />重试
        </button>
      </div>
    )
  } else if (data.status !== 'ready') {
    body = <div className="text-[11px] text-muted">{data.reason}</div>
  } else if (data.scope === 'market') {
    body = <MarketBody data={data} open={open} />
  } else {
    body = <UserBody data={data} open={open} />
  }

  return (
    <section className="flex h-full flex-col overflow-hidden rounded-card border border-border bg-surface/80 p-1.5 text-[12px] shadow-[0_1px_2px_hsl(var(--border)/0.4)] backdrop-blur-sm transition-shadow hover:shadow-[0_2px_8px_hsl(var(--border)/0.5)]">
      <div className="mb-1.5 flex min-w-0 items-center gap-1.5">
        <Compass className="h-3.5 w-3.5 shrink-0 text-accent" />
        <h2 className="shrink-0 text-xs font-semibold text-foreground">风向标</h2>
        <ScopeTabs scope={scope} onChange={onScopeChange} />
        {isLatestDate && data?.status === 'ready' && (
          <span className={cn('ml-auto truncate font-mono text-[10px]', data.stale ? 'text-warning' : 'text-muted')}>{timeText(data)}</span>
        )}
        <Link to="/review" title="在复盘页查看完整风向标与口径说明" aria-label="在复盘页查看完整风向标"
          className={cn('inline-flex h-5 w-5 shrink-0 items-center justify-center rounded text-muted transition-colors hover:bg-accent/10 hover:text-accent',
            !(isLatestDate && data?.status === 'ready') && 'ml-auto')}>
          <ArrowUpRight className="h-3.5 w-3.5" />
        </Link>
      </div>
      <div className="min-h-0 flex-1 space-y-1 overflow-y-auto">{body}</div>
    </section>
  )
}
