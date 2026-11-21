// @vitest-environment jsdom
import { renderToStaticMarkup } from 'react-dom/server'
import { expect, it } from 'vitest'
import type { FirstBoardFeedback, FirstBoardFeedbackSummary, FirstBoardSnapshot } from '@/lib/api'
import { KeyNumbers } from './KeyNumbers'

type Ladder = NonNullable<FirstBoardSnapshot['market_ladder']>
const ladder: Ladder = { as_of: '2026-11-20', observed_at: '2026-11-20T15:00:00+08:00', live: false, sealed_corrected: true,
  limit_up: 30, broken: 14, limit_down: 61, max_consecutive: 4, first_board: 26, ge2_count: 4, seal_rate: .6818, promo_rate: .14, promo_pool: 28 }
const all = { sample_count: 19, limit_up_count: 1, mean_change_pct: -.0448 } as FirstBoardFeedbackSummary
const feedback = { groups: { all, main: all, growth: all } } as FirstBoardFeedback

function render(props: Parameters<typeof KeyNumbers>[0]) {
  const host = document.createElement('div')
  host.innerHTML = renderToStaticMarkup(<KeyNumbers {...props} />)
  return host
}
const labels = (host: HTMLElement) => [...host.querySelectorAll('dt')].map(node => node.textContent)
const values = (host: HTMLElement) => [...host.querySelectorAll('dd')].map(node => node.textContent)
const value = (host: HTMLElement, label: string) => [...host.querySelectorAll('dt')].find(node => node.textContent === label)!.nextElementSibling as HTMLElement

it('shows the five numbers in order', () => {
  const host = render({ ladder, feedback, muted: false })
  expect(host.querySelector('dl')?.getAttribute('aria-label')).toBe('关键数字')
  expect(labels(host)).toEqual(['封板率', '昨日首板均涨', '晋级二板', '涨停:跌停', '高度'])
  expect(values(host)).toEqual(['68%', '-4.48%', '1 / 19', '30:61', '4 板'])
  expect(value(host, '昨日首板均涨').className).toContain('text-bear')
})

it('neutralizes the feedback mean when muted', () => {
  const mean = value(render({ ladder, feedback, muted: true }), '昨日首板均涨')
  expect(mean.className).not.toContain('text-bear')
  expect(mean.className).toContain('text-foreground')
})

it('shows placeholders for an older backend', () => {
  expect(values(render({ muted: false }))).toEqual(['—', '—', '—', '—', '—'])
  const partial = render({ ladder: { ...ladder, seal_rate: null, limit_down: null }, feedback, muted: false })
  expect(value(partial, '封板率').textContent).toBe('—')
  expect(value(partial, '涨停:跌停').textContent).toBe('—')
  const empty = render({ ladder, feedback: { groups: { all: { ...all, sample_count: 0 } } } as FirstBoardFeedback, muted: false })
  expect(value(empty, '晋级二板').textContent).toBe('—')
})

it('does not turn unconfirmed limit-up states into zero', () => {
  const none = { ...all, limit_up_count: 0, limit_known_count: 0 } as FirstBoardFeedbackSummary
  expect(value(render({ ladder, feedback: { groups: { all: none } } as FirstBoardFeedback, muted: false }), '晋级二板').textContent).toBe('—')
  const some = { ...all, limit_up_count: 3, limit_known_count: 14 } as FirstBoardFeedbackSummary
  const host = render({ ladder, feedback: { groups: { all: some } } as FirstBoardFeedback, muted: false })
  expect(value(host, '晋级二板').textContent).toBe('3 / 19 · 待确认 5')
})

it('keeps real zeros and colors the mean by sign', () => {
  const up = { ...all, mean_change_pct: .02 } as FirstBoardFeedbackSummary
  const host = render({ ladder: { ...ladder, limit_down: 0 }, feedback: { groups: { all: up } } as FirstBoardFeedback, muted: false })
  expect(value(host, '涨停:跌停').textContent).toBe('30:0')
  expect(value(host, '昨日首板均涨').className).toContain('text-bull')
  const flat = { ...all, mean_change_pct: 0 } as FirstBoardFeedbackSummary
  expect(value(render({ feedback: { groups: { all: flat } } as FirstBoardFeedback, muted: false }), '昨日首板均涨').className).toContain('text-muted')
})
