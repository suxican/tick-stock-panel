// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { AuctionSentimentPayload, AuctionSentimentRow } from '@/lib/api'
import { AuctionSentimentCard } from './RegimeAuctionSentiment'

const fixtures = vi.hoisted(() => ({ payload: null as unknown, options: [] as any[] }))
vi.mock('@/lib/api', () => ({ api: { regimeAuctionSentiment: vi.fn(async () => fixtures.payload) } }))
vi.mock('echarts', () => ({
  init: () => ({
    setOption: (o: any) => { fixtures.options.push(o) }, resize: vi.fn(), dispose: vi.fn(), isDisposed: () => false,
  }),
}))

function row(over: Partial<AuctionSentimentRow> = {}): AuctionSentimentRow {
  return {
    date: '2026-11-20', count: 5300, high_open_ratio: 0.0183, low_open_ratio: 0.9611, median_pct: -0.0175,
    limit_up_open: 2, limit_down_open: 18, prev_sealed: 40, prev_mean_pct: -0.0312, prev_high_open_ratio: 0.15,
    prev_weak_count: 21, amount: 1.35e10, amount_ratio_5d: 1.4, ...over,
  }
}

function payload(over: Partial<AuctionSentimentPayload> = {}): AuctionSentimentPayload {
  return {
    rows: [row({ date: '2026-11-19', high_open_ratio: 0.42, prev_mean_pct: 0.021 }), row()],
    latest: { ...row(), pct: { high_open_ratio: 1.2, prev_mean_pct: 3, prev_weak_count: 99, amount_ratio_5d: 88 } },
    first_date: '2019-01-02', ...over,
  }
}

let host: HTMLDivElement
let root: Root
let client: QueryClient
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} })
  fixtures.options.length = 0
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
})
afterEach(async () => {
  await act(async () => root.unmount())
  client.clear()
  host.remove()
  vi.unstubAllGlobals()
})

async function render(data: AuctionSentimentPayload) {
  fixtures.payload = data
  await act(async () => root.render(
    <QueryClientProvider client={client}><AuctionSentimentCard start="2026-11-19" end="2026-11-20" /></QueryClientProvider>,
  ))
  await act(async () => { await new Promise(r => setTimeout(r, 30)) })
}

function tile(metric: string): HTMLElement {
  const el = host.querySelector<HTMLElement>(`[data-metric="${metric}"]`)
  expect(el, metric).not.toBeNull()
  return el!
}

it('shows money effect and open breadth with one-year percentiles', async () => {
  await render(payload())
  expect(tile('prev_mean_pct').textContent).toContain('-3.12%')
  expect(tile('prev_mean_pct').textContent).toContain('近一年 3 分位')
  expect(tile('prev_weak_count').textContent).toContain('21')
  expect(tile('high_open_ratio').textContent).toContain('1.8%')
  expect(tile('amount_ratio_5d').textContent).toContain('1.40 倍')
  expect(host.textContent).toContain('竞价涨停 2 · 跌停 18')
  expect(host.textContent).toContain('2026-11-20')
})

it('shows dashes instead of zero when data is missing', async () => {
  await render(payload({ latest: { ...row({ prev_mean_pct: null, amount_ratio_5d: null, limit_up_open: null, limit_down_open: null }), pct: {} } }))
  expect(tile('prev_mean_pct').textContent).toContain('—')
  expect(tile('amount_ratio_5d').textContent).toContain('—')
  expect(host.textContent).toContain('竞价涨停 — · 跌停 —')
})

it('plots high-open ratio and previous-limit auction mean as percentages', async () => {
  await render(payload())
  const option = fixtures.options.at(-1)
  expect(option.xAxis.data).toEqual(['2026-11-19', '2026-11-20'])
  const byName = Object.fromEntries(option.series.map((s: any) => [s.name, s.data]))
  expect(byName['高开占比']).toEqual([42, 1.8])
  expect(byName['昨涨停竞价均值']).toEqual([2.1, -3.1])
})

it('renders nothing when there is no archive', async () => {
  await render({ rows: [], latest: null, first_date: null })
  expect(host.querySelector('[data-metric]')).toBeNull()
})
