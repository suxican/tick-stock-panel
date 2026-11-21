import { expect, it } from 'vitest'
import { applyFrozenOrder } from './frozenOrder'

type Row = { id: string; v: number }
const row = (id: string, v = 0): Row => ({ id, v })
const ids = (rows: Row[]) => rows.map(item => item.id)
const keyOf = (item: Row) => item.id

it('shows the live order when nothing was frozen yet', () => {
  const live = [row('c'), row('a'), row('b')]
  for (const previous of [null, []]) {
    const result = applyFrozenOrder(live, previous, keyOf)
    expect(ids(result.rows)).toEqual(['c', 'a', 'b'])
    expect(result.keys).toEqual(['c', 'a', 'b'])
    expect(result.changed).toBe(false)
  }
})

it('keeps the previous order but uses the latest row objects', () => {
  const result = applyFrozenOrder([row('b', 9), row('a'), row('c')], ['a', 'b', 'c'], keyOf)
  expect(ids(result.rows)).toEqual(['a', 'b', 'c'])
  expect(result.rows[1].v).toBe(9)
  expect(result.changed).toBe(true)
})

it('appends new rows at the end in live order and drops missing ones', () => {
  const result = applyFrozenOrder([row('d'), row('a'), row('c'), row('e')], ['a', 'b', 'c'], keyOf)
  expect(ids(result.rows)).toEqual(['a', 'c', 'd', 'e'])
  expect(result.keys).toEqual(['a', 'c', 'd', 'e'])
})

it('treats a row that disappeared and came back as new', () => {
  const first = applyFrozenOrder([row('a'), row('c')], ['a', 'b', 'c'], keyOf)
  expect(first.keys).toEqual(['a', 'c'])
  const second = applyFrozenOrder([row('b'), row('a'), row('c')], first.keys, keyOf)
  expect(ids(second.rows)).toEqual(['a', 'c', 'b'])
})

it('reports no change when the frozen order equals the live order', () => {
  expect(applyFrozenOrder([row('a', 1), row('b', 2)], ['a', 'b'], keyOf).changed).toBe(false)
})
