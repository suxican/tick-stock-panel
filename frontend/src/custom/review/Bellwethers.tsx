/**
 * 盘中风向标 —— 上一交易日选定的情绪龙头、主线龙头与中军, 对照当前行情给出规则结论。
 * 名单盘中不重选; 与竞价的「盘前风向标」无关。比例字段为小数制, 展示时换算为百分数。
 */
import { useQuery } from '@tanstack/react-query'
import { Compass, RefreshCw } from 'lucide-react'
import { api, type BellwetherPoolPerf, type BellwetherSnapshot, type BellwetherState } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { cn } from '@/lib/cn'
import { priceColorClass } from '@/lib/format'

type Item = BellwetherSnapshot['items'][number]

const VERDICT_CLS: Record<BellwetherSnapshot['verdict']['key'], string> = {
  attack: 'border-bull/30 bg-bull/10 text-bull',
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

function pct(v: number | null | undefined, digits = 2): string {
  if (v == null) return '—'
  return `${v > 0 ? '+' : ''}${(v * 100).toFixed(digits)}%`
}

function boardsText(item: Item): string | null {
  if (item.boards_prev == null || item.boards_prev < 1) return null
  if (item.boards_today == null) return `昨 ${item.boards_prev} 板`
  // 盘中未封板不等于晋级失败, 只陈述当前事实
  return `昨 ${item.boards_prev} 板 → ${item.boards_today > item.boards_prev ? `今 ${item.boards_today} 板` : '今未封板'}`
}

function Row({ item, onOpenStock }: { item: Item; onOpenStock?: (symbol: string) => void }) {
  const boards = boardsText(item)
  return (
    <button
      type="button"
      onClick={() => onOpenStock?.(item.symbol)}
      title={item.roles.map(r => r.detail).join('；')}
      className="flex min-w-0 items-center gap-2 rounded px-1.5 py-1 text-left transition-colors hover:bg-elevated"
    >
      <span className="flex shrink-0 gap-1">
        {item.roles.map(r => (
          <span key={`${r.key}-${r.detail}`} className="rounded border border-border px-1 text-[10px] leading-4 text-secondary">{r.label}</span>
        ))}
      </span>
      <span className="min-w-0 truncate text-foreground">{item.name}</span>
      <span className={cn('shrink-0 font-mono tabular-nums', priceColorClass(item.change_pct))}>{pct(item.change_pct)}</span>
      <span className={cn('shrink-0 rounded px-1.5 text-[10px] leading-4', STATE_CLS[item.state])}>{STATE_TEXT[item.state]}</span>
      <span className="ml-auto hidden min-w-0 shrink truncate text-[11px] text-muted sm:block">
        {[boards, item.seal_vol != null ? `封单 ${item.seal_vol.toLocaleString()} 手` : null].filter(Boolean).join(' · ')}
      </span>
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

function timeText(data: BellwetherSnapshot): string {
  if (data.live && data.quote_time) return `行情 ${data.quote_time.slice(11, 16)}`
  return data.as_of ? `${data.as_of.slice(5)} ${data.live ? '盘中' : '非交易时段'}` : ''
}

export function BellwetherCard({ onOpenStock }: { onOpenStock?: (symbol: string) => void }) {
  const query = useQuery({
    queryKey: QK.reviewBellwethers,
    queryFn: () => api.reviewBellwethers(),
    // 盘中跟随行情刷新; 非交易时段数据静止, 只需低频确认是否开盘
    refetchInterval: (q) => (q.state.data?.live ? 15_000 : 60_000),
    staleTime: 10_000,
  })
  const data = query.data
  if (query.isLoading) return null

  const shell = 'rounded-card border border-border bg-surface/80 px-3.5 py-2 text-[12px]'
  const title = (
    <>
      <Compass className="h-3.5 w-3.5 shrink-0 text-accent" />
      <span className="shrink-0 text-muted">盘中风向标</span>
    </>
  )
  if (!data || data.status !== 'ready') {
    return (
      <div className={cn(shell, 'flex items-center gap-2')}>
        {title}
        <span className="min-w-0 flex-1 truncate text-muted">{data ? data.reason : '加载失败'}</span>
        {!data && (
          <button
            type="button"
            onClick={() => query.refetch()}
            disabled={query.isFetching}
            className="inline-flex shrink-0 items-center gap-1 rounded-btn border border-border bg-elevated px-2 py-0.5 text-[11px] text-secondary transition-colors hover:text-foreground disabled:opacity-50"
          >
            <RefreshCw className={cn('h-3 w-3', query.isFetching && 'animate-spin')} />重试
          </button>
        )}
      </div>
    )
  }

  const { verdict, ladder, feedback } = data
  return (
    <div className={cn(shell, 'space-y-1.5')}>
      <div className="flex items-center gap-2">
        {title}
        <span className={cn('shrink-0 rounded border px-1.5 text-[11px] font-medium leading-5', VERDICT_CLS[verdict.key])}>{verdict.label}</span>
        <span className="min-w-0 flex-1 truncate text-secondary" title={verdict.reason}>{verdict.reason}</span>
        <span className={cn('shrink-0 text-[11px]', data.stale ? 'text-warning' : 'text-muted')}>{timeText(data)}</span>
      </div>

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

      <details className="text-[11px] text-muted">
        <summary className="cursor-pointer select-none hover:text-secondary">口径说明</summary>
        <ul className="mt-1 list-disc space-y-0.5 pl-4">
          <li>名单只用 {data.reference_date?.slice(5)} 收盘数据选出, 盘中不重选; 今天新走强的股票不会顶替。</li>
          <li>情绪 = 昨日最高连板(同高度取成交额最大); 龙头/中军来自昨日涨停主线前 {data.mainlines.length || 3} 名。</li>
          <li>涨跌幅达到 ±3% 归为强势/走弱; 两类以上风向标走弱为退潮, 无走弱且两类以上走强为进攻, 其余为分歧。阈值为实验门槛。</li>
          {data.notes.map(note => <li key={note}>{note}</li>)}
        </ul>
      </details>
    </div>
  )
}
