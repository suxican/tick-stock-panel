import type { FirstBoardCandidate } from '@/lib/api'

export const MAX_IMMINENT_CARDS = 3
const keyOf = (row: FirstBoardCandidate) => `${row.symbol}-${row.pattern}`

/**
 * 选出“临近涨停”焦点卡片：可提示的在前，其次距涨停近的在前；已显示且仍入选的卡片保持原位，
 * 空位由排序最靠前的候选补上，避免盘中刷新时卡片互相换位造成误点；新变为可提示的候选会顶替已显示的最靠后的不可提示卡片。
 */
export function pickImminent(rows: FirstBoardCandidate[], previous: string[], max = MAX_IMMINENT_CARDS): { cards: FirstBoardCandidate[]; keys: string[]; total: number } {
  const approaching = rows.filter(row => row.state === 'approaching')
  const distance = (row: FirstBoardCandidate) => row.distance_to_limit_pct ?? Infinity
  const ranked = [...approaching].sort((a, b) => Number(b.can_buy) - Number(a.can_buy) || distance(a) - distance(b) || keyOf(a).localeCompare(keyOf(b)))
  const byKey = new Map(approaching.map(row => [keyOf(row), row]))
  const kept = [...new Set(previous)].filter(key => byKey.has(key)).slice(0, max).map(key => byKey.get(key)!)
  const keptKeys = new Set(kept.map(keyOf))
  const rest = ranked.filter(row => !keptKeys.has(keyOf(row)))
  const cards = [...kept, ...rest.slice(0, max - kept.length)]
  // 新变为可提示的候选不能被已显示的不可提示卡片挡在外面：顶替其中排序最靠后的那张（原位替换，不挪动其他卡片）。
  const shownKeys = new Set(cards.map(keyOf))
  for (const buyable of rest.filter(row => row.can_buy && !shownKeys.has(keyOf(row)))) {
    const victim = cards.reduce((worst, card, index) => (!card.can_buy && (worst < 0 || ranked.indexOf(card) > ranked.indexOf(cards[worst]))) ? index : worst, -1)
    if (victim < 0) break
    shownKeys.delete(keyOf(cards[victim]))
    cards[victim] = buyable
    shownKeys.add(keyOf(buyable))
  }
  return { cards, keys: cards.map(keyOf), total: approaching.length }
}
