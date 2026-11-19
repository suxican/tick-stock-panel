// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { SealQualityPayload, SealQualityRow } from '@/lib/api'
import { SealQualityCard } from './RegimeSealQuality'

const fixtures = vi.hoisted(() => ({ payload: null as unknown, options: [] as any[] }))
vi.mock('@/lib/api', () => ({ api: { regimeSealQuality: vi.fn(async () => fixtures.payload) } }))
vi.mock('echarts', () => ({
  init: () => ({
    setOption: (o: any) => { fixtures.options.push(o) }, resize: vi.fn(), dispose: vi.fn(), isDisposed: () => false,
  }),
}))

function row(over: Partial<SealQualityRow> = {}): SealQualityRow {
  return {
    date: '2026-11-19', touched: 47, sealed: 24, break_rate: 0.4894, reopen_rate: 0.5, early_share: 0.5,
    late_share: 0.0417, oneword_share: 0.125, fd_median: 57354546, pool_coverage: 1, ...over,
  }
}

function payload(over: Partial<SealQualityPayload> = {}): SealQualityPayload {
  return {
    rows: [row({ date: '2026-11-18', break_rate: 0.3 }), row()],
    latest: { ...row(), pct: { break_rate: 98, early_share: 55.6, reopen_rate: 57.6, fd_median: 29.6 } },
    first_date: '2018-09-12', reopen_from: '2020-01-02', ...over,
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

async function render(data: SealQualityPayload) {
  fixtures.payload = data
  await act(async () => root.render(
    <QueryClientProvider client={client}><SealQualityCard start="2026-11-18" end="2026-11-19" /></QueryClientProvider>,
  ))
  await act(async () => { await new Promise(r => setTimeout(r, 30)) })
}

function tile(metric: string): HTMLElement {
  const el = host.querySelector<HTMLElement>(`[data-metric="${metric}"]`)
  expect(el, metric).not.toBeNull()
  return el!
}

it('shows the latest values with one-year percentiles', async () => {
  await render(payload())
  expect(tile('break_rate').textContent).toContain('48.9%')
  expect(tile('break_rate').textContent).toContain('近一年 98 分位')
  expect(tile('early_share').textContent).toContain('50.0%')
  expect(tile('reopen_rate').textContent).toContain('50.0%')
  expect(tile('fd_median').textContent).toContain('0.57亿')
  expect(host.textContent).toContain('2026-11-19')
})

it('shows a dash for metrics without data instead of zero', async () => {
  await render(payload({
    latest: { ...row({ reopen_rate: null, fd_median: null }), pct: { break_rate: null } },
  }))
  expect(tile('reopen_rate').textContent).toContain('—')
  expect(tile('fd_median').textContent).toContain('—')
  expect(tile('break_rate').textContent).not.toContain('分位')
})

it('plots break, early-seal and reopen rates as percentages', async () => {
  await render(payload())
  const option = fixtures.options.at(-1)
  expect(option.xAxis.data).toEqual(['2026-11-18', '2026-11-19'])
  const byName = Object.fromEntries(option.series.map((s: any) => [s.name, s.data]))
  expect(byName['炸板率']).toEqual([30, 48.9])
  expect(byName['早盘封板占比']).toEqual([50, 50])
  expect(byName['开板率']).toEqual([50, 50])
})

it('explains the data scope', async () => {
  await render(payload())
  expect(host.textContent).toContain('开板率 2020-01-02 起')
})

it('renders nothing when there is no archive', async () => {
  await render({ rows: [], latest: null, first_date: null, reopen_from: '2020-01-02' })
  expect(host.querySelector('[data-metric]')).toBeNull()
})
