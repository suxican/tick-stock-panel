// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { api, type StockLevels } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { StockAnalysisBoard } from './StockAnalysisBoard'

const chart = vi.hoisted(() => ({ props: null as any }))
vi.mock('./AnalysisKChart', () => ({
  AnalysisKChart: (props: any) => {
    chart.props = props
    return <output data-chart>{props.scopeKey}</output>
  },
}))

const symbol = '600000.SH'
const rows = [{ date: '2026-11-06', open: 10, high: 12, low: 9, close: 11, volume: 100 }]
const levels = {
  symbol, close: 11, summary: '', dates: ['2026-11-06'],
  levels: { sr: [{ type: 'sr', side: 'support', value: 10, label: '支撑位' }] },
} as StockLevels
let host: HTMLDivElement
let root: Root
let client: QueryClient

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  chart.props = null
  vi.spyOn(api, 'klineDaily').mockResolvedValue({ symbol, rows })
  vi.spyOn(api, 'stockAnalysisLevels').mockResolvedValue(levels)
})
afterEach(async () => {
  await act(async () => root.unmount())
  client.clear()
  host.remove()
  vi.restoreAllMocks()
})

const settle = () => act(async () => { await new Promise(resolve => setTimeout(resolve, 10)) })
async function render(stock = symbol) {
  await act(async () => root.render(<QueryClientProvider client={client}>
    <StockAnalysisBoard symbol={stock} height={360} />
  </QueryClientProvider>))
  await settle()
}
const button = (label: string) => [...host.querySelectorAll('button')].find(item => item.textContent === label)!

it('shares the existing chart and distinguishes its 250-day levels from the alert cache', async () => {
  client.setQueryData(QK.stockLevels(symbol), { ...levels, close: 99 })
  await render()
  expect(api.klineDaily).toHaveBeenCalledWith(symbol, 250)
  expect(api.stockAnalysisLevels).toHaveBeenCalledWith(symbol, 250)
  expect(client.getQueryData(QK.stockLevels(symbol, 250))).toEqual(levels)
  expect(host.textContent).toContain('11.00')
  expect(host.textContent).not.toContain('99.00')
  expect(host.textContent).toContain('2026-11-06')
  expect(chart.props).toMatchObject({ rows, levels: levels.levels, height: 360, scopeKey: symbol,
    defaultLevelTypes: ['sr', 'pivot', 'keltner_s'] })
})

it('announces daily data loading without showing an empty chart', async () => {
  vi.mocked(api.klineDaily).mockReturnValue(new Promise(() => {}))
  await render()
  expect(host.textContent).toContain('正在加载日 K 数据')
  expect(host.querySelector('[data-chart]')).toBeNull()
})

it('uses the latest daily close for the dated header even when calculated levels lag behind', async () => {
  vi.mocked(api.stockAnalysisLevels).mockResolvedValue({ ...levels, close: 10, dates: ['2026-11-05'] })
  await render()
  expect(host.textContent).toContain('2026-11-06')
  expect(host.textContent).toContain('11.00')
  expect(host.textContent).not.toContain('10.00')
  expect(chart.props.rows).toEqual(rows)
})

it('keeps daily candles visible while levels load', async () => {
  vi.mocked(api.stockAnalysisLevels).mockReturnValue(new Promise(() => {}))
  await render()
  expect(host.textContent).toContain('正在加载关键价位')
  expect(host.querySelector('[data-chart]')).not.toBeNull()
  expect(chart.props.levels).toBeUndefined()
})

it('offers a retry when daily data fails', async () => {
  vi.mocked(api.klineDaily).mockRejectedValueOnce(new Error('offline'))
  await render()
  expect(host.textContent).toContain('日 K 数据加载失败')
  await act(async () => button('重试').click())
  await settle()
  expect(host.querySelector('[data-chart]')).not.toBeNull()
})

it('distinguishes failed levels from empty levels and retries without losing candles', async () => {
  vi.mocked(api.stockAnalysisLevels).mockRejectedValueOnce(new Error('offline'))
  await render()
  expect(host.textContent).toContain('关键价位加载失败')
  expect(host.textContent).not.toContain('暂无可用关键价位')
  expect(host.querySelector('[data-chart]')).not.toBeNull()
  await act(async () => button('重试').click())
  await settle()
  expect(chart.props.levels).toEqual(levels.levels)
  expect(host.textContent).not.toContain('关键价位加载失败')
})

it('shows separate empty daily and empty levels states', async () => {
  vi.mocked(api.klineDaily).mockResolvedValueOnce({ symbol, rows: [] })
  await render()
  expect(host.textContent).toContain('暂无日 K 数据')
  expect(host.querySelector('[data-chart]')).toBeNull()
  vi.mocked(api.stockAnalysisLevels).mockResolvedValue({ ...levels, levels: {} as StockLevels['levels'] })
  await act(async () => client.invalidateQueries())
  await settle()
  expect(host.textContent).toContain('暂无可用关键价位')
  expect(host.querySelector('[data-chart]')).not.toBeNull()
})

it('never displays the previous stock while the next stock is loading', async () => {
  await render()
  vi.mocked(api.klineDaily).mockReturnValue(new Promise(() => {}))
  vi.mocked(api.stockAnalysisLevels).mockReturnValue(new Promise(() => {}))
  await render('000001.SZ')
  expect(host.textContent).not.toContain('11.00')
  expect(host.querySelector('[data-chart]')).toBeNull()
  expect(host.textContent).toContain('正在加载日 K 数据')
})

it('retains the last valid chart when a background refresh fails', async () => {
  await render()
  vi.mocked(api.klineDaily).mockRejectedValue(new Error('offline'))
  vi.mocked(api.stockAnalysisLevels).mockRejectedValue(new Error('offline'))
  await act(async () => client.invalidateQueries())
  await settle()
  expect(host.querySelector('[data-chart]')).not.toBeNull()
  expect(chart.props.rows).toEqual(rows)
  expect(chart.props.levels).toEqual(levels.levels)
  expect(host.textContent).toContain('更新失败')
})
