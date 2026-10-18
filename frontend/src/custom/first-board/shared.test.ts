// @vitest-environment node
import { afterEach, expect, it, vi } from 'vitest'
import { beijingDay } from './shared'

afterEach(() => vi.restoreAllMocks())

it.each([
  ['2026-10-08T15:59:59Z', 0, '2026-10-08'],
  ['2026-10-08T16:00:00Z', 0, '2026-10-09'],
  ['2026-12-31T16:00:00Z', 0, '2027-01-01'],
  ['2026-12-31T16:00:00Z', -1, '2026-12-31'],
  ['2024-02-28T16:00:00Z', 1, '2024-03-01'],
  ['2026-10-09T02:00:00Z', -90, '2026-07-11'],
])('formats Beijing date for %s with calendar offset %s', (now, offset, expected) => {
  expect(beijingDay(offset, Date.parse(now))).toBe(expected)
})

it('keeps the API date in ISO format when the requested locale falls back to US English', () => {
  const DateTimeFormat = Intl.DateTimeFormat
  vi.spyOn(Intl, 'DateTimeFormat').mockImplementation(function (_locales, options) {
    return new DateTimeFormat('en-US', options)
  })
  expect(beijingDay(0, Date.parse('2026-10-09T02:00:00Z'))).toBe('2026-10-09')
})
