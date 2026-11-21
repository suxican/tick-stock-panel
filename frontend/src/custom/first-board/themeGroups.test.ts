import { expect, it } from 'vitest'
import type { FirstBoardCandidate } from '@/lib/api'
import { groupCandidatesByTheme } from './themeGroups'

type Concept = { name: string; members?: number; mean?: number | null; up?: number | null; leader?: { name: string; at: boolean | null } | null }
function stock(symbol: string, concepts: Concept[], options: { pattern?: string; sector?: string | null; mainline?: [string, number][]; status?: string } = {}) {
  return {
    symbol, pattern: options.pattern ?? 'platform', sector: options.sector ?? null,
    attention_context: { schema_version: 1, status: options.status ?? 'ready', groups: concepts.map(item => ({
      id: `id-${item.name}`, kind: 'concept', name: item.name, member_count: item.members, mean_change_pct: item.mean ?? null, up_ratio: item.up ?? null,
      leader: item.leader ? { symbol: 'L', name: item.leader.name, at_limit: item.leader.at } : null })) },
    mainline: { hit: !!options.mainline?.length, date: '2026-11-20', reason: '', matches: (options.mainline ?? []).map(([name, rank]) => ({ kind: 'concept', name, rank })) },
  } as unknown as FirstBoardCandidate
}
const names = (groups: ReturnType<typeof groupCandidatesByTheme>) => groups.map(group => [group.name, group.rows.map(row => row.symbol)])

it('puts stocks that share a concept into one group, first appearance order', () => {
  const rows = [stock('C', [{ name: 'Z', members: 20 }]), stock('A', [{ name: 'X', members: 50 }, { name: 'Y', members: 5 }]), stock('B', [{ name: 'X', members: 50 }])]
  expect(names(groupCandidatesByTheme(rows, true))).toEqual([['Z', ['C']], ['X', ['A', 'B']]])
})

it('counts a stock once even with several patterns', () => {
  // A 的两个形态都只有 X；C 有 X 与成分更少的 Z；D 只有 Z。按股票计数 X=2、Z=2 → C 取成分更少的 Z；按行计数 X=3 → C 会误归 X。
  const rows = [stock('A', [{ name: 'X', members: 50 }], { pattern: 'platform' }), stock('A', [{ name: 'X', members: 50 }], { pattern: 'trend' }),
    stock('C', [{ name: 'X', members: 50 }, { name: 'Z', members: 5 }]), stock('D', [{ name: 'Z', members: 5 }])]
  const groups = groupCandidatesByTheme(rows, true)
  expect(names(groups)).toEqual([['X', ['A', 'A']], ['Z', ['C', 'D']]])
  expect(groups[0].stockCount).toBe(1)
  expect(groups[1].stockCount).toBe(2)
})

it('breaks ties with the mainline and then with fewer members and then by name', () => {
  const rows = [stock('A', [{ name: 'P', members: 40 }, { name: 'Q', members: 10 }], { mainline: [['P', 2]] }),
    stock('B', [{ name: 'R', members: 30 }, { name: 'S', members: 10 }]),
    stock('C', [{ name: 'M2' }, { name: 'M1' }])]
  const groups = groupCandidatesByTheme(rows, true)
  expect(groups.map(group => group.name)).toEqual(['P', 'S', 'M1'])
  expect(groups[0].mainlineRank).toBe(2)
  expect(groups[1].mainlineRank).toBeNull()
  // 缺失的成分数视为最多：有成分数的概念优先。
  expect(groupCandidatesByTheme([stock('D', [{ name: 'P' }, { name: 'Q', members: 100 }])], true)[0].name).toBe('Q')
  // 两个主线概念：名次靠前者。
  expect(groupCandidatesByTheme([stock('E', [{ name: 'P' }, { name: 'Q' }], { mainline: [['P', 3], ['Q', 1]] })], true)[0].name).toBe('Q')
})

it('treats a mainline concept as a candidate theme', () => {
  const groups = groupCandidatesByTheme([stock('A', [{ name: 'X', members: 3 }], { mainline: [['M', 1]] })], true)
  expect(groups[0].name).toBe('M')
  expect(groups[0].mainlineRank).toBe(1)
})

it('falls back to the industry and then to the unclassified group', () => {
  const groups = groupCandidatesByTheme([stock('A', [], { sector: '元件-被动元件' }), stock('B', [])], true)
  expect(groups.map(group => [group.key, group.name, group.kind, group.stats])).toEqual([
    ['industry:元件-被动元件', '元件-被动元件', 'industry', null], ['none', '未归类', 'none', null]])
})

it('keeps the incoming order inside a group', () => {
  const rows = [stock('B', [{ name: 'X' }]), stock('A', [{ name: 'X' }])]
  expect(groupCandidatesByTheme(rows, true)[0].rows.map(row => row.symbol)).toEqual(['B', 'A'])
})

it('has stats from the first member with current attention data', () => {
  const rows = [stock('A', [{ name: 'X', mean: .5 }], { status: 'stale' }),
    stock('B', [{ name: 'X', mean: .031, up: .68, leader: { name: '华微电子', at: true } }])]
  expect(groupCandidatesByTheme(rows, true)[0].stats).toEqual({ meanChangePct: .031, upRatio: .68, leader: { name: '华微电子', atLimit: true } })
})

it('has no stats without current attention data', () => {
  const rows = [stock('A', [{ name: 'X', mean: .031 }]), stock('B', [{ name: 'X', mean: .02 }], { status: 'stale' })]
  expect(groupCandidatesByTheme(rows, false)[0].stats).toBeNull()
  expect(groupCandidatesByTheme([stock('A', [{ name: 'X', mean: .031 }], { status: 'stale' })], true)[0].stats).toBeNull()
})

it('keeps a stock in its pinned theme when later rows would change the sharing counts', () => {
  const base = [stock('A', [{ name: 'X', members: 50 }, { name: 'Y', members: 10 }]), stock('B', [{ name: 'X', members: 50 }]), stock('C', [{ name: 'Y', members: 10 }])]
  const first = groupCandidatesByTheme(base, true)
  expect(names(first)).toEqual([['Y', ['A', 'C']], ['X', ['B']]])
  const pinned = new Map(first.flatMap(group => group.rows.map(row => [`${row.symbol}-${row.pattern}`, group.key] as const)))
  const later = [...base, stock('D', [{ name: 'X', members: 50 }])]
  // 不钉住时 X 的共享数变成 3，A 会改归 X；钉住后 A 留在 Y，新来的 D 按规则归 X。
  expect(names(groupCandidatesByTheme(later, true))[0]).toEqual(['X', ['A', 'B', 'D']])
  expect(names(groupCandidatesByTheme(later, true, pinned))).toEqual([['Y', ['A', 'C']], ['X', ['B', 'D']]])
})

it('ignores a pinned theme the stock no longer has', () => {
  const groups = groupCandidatesByTheme([stock('A', [{ name: 'X' }])], true, new Map([['A-platform', 'concept:Gone']]))
  expect(groups[0].name).toBe('X')
})

it('picks the group leader across all members, not just the first member view', () => {
  // 后端为每只候选给出的领涨股会剔除它自己：L1 看到的是 L2，L2 看到的是 L1；真正最先涨停的是 L1。
  const rows = [stock('L1', [{ name: 'X', leader: { name: 'L2', at: true } }]), stock('L2', [{ name: 'X', leader: { name: 'L1', at: true } }])]
  ;(rows[0].attention_context!.groups[0].leader as { observed_limit_at: string }).observed_limit_at = '2026-11-20T09:40:00+08:00'
  ;(rows[1].attention_context!.groups[0].leader as { observed_limit_at: string }).observed_limit_at = '2026-11-20T09:31:00+08:00'
  expect(groupCandidatesByTheme(rows, true)[0].stats?.leader).toEqual({ name: 'L1', atLimit: true })
})
