import { expect, it } from 'vitest'
import type { FirstBoardCandidate } from '@/lib/api'
import { MAX_IMMINENT_CARDS, pickImminent } from './pickImminent'

function row(symbol: string, state: FirstBoardCandidate['state'], distance: number | null, canBuy = false) {
  return { symbol, pattern: 'platform', state, can_buy: canBuy, distance_to_limit_pct: distance } as FirstBoardCandidate
}
const symbols = (result: ReturnType<typeof pickImminent>) => result.cards.map(card => card.symbol)

it('picks only approaching rows, can-buy first and then the nearest to the limit', () => {
  const result = pickImminent([row('A', 'approaching', .02), row('B', 'approaching', .01), row('C', 'sealed', 0), row('D', 'approaching', .03, true)], [])
  expect(symbols(result)).toEqual(['D', 'B', 'A'])
  expect(result.total).toBe(3)
  expect(result.keys).toEqual(['D-platform', 'B-platform', 'A-platform'])
})

it('puts missing distances last', () => {
  expect(symbols(pickImminent([row('E', 'approaching', null), row('F', 'approaching', .02)], []))).toEqual(['F', 'E'])
})

it('caps at three and reports the total', () => {
  const rows = ['A', 'B', 'C', 'D', 'E'].map((symbol, index) => row(symbol, 'approaching', .01 * (index + 1)))
  const result = pickImminent(rows, [])
  expect(MAX_IMMINENT_CARDS).toBe(3)
  expect(result.cards).toHaveLength(3)
  expect(result.total).toBe(5)
})

it('keeps shown cards in place and fills free slots with the best remaining', () => {
  const rows = [row('A', 'approaching', .03), row('B', 'approaching', .02), row('C', 'approaching', .01), row('D', 'approaching', .04)]
  const previous = ['A-platform', 'B-platform', 'C-platform']
  expect(symbols(pickImminent(rows, previous))).toEqual(['A', 'B', 'C'])
  const withoutB = [rows[0], row('B', 'sealed', 0), rows[2], rows[3]]
  expect(symbols(pickImminent(withoutB, previous))).toEqual(['A', 'C', 'D'])
})

it('appends new cards and treats a card that disappeared and came back as new', () => {
  const first = pickImminent([row('A', 'approaching', .01), row('B', 'approaching', .02)], [])
  expect(symbols(first)).toEqual(['A', 'B'])
  const gone = pickImminent([row('B', 'approaching', .02)], first.keys)
  expect(symbols(gone)).toEqual(['B'])
  const back = pickImminent([row('A', 'approaching', .001), row('B', 'approaching', .02)], gone.keys)
  expect(symbols(back)).toEqual(['B', 'A'])
})

it('lets a buyable candidate replace the farthest non-buyable shown card', () => {
  const rows = [row('A', 'approaching', .01), row('B', 'approaching', .02), row('C', 'approaching', .03), row('D', 'approaching', .04, true)]
  expect(symbols(pickImminent(rows, ['A-platform', 'B-platform', 'C-platform']))).toEqual(['A', 'B', 'D'])
  // 已显示的都是可提示时不替换。
  const buyable = [row('A', 'approaching', .01, true), row('B', 'approaching', .02, true), row('C', 'approaching', .03, true), row('D', 'approaching', .001, true)]
  expect(symbols(pickImminent(buyable, ['A-platform', 'B-platform', 'C-platform']))).toEqual(['A', 'B', 'C'])
})
