import { useState } from 'react'
import type { FirstBoardCandidate, FirstBoardPriority, FirstBoardPriorityStatus, FirstBoardRadarBoard } from '@/lib/api'
import { pct, stamp } from './shared'

const statusLabels: Record<FirstBoardPriorityStatus, string> = {
  ready: '可提示', partial: '可提示 · 缺部分证据', shadow: '强板块影子候选', watch: '等待临板',
  blocked: '门槛阻断', sealed: '已触及涨停价', broken: '已开板', invalid: '已失效', stale: '已过期',
}
const roleLabels = { leader: '板内领涨', follower: '跟随走强', ordinary: '一般成员', unknown: '角色待确认' }
const partLabels = { stock: '个股量价', sector: '板块强度', timing: '分时确认', popularity: '个股人气' } as const
export const priorityFilters = { all: '全部候选', actionable: '可提示有分', concept: '强概念／题材成员', industry: '强行业成员', watch: '观察有分', shadow: '强板块影子候选', incomplete: '有分但缺证据' } as const
export type PriorityFilter = keyof typeof priorityFilters

function rankMovement(gain: number | null | undefined) {
  if (gain == null) return ''
  if (gain === 0) return ' · 较前次持平'
  return ` · 较前次${gain > 0 ? '上升' : '下降'} ${Math.abs(gain)} 位`
}

function RadarBoard({ entry }: { entry?: FirstBoardRadarBoard }) {
  if (entry?.status !== 'ready') return null
  const kind = entry.plate_type && { 14: '行业', 15: '概念', 17: '题材' }[entry.plate_type]
  return <p>{entry.source === 'official_core' ? '官方核心股' : `供应商${kind || '板块'}榜`} · {entry.name} · 第 {entry.rank ?? '—'} 名
    <br />榜单更新 {stamp(entry.source_at)} · 获取 {stamp(entry.fetched_at)}
    {entry.members_at && <><br />成员核验 {stamp(entry.members_at)}</>}
  </p>
}

function vendorBoards(radar?: FirstBoardPriority['radar']) {
  const prior = radar?.plate ?? radar?.theme
  return radar?.plates ?? (prior?.source === 'vendor_plate' ? [prior] : [])
}

export function strongBoards(row: FirstBoardCandidate, kind: 'industry' | 'concept', current: boolean) {
  const boards = new Map<string, string>()
  if (!current || row.priority_score?.status === 'stale') return boards
  const context = row.attention_context
  if (context?.schema_version === 1 && context.membership_version && (context.status === 'ready' || context.status === 'partial')) {
    for (const group of context.groups) {
      if (group.kind === kind && group.peer_strong === true) boards.set(group.id, `${group.name} · ${group.source_id}`)
    }
  }
  for (const plate of vendorBoards(row.priority_score?.radar)) {
    if (plate.status !== 'ready' || plate.source !== 'vendor_plate' || plate.rank !== 1 || !plate.tid || !plate.name) continue
    if (kind === 'industry' ? plate.plate_type === 14 : plate.plate_type === 15 || plate.plate_type === 17) {
      boards.set(`zzshare:${plate.plate_type}:${plate.tid}`, `${plate.name} · 供应商榜首`)
    }
  }
  return boards
}

export function currentPriority(row: Pick<FirstBoardCandidate, 'priority_score'>, current: boolean) {
  const p = row.priority_score
  return current && p?.version === 'priority-v1' && ['ready', 'partial', 'watch', 'shadow'].includes(p.status)
    && typeof p.score === 'number' && Number.isFinite(p.score) ? p.score : null
}

export function matchesPriority(row: FirstBoardCandidate, filter: PriorityFilter, current: boolean) {
  if (filter === 'all') return true
  const p = row.priority_score
  if (!p || currentPriority(row, current) === null) return false
  if (filter === 'actionable') return row.can_buy && p.can_buy
  if (filter === 'incomplete') return p.known_max < 100
  return p.status === filter
}

export function PriorityCell({ row, current, archived = false }: {
  row: Pick<FirstBoardCandidate, 'priority_score' | 'last_priority'>; current: boolean; archived?: boolean
}) {
  const [expanded, setExpanded] = useState(false)
  const p = row.priority_score
  const previous = row.last_priority
  const score = currentPriority(row, current)
  if (!p || p.version !== 'priority-v1') return <span className="text-secondary">暂无评分</span>
  const status = current ? statusLabels[p.status] : '非当前评分'
  const group = p.primary_sector
  const popularity = p.radar?.popularity
  const theme = p.radar?.theme
  const plate = p.radar?.plate
  const radarName = theme?.name || plate?.name
  const scored = score !== null
  return <div data-priority-cell className="min-w-36 max-w-64 space-y-1 leading-relaxed">
    {scored && <strong className="text-sm tabular-nums">{archived && '信号时优先分 '}{score} 分</strong>}
    <p className="text-secondary">{archived && '记录状态 · '}{status}</p>
    {scored && <>
      <p>{group?.name ?? (radarName ? `雷达 · ${radarName}` : '板块待确认')}{group && ` · ${roleLabels[p.role]}`}</p>
      {group?.peer_persistent && <p>同行持续强势</p>}
      <p className="tabular-nums text-secondary">证据覆盖 {p.known_max} / 100</p>
    </>}
    {!scored && previous && <p className="tabular-nums text-secondary">{previous.source === 'event' ? '最近信号评分' : '最近盘中评分'} {stamp(previous.as_of)} · {previous.score} 分</p>}
    {!scored && current && p.reason && <p className="text-secondary">{p.reason}</p>}
    {scored && <details onToggle={e => setExpanded(e.currentTarget.open)} className="text-secondary">
      <summary className="cursor-pointer rounded focus-visible:outline focus-visible:outline-accent">评分依据</summary>
      {expanded && <div className="mt-2 space-y-2">
        <dl className="space-y-1.5">{(Object.keys(partLabels) as (keyof typeof partLabels)[]).map(key => {
          const part = p.parts[key]
          return <div key={key}><dt className="font-medium text-foreground">{partLabels[key]}　{part?.points ?? '—'} / {part?.max_points ?? '—'}</dt><dd>{part?.reason ?? '缺少当时证据'}</dd></div>
        })}</dl>
        {group && <p>{group.kind === 'industry' ? '行业' : '概念'} · {group.source_id}<br />剔除本股后排名 {group.peer_rank ?? '—'} / {group.peer_total ?? '—'}，覆盖 {group.peer_valid_count ?? '—'} / {group.peer_count ?? '—'}<br />平均涨幅 {pct(group.peer_mean_change_pct)} · 上涨占比 {pct(group.peer_up_ratio)}</p>}
        {group && <p>{group.peer_persistent == null ? '同行持续性待后续观测' : group.peer_persistent ?
          `持续强势参考时点 ${stamp(group.peer_reference_at)}` : '当前未持续强势'}</p>}
        {popularity?.status === 'ready' ? <p>同花顺当时人气第 {popularity.rank} 名{rankMovement(popularity.rank_gain)}<br />榜单更新 {stamp(popularity.source_at)} · 获取 {stamp(popularity.fetched_at)}</p>
          : <p>{popularity?.status === 'unranked' ? '当时完整人气榜未上榜' : '当时人气榜待确认'}{popularity?.fetched_at && ` · 获取 ${stamp(popularity.fetched_at)}`}</p>}
        {theme?.source === 'official_core' && <RadarBoard entry={theme} />}
        {vendorBoards(p.radar).map(entry => <RadarBoard key={`${entry.plate_type}-${entry.tid}`} entry={entry} />)}
        <p>{p.reason}</p>
      </div>}
    </details>}
  </div>
}
