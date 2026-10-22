import { useState } from 'react'
import type { FirstBoardAttention, FirstBoardSnapshot } from '@/lib/api'
import { cn } from '@/lib/cn'
import { pct, stamp } from './shared'

export type AttentionFilter = 'all' | 'strong' | 'follow' | 'both'

const badgeClass = 'inline-flex items-center rounded border px-1.5 py-0.5 font-medium'
const colors = {
  strong: 'border-cyan-500/25 bg-cyan-500/10 text-cyan-800 dark:text-cyan-300',
  follow: 'border-amber-500/25 bg-amber-500/10 text-amber-800 dark:text-amber-300',
  both: 'border-violet-500/25 bg-violet-500/10 text-violet-800 dark:text-violet-300',
}
const leaderStatus = (atLimit: boolean | null) => atLimit == null ? '触板状态待确认' : atLimit ? '触板' : '已开板'

export function AttentionSampling({ sampling }: { sampling: FirstBoardSnapshot['attention_sampling'] }) {
  if (!sampling) return null
  const labels = { recorded: '研究采样已记录', already_recorded: '本时点已采样', not_due: '等待下一采样时点', paused: '研究采样暂停', error: '研究采样暂不可用' }
  return <p className="text-xs leading-relaxed text-secondary" role="status">
    {labels[sampling.status]} · 今日采样时点 {sampling.sample_count} · 最近采样 {stamp(sampling.last_sample_at)}
    {sampling.reason && <span className="block">{sampling.reason}</span>}
  </p>
}

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
        {group.leader && <p className={cn('text-secondary', group.follow && 'text-amber-800 dark:text-amber-300')}>{group.leader.name || group.leader.symbol} · {leaderStatus(group.leader.at_limit)}</p>}
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
          <p>排名 {group.rank ?? '—'} / {group.total ?? '—'} · 覆盖 {group.valid_count} / {group.member_count}（{pct(group.coverage)}）</p>
          <p>平均涨幅 {pct(group.mean_change_pct)} · 上涨占比 {pct(group.up_ratio)}</p>
          {group.market_mean_change_pct != null && <p>同来源分类均值 {pct(group.market_mean_change_pct)}</p>}
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
