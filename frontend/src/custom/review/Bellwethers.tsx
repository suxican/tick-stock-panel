/**
 * 盘中风向标 —— 上一交易日选定的风向标, 对照当前行情给出规则结论。
 * 大盘: 情绪龙头、主线龙头与中军; 持仓/自选: 所属板块各自的龙头、中军与板块广度。
 * 名单盘中不重选; 与竞价的「盘前风向标」无关。共用界面见 components/bellwether,
 * 本文件为复盘页的详细视图(口径说明、分组筛选、封单等)。
 */
import { useState, type ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Compass, RefreshCw } from 'lucide-react'
import { api, type BellwetherScope, type BellwetherSnapshot, type UserBellwetherSnapshot } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { cn } from '@/lib/cn'
import { priceColorClass } from '@/lib/format'
import {
  BellwetherRow,
  ChangeHint,
  isBellwetherScope,
  Newcomers,
  pct,
  PoolPerf,
  RELATIVE_TEXT,
  relativeClass,
  ScopeTabs,
  TimelineHint,
  timeText,
  useBellwetherSnapshot,
  VerdictTag,
  type OpenStock,
  type UserSector,
} from '@/components/bellwether/parts'

const SCOPE_STORAGE = 'review.bellwether.scope'

function readScope(): BellwetherScope {
  try {
    const saved = localStorage.getItem(SCOPE_STORAGE)
    return isBellwetherScope(saved) ? saved : 'market'
  } catch {
    return 'market'
  }
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
          {data.items.map(item => <BellwetherRow key={item.symbol} item={item} onOpenStock={onOpenStock} />)}
        </div>
      ) : (
        <div className="text-[11px] text-muted">上一交易日没有 2 板以上的连板股, 也没有可用的主线龙头</div>
      )}
      <Newcomers label="新晋" items={data.newcomers} onOpenStock={onOpenStock} />
      <div className="flex flex-wrap gap-x-3 gap-y-0.5 border-t border-border/40 pt-1.5 text-[11px] text-secondary">
        <PoolPerf label="昨日涨停今日" perf={feedback.limit_up} />
        <PoolPerf label="昨日炸板今日" perf={feedback.broken} />
        {ladder?.seal_rate != null && <span>封板率 <b className="font-mono text-foreground">{(ladder.seal_rate * 100).toFixed(0)}%</b></span>}
        {ladder?.promo_rate != null && <span>晋级率 <b className="font-mono text-foreground">{(ladder.promo_rate * 100).toFixed(0)}%</b></span>}
      </div>
      <Notes items={[
        `名单只用 ${data.reference_date?.slice(5)} 收盘数据选出, 盘中不重选; 今天新走强的股票不会顶替。`,
        `情绪 = 昨日最高连板(同高度取成交额最大); 龙头/中军来自昨日涨停主线前 ${data.mainlines.length || 3} 名。`,
        '新晋 = 今天连板高度追平或超过昨日最高板、但不在名单里的股票, 可能是新周期的切换信号; 名单本身不变。',
        '首封、炸板次数与弱转强/强转弱(对比 15 分钟前)由后台在盘中按行情记录, 鼠标悬停可看完整变化; 后端重启后读回当日记录继续。',
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
        <ChangeHint change={sector.verdict_change} />
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
        {sector.items.map(item => <BellwetherRow key={item.symbol} item={item} onOpenStock={onOpenStock} detail={false} />)}
        {sector.mine.length > 0 && (
          <span className="flex min-w-0 flex-wrap items-center gap-x-2 text-[11px] text-muted">
            我的
            {sector.mine.map(m => (
              <button key={m.symbol} type="button" onClick={() => onOpenStock?.(m.symbol)}
                className="inline-flex items-center gap-1 rounded px-1 py-0.5 hover:bg-elevated">
                <span className="text-foreground">{m.name}</span>
                <span className={cn('font-mono', priceColorClass(m.change_pct))}>{pct(m.change_pct)}</span>
                {RELATIVE_TEXT[m.relative] && <span className={relativeClass(m.relative)}>{RELATIVE_TEXT[m.relative]}</span>}
                <TimelineHint t={m.timeline} />
              </button>
            ))}
          </span>
        )}
        <Newcomers label="新涨停" items={sector.new_limit_ups} onOpenStock={onOpenStock} />
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
        '新涨停 = 今天封板、但不在盘前名单和你的股票里的板块成员。首封、炸板与弱转强/强转弱由后台盘中记录。',
        '龙头 = 昨日板块内连板最高; 昨日无涨停时只给弱龙头, 不参与结论。中军 = 昨日板块内红盘股中成交额最大。',
        '板块广度优先只算近 5 日涨停过的活跃成员(不足 5 只时用全体成员, 显示为「均涨」): 均涨 ≥1% 且红盘 ≥60% 为走强, 均涨为负或红盘 <40% 为走弱; 有效行情覆盖不足 80% 时不判定。本股相对同一口径的板块均涨 ±1% 以内为同步。四档结论同大盘。阈值为实验门槛。',
        ...data.notes,
      ]} />
    </>
  )
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
  const { query, data } = useBellwetherSnapshot(scope, groupId)

  const header = (
    <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
      <Compass className="h-3.5 w-3.5 shrink-0 text-accent" />
      <span className="shrink-0 text-muted">盘中风向标</span>
      <ScopeTabs scope={scope} onChange={setScope} />
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
          <ChangeHint change={data.verdict_change} />
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
