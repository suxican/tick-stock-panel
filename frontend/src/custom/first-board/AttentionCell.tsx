import { useState } from 'react'
import type { FirstBoardAttention } from '@/lib/api'
import { cn } from '@/lib/cn'
import { pct, positiveBadgeClass, stamp } from './shared'

export type AttentionFilter = 'all' | 'strong' | 'follow' | 'both'

const badgeClass = 'inline-flex items-center whitespace-nowrap rounded border px-1.5 py-0.5 font-medium'
// 联动命中属于正向信号，统一用蓝色；双条件用实心加强，单项用描边。
const colors = {
  strong: 'border-accent/40 text-blue-700 dark:text-blue-300',
  follow: 'border-accent/40 text-blue-700 dark:text-blue-300',
  both: positiveBadgeClass,
}
const leaderStatus = (atLimit: boolean | null) => atLimit == null ? '触板状态待确认' : atLimit ? '触板' : '已开板'

export function hasCurrentAttention(context: FirstBoardAttention | undefined, snapshotCurrent = true): context is FirstBoardAttention {
  return snapshotCurrent && context?.schema_version === 1 && (context.status === 'ready' || context.status === 'partial')
}

export function matchesAttention(context: FirstBoardAttention | undefined, filter: AttentionFilter, snapshotCurrent: boolean) {
  if (filter === 'all') return true
  if (!hasCurrentAttention(context, snapshotCurrent)) return false
  if (filter === 'strong') return context.strong_sector === true
  if (filter === 'follow') return context.leader_follow === true
  // 两个不同概念分别满足 S 和 F，不能在前端合并成同板块双条件。
  return context.group === 'S1F1'
}

export function attentionPriority(context: FirstBoardAttention | undefined) {
  return hasCurrentAttention(context) && context.priority != null && [0, 1, 2].includes(context.priority) ? context.priority : null
}

/** 列表内只显示命中的联动标签或一句状态；板块、领涨股和分钟证据在行展开区查看。 */
export function AttentionSummary({ context, snapshotCurrent }: { context?: FirstBoardAttention; snapshotCurrent: boolean }) {
  const stale = !!context && (!snapshotCurrent || context.status === 'stale')
  if (!hasCurrentAttention(context, snapshotCurrent)) {
    return <span data-attention-summary className="text-secondary">{stale ? '联动数据已过期' : '联动数据待确认'}</span>
  }
  const badges: [keyof typeof colors, string][] = context.group === 'S1F1' ? [['both', '同板块双条件']] : [
    ...(context.strong_sector ? [['strong', '强势板块'] as [keyof typeof colors, string]] : []),
    ...(context.leader_follow ? [['follow', '领涨带动'] as [keyof typeof colors, string]] : []),
  ]
  if (context.intraday_rising) badges.push(['strong', '分时走强'])
  const unknown = context.strong_sector == null || context.leader_follow == null
  return <div data-attention-summary className="flex flex-wrap gap-1">
    {badges.map(([tone, label]) => <span key={label} data-attention-positive="true" className={cn(badgeClass, colors[tone])}>{label}</span>)}
    {!context.strong_sector && !context.leader_follow && <span className="text-secondary">{unknown ? '待确认' : '未满足'}</span>}
  </div>
}

export function AttentionCell({ context, snapshotCurrent }: { context?: FirstBoardAttention; snapshotCurrent: boolean }) {
  const [evidenceOpen, setEvidenceOpen] = useState(false)
  const current = hasCurrentAttention(context, snapshotCurrent)
  const stale = !!context && (!snapshotCurrent || context.status === 'stale')
  const matched = current ? context.groups.filter(group => group.strong || group.follow || group.leader).sort((a, b) => {
    const priority = (group: typeof a) => group.strong && group.follow ? 2 : group.strong || group.follow ? 1 : 0
    return priority(b) - priority(a)
  }) : []
  const shownGroups = matched.slice(0, 2)
  const unknown = !context || context.strong_sector == null || context.leader_follow == null

  return <div data-attention-cell className="min-w-60 max-w-80 space-y-2 break-words leading-relaxed">
    {current ? <>
      <div className="flex flex-wrap gap-1.5">
        {context.group === 'S1F1' ? <span data-attention-positive="true" className={cn(badgeClass, colors.both)}>同板块双条件</span> : <>
          {context.strong_sector && <span data-attention-positive="true" className={cn(badgeClass, colors.strong)}>强势板块</span>}
          {context.leader_follow && <span data-attention-positive="true" className={cn(badgeClass, colors.follow)}>领涨带动</span>}
        </>}
        {context.intraday_rising ? <span data-attention-positive="true" className={cn(badgeClass, colors.both)}>分时走强</span> : <span className="text-secondary">{context.intraday_rising === false ? '分时未走强' : '分时数据不足'}</span>}
      </div>
      {shownGroups.map(group => <div key={group.id}>
        <p>{group.kind === 'industry' ? '行业' : '概念'} · {group.name}</p>
        {group.leader && <p className={cn('text-secondary', group.follow && 'text-foreground')}>{group.leader.name || group.leader.symbol} · {leaderStatus(group.leader.at_limit)}</p>}
      </div>)}
      {matched.length > shownGroups.length && <p className="text-secondary">另有 {matched.length - shownGroups.length} 个关联板块，见联动依据</p>}
      {context.strong_sector && context.leader_follow && context.group !== 'S1F1' && <p className="text-secondary">不同板块条件，未合并</p>}
      {!context.strong_sector && !context.leader_follow && <p className="text-secondary">{unknown ? '板块联动待确认' : '未达到重点关注条件'}</p>}
      {context.status === 'partial' && <p className="text-secondary">部分数据待确认</p>}
    </> : <p className="text-secondary">{stale ? '联动数据已过期' : '联动数据待确认'}</p>}
    {context?.schema_version === 1 && !!context.reasons.length && <p className="text-secondary">{context.reasons.join('；')}</p>}
    {context?.schema_version === 1 && <details className="text-secondary" onToggle={event => setEvidenceOpen(event.currentTarget.open)}>
      <summary className="cursor-pointer rounded focus-visible:outline focus-visible:outline-accent">联动依据{!current ? '（最近观测）' : ''}</summary>
      {evidenceOpen && <div className="mt-2 space-y-3 break-words">
        <p>观测 {stamp(context.as_of)} · 规则 {context.rule_version}<br />成分版本 {context.membership_version || '待确认'}</p>
        {context.groups.map(group => <div key={group.id} className="space-y-1">
          <p className="font-medium text-foreground">{group.kind === 'industry' ? '行业' : '概念'} · {group.name}</p>
          {group.peer_count != null ? <>
            <p>剔除本股后排名 {group.peer_rank ?? '—'} / {group.peer_total ?? '—'} · 覆盖 {group.peer_valid_count ?? '—'} / {group.peer_count}</p>
            <p>同行平均涨幅 {pct(group.peer_mean_change_pct)} · 上涨占比 {pct(group.peer_up_ratio)}</p>
            {group.peer_market_mean_change_pct != null && <p>同来源分类均值 {pct(group.peer_market_mean_change_pct)}</p>}
          </> : <>
            <p>排名 {group.rank ?? '—'} / {group.total ?? '—'} · 覆盖 {group.valid_count} / {group.member_count}（{pct(group.coverage)}）</p>
            <p>平均涨幅 {pct(group.mean_change_pct)} · 上涨占比 {pct(group.up_ratio)}</p>
            {group.market_mean_change_pct != null && <p>同来源分类均值 {pct(group.market_mean_change_pct)}</p>}
          </>}
          <p>{group.reason}</p>
          {group.leader && <p>领涨股 {group.leader.name || group.leader.symbol}（{group.leader.symbol}）· {leaderStatus(group.leader.at_limit)}<br />首次观测触板 {stamp(group.leader.observed_limit_at)}</p>}
          <p>来源 {group.source_id} · 分类 {group.field}</p>
        </div>)}
        <div className="space-y-1">
          <p>分钟截至 {stamp(context.intraday.as_of)}</p>
          <p>近 5 分钟 {pct(context.intraday.return_5m)} · 近 15 分钟 {pct(context.intraday.return_15m)}</p>
          <p>5分钟收盘价回撤 {pct(context.intraday.max_drawdown)} · {context.intraday.above_vwap == null ? '均价线待确认' : context.intraday.above_vwap ? '位于均价线上方' : '未在均价线上方'}</p>
          <p>{context.intraday.reason}</p>
        </div>
      </div>}
    </details>}
  </div>
}
