import { expect, it } from 'vitest'
import type { FirstBoardCandidate } from '@/lib/api'
import { MAX_IMMINENT_CARDS, pickImminent } from './pickImminent'

function row(symbol: string, state: FirstBoardCandidate['state'], distance: number | null, canBuy = false, pattern = 'platform') {
  return { symbol, pattern, pattern_label: pattern, state, can_buy: canBuy, distance_to_limit_pct: distance } as FirstBoardCandidate
}
const symbols = (result: ReturnType<typeof pickImminent>) => result.cards.map(card => card.symbol)

it('picks only approaching rows, can-buy first and then the nearest to the limit', () => {
  const result = pickImminent([row('A', 'approaching', .02), row('B', 'approaching', .01), row('C', 'sealed', 0), row('D', 'approaching', .03, true)], [])
  expect(symbols(result)).toEqual(['D', 'B', 'A'])
  expect(result.total).toBe(3)
  expect(result.keys).toEqual(['D', 'B', 'A'])
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
  const previous = ['A', 'B', 'C']
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
  expect(symbols(pickImminent(rows, ['A', 'B', 'C']))).toEqual(['A', 'B', 'D'])
  // 已显示的都是可提示时不替换。
  const buyable = [row('A', 'approaching', .01, true), row('B', 'approaching', .02, true), row('C', 'approaching', .03, true), row('D', 'approaching', .001, true)]
  expect(symbols(pickImminent(buyable, ['A', 'B', 'C']))).toEqual(['A', 'B', 'C'])
})

it('merges several patterns of one stock into one card using its best row', () => {
  const rows = [row('A', 'approaching', .02, false, 'platform'), row('A', 'approaching', .02, true, 'trend'), row('B', 'approaching', .01)]
  const result = pickImminent(rows, [])
  expect(result.cards.map(card => [card.symbol, card.pattern])).toEqual([['A', 'trend'], ['B', 'platform']])
  expect(result.total).toBe(2)
  expect(result.keys).toEqual(['A', 'B'])
  expect(result.patternLabels.A).toEqual(['trend', 'platform'])
})

it('keeps a merged card in place when its best pattern changes', () => {
  const first = pickImminent([row('A', 'approaching', .02, false, 'platform'), row('B', 'approaching', .03)], [])
  const next = pickImminent([row('A', 'approaching', .02, true, 'trend'), row('A', 'approaching', .02, false, 'platform'), row('B', 'approaching', .03)], first.keys)
  expect(next.cards.map(card => card.symbol)).toEqual(['A', 'B'])
  expect(next.cards[0].pattern).toBe('trend')
})
