import type { FirstBoardCandidate } from '@/lib/api'
import { hasCurrentAttention } from './AttentionCell'

export interface ThemeGroup {
  key: string
  name: string
  kind: 'concept' | 'industry' | 'none'
  /** 命中昨日主线时的名次。 */
  mainlineRank: number | null
  rows: FirstBoardCandidate[]
  /** 组内不同股票数（同一只股票的多个形态只算一次）。 */
  stockCount: number
  /** 概念组才有；联动数据缺失或已过期时为 null。 */
  stats: { meanChangePct: number | null; upRatio: number | null; leader: { name: string; atLimit: boolean | null } | null } | null
}

interface Leader { name: string; atLimit: boolean | null; at: number }
/** 已在涨停的优先，其次触板时间更早的优先。 */
const better = (a: Leader, b: Leader) => (a.atLimit === true) !== (b.atLimit === true) ? a.atLimit === true : (a.at || Infinity) < (b.at || Infinity)

interface Theme { members: number | null; mainlineRank: number | null }

/** 候选可选的题材：联动上下文里的概念，加上命中的昨日主线概念（按名称识别）。 */
function themesOf(row: FirstBoardCandidate) {
  const themes = new Map<string, Theme>()
  for (const group of row.attention_context?.groups ?? []) {
    if (group.kind === 'concept') themes.set(group.name, { members: group.member_count ?? null, mainlineRank: null })
  }
  for (const match of row.mainline?.matches ?? []) {
    if (match.kind !== 'concept') continue
    const known = themes.get(match.name)
    themes.set(match.name, { members: known?.members ?? null, mainlineRank: Math.min(known?.mainlineRank ?? Infinity, match.rank) })
  }
  return themes
}

/**
 * 把候选按题材分组（每只候选只归属一个题材）。归属只用不随盘中波动的信息：
 * ① 显示的候选里共享该概念的不同股票更多；② 命中昨日主线（名次靠前优先）；③ 成分股更少；④ 名称。
 * 没有概念归到行业，再没有归到“未归类”。组按第一行出现的位置排序，组内保持传入顺序。
 * pinned 为 行键(`symbol-pattern`) → 题材键 的已有归属：只要该概念仍是候选的可选题材就沿用，
 * 这样盘中行进出导致共享数变化时，已显示的股票不会在组间跳动；新出现的行按规则归属一次。
 */
export function groupCandidatesByTheme(rows: FirstBoardCandidate[], current: boolean, pinned?: ReadonlyMap<string, string>): ThemeGroup[] {
  const themes = rows.map(themesOf)
  const sharers = new Map<string, Set<string>>()
  rows.forEach((row, index) => {
    for (const name of themes[index].keys()) sharers.set(name, (sharers.get(name) ?? new Set()).add(row.symbol))
  })
  const groups = new Map<string, ThemeGroup>()
  const leaders = new Map<string, Leader>()
  rows.forEach((row, index) => {
    const options = [...themes[index]].sort(([nameA, a], [nameB, b]) =>
      sharers.get(nameB)!.size - sharers.get(nameA)!.size
      || (a.mainlineRank ?? Infinity) - (b.mainlineRank ?? Infinity) || (a.members ?? Infinity) - (b.members ?? Infinity)
      || nameA.localeCompare(nameB, 'zh-CN'))
    const keptName = pinned?.get(`${row.symbol}-${row.pattern}`)?.replace(/^concept:/, '')
    const kept = keptName !== undefined && pinned?.get(`${row.symbol}-${row.pattern}`)?.startsWith('concept:') ? options.findIndex(([name]) => name === keptName) : -1
    if (kept > 0) options.unshift(...options.splice(kept, 1))
    const sector = row.sector?.trim()
    const [key, name, kind] = options.length ? [`concept:${options[0][0]}`, options[0][0], 'concept' as const]
      : sector ? [`industry:${sector}`, sector, 'industry' as const] : ['none', '未归类', 'none' as const]
    const group = groups.get(key) ?? { key, name, kind, mainlineRank: null, rows: [], stockCount: 0, stats: null }
    groups.set(key, group)
    group.rows.push(row)
    group.stockCount = new Set(group.rows.map(item => item.symbol)).size
    if (kind !== 'concept') return
    const own = options[0][1].mainlineRank
    if (own !== null) group.mainlineRank = Math.min(group.mainlineRank ?? Infinity, own)
    const found = hasCurrentAttention(row.attention_context, current)
      ? row.attention_context!.groups.find(item => item.kind === 'concept' && item.name === name) : undefined
    if (!found) return
    // 板块统计对所有成员相同，取第一份；领涨股则逐成员不同（后端剔除了该成员自己），需要在所有成员的版本里选最先涨停的。
    const leader = found.leader ? { name: found.leader.name || found.leader.symbol, atLimit: found.leader.at_limit, at: Date.parse(found.leader.observed_limit_at) } : null
    const known = leaders.get(key)
    if (leader && (!known || better(leader, known))) leaders.set(key, leader)
    if (group.stats === null) group.stats = { meanChangePct: found.mean_change_pct ?? null, upRatio: found.up_ratio ?? null, leader: null }
    const best = leaders.get(key)
    group.stats.leader = best ? { name: best.name, atLimit: best.atLimit } : null
  })
  return [...groups.values()]
}
