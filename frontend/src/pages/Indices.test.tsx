// @vitest-environment jsdom
import { act, useState } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { Indices } from './Indices'

type MinuteResponse = {
  symbol: string
  date: string
  rows: { datetime: string; close: number }[]
}
const fixtures = vi.hoisted(() => ({
  pending: [] as { symbol: string; day: string; resolve: (value: MinuteResponse) => void }[],
  mounts: 0,
  thsUsable: true,
  thsDailyState: 'ready',
  catalog: vi.fn(async () => ({ rows: [
    { symbol: '000001.SH', name: '上证指数', market: 'CN' },
    { symbol: '000688.SH', name: '科创50', market: 'CN' },
    { symbol: '883421.TI', name: '同花顺全A（沪深）', market: 'CN', capability: 'ths_index' },
    { symbol: '^GSPC', name: '标普500', market: 'US' },
    { symbol: '^N225', name: '日经225', market: 'JP' },
    { symbol: '^KS11', name: '韩国综合指数', market: 'KR' },
  ] })),
  quotes: vi.fn(async (_symbols: string[]) => ({ rows: [
    { symbol: '000688.SH', last_price: 1111.2, change_pct: 1.23, date: '2026-09-10', source: 'index_daily' },
  ] })),
  overseasQuotes: vi.fn(async () => ({ rows: [
    { symbol: '^N225', last_price: 50001, change_pct: -0.62, date: '2026-09-11' },
  ], source: 'overseas_fixture' })),
  thsQuotes: vi.fn(async () => ({ rows: [
    { symbol: '883421.TI', last_price: 1281.983, change_pct: 2.285415, snapshot_time: 1793894426000 },
  ], source: 'fuyao', reason: undefined as string | undefined })),
}))

vi.mock('@/lib/api', () => ({
  api: {
    indexCatalog: fixtures.catalog,
    indexQuotes: fixtures.quotes,
    overseasIndexQuotes: fixtures.overseasQuotes,
    thsIndexQuotes: fixtures.thsQuotes,
    indexDaily: async (symbol: string) => {
      if (symbol === '883421.TI' && fixtures.thsDailyState !== 'ready') return {
        symbol, rows: [], reason: '本次未取得日K', unavailable: fixtures.thsDailyState === 'unavailable',
      }
      return {
        symbol,
        rows: (symbol === '^KS11' ? [] : ['2026-09-09', '2026-09-10', '2026-09-11']).map(date => ({
          date, open: 10, high: 12, low: 9, close: 11, volume: 1,
        })),
      }
    },
    indexMinute: (symbol: string, day: string) => new Promise<MinuteResponse>(resolve => {
      fixtures.pending.push({ symbol, day, resolve })
    }),
  },
}))
vi.mock('@/lib/useSharedQueries', () => ({
  useCapabilityMatrix: () => ({ data: { capabilities: [
    { id: 'minute', usable: true }, { id: 'overseas', usable: true, effective: 'overseas_fixture' },
    { id: 'ths_index', usable: fixtures.thsUsable, effective: 'fuyao', effective_display: '扶摇' },
  ] } }),
}))
vi.mock('@/components/EChartsCandlestick', () => ({
  EChartsCandlestick: ({ data, onDateClick }: {
    data: { date: string }[]; onDateClick: (date: string) => void
  }) => <div>{data.map(row => (
    <button key={row.date} data-day={row.date} onClick={() => onDateClick(row.date)}>{row.date}</button>
  ))}</div>,
}))
vi.mock('@/components/EChartsIntraday', () => ({
  EChartsIntraday: ({ data, date }: { data: MinuteResponse['rows']; date: string }) => {
    const [mount] = useState(() => ++fixtures.mounts)
    return <div data-chart data-mount={mount}>{`${date}|${data[0].datetime}|${data[0].close}`}</div>
  },
}))

let host: HTMLDivElement
let root: Root
let client: QueryClient

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  fixtures.pending.length = 0
  fixtures.mounts = 0
  fixtures.thsUsable = true
  fixtures.thsDailyState = 'ready'
  vi.clearAllMocks()
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: {
    retry: false, staleTime: Infinity, gcTime: Infinity,
  } } })
})

async function openIndices(path = '/indices') {
  await act(async () => root.render(
    <MemoryRouter initialEntries={[path]}><QueryClientProvider client={client}><Indices /></QueryClientProvider></MemoryRouter>,
  ))
  await settle()
}

it('groups the catalog and shows the trading date and percentage of the selected index', async () => {
  await openIndices('/indices?symbol=000688.SH')
  const selector = host.querySelector<HTMLSelectElement>('select[aria-label="选择指数"]')!
  expect([...selector.querySelectorAll('optgroup')].map(group => group.label)).toEqual(['A股', '美股', '日本', '韩国'])
  expect(selector.value).toBe('000688.SH')
  expect(fixtures.quotes).toHaveBeenCalledWith(['000001.SH', '000688.SH'])
  expect(host.querySelector('main')?.textContent).toContain('1.23%')
  expect(host.querySelector('main')?.textContent).toContain('2026-09-10')
  await act(async () => {
    selector.value = '^N225'
    selector.dispatchEvent(new Event('change', { bubbles: true }))
  })
  await settle()
  expect(host.querySelector('h2')?.textContent).toBe('日经225')
  expect(host.querySelector('main')?.textContent).toContain('-0.62%')
  expect(host.querySelector('main')?.textContent).toContain('当地交易日 2026-09-11')
  expect(fixtures.pending.some(call => call.symbol === '^N225')).toBe(false)
  expect(host.textContent).not.toContain('同步指数日K')
})

it('shows missing overseas history and keeps its quote separate from other markets', async () => {
  await openIndices('/indices?symbol=%5EKS11')
  expect(host.querySelector('h2')?.textContent).toBe('韩国综合指数')
  expect(host.querySelector('main')?.textContent).toContain('暂无日K')
  expect(host.querySelector('main')?.textContent).not.toContain('50001')
  expect(host.querySelector('main [data-day]')).toBeNull()
  expect(fixtures.pending).toHaveLength(0)
})

it('keeps a domestic monitoring link outside the fixed catalog accessible', async () => {
  await openIndices('/indices?symbol=000300.SH')
  expect(host.querySelector('h2')?.textContent).toBe('000300.SH')
  expect(fixtures.quotes).toHaveBeenCalledWith(['000001.SH', '000688.SH', '000300.SH'])
  expect(fixtures.pending.some(call => call.symbol === '000300.SH')).toBe(true)
})

it('shows THS quotes with a source and timestamp through its independent provider', async () => {
  await openIndices('/indices?symbol=883421.TI')
  const content = host.querySelector('main')?.textContent
  expect(host.querySelector('h2')?.textContent).toBe('同花顺全A（沪深）')
  expect(content).toContain('1281.98')
  expect(content).toContain('2.29%')
  expect(content).toContain('扶摇 · 快照获取时间')
  expect(content).toContain('2026/11/6 00:00:26')
  expect(fixtures.thsQuotes).toHaveBeenCalledOnce()
  expect(fixtures.quotes).toHaveBeenCalledWith(['000001.SH', '000688.SH'])
  expect(fixtures.pending).toHaveLength(0)
  expect(host.textContent).not.toContain('同步指数日K')
  expect(client.getQueryCache().findAll({ queryKey: ['ths-index-daily', 'fuyao', '883421.TI'] })).toHaveLength(1)
})

it('includes the current Beijing date when it is still the previous UTC day', async () => {
  vi.useFakeTimers()
  vi.setSystemTime(new Date('2026-11-05T16:05:00Z'))
  await openIndices('/indices?symbol=883421.TI')
  expect(host.querySelector<HTMLInputElement>('input[aria-label="结束日期"]')?.value).toBe('2026-11-06')
})

it('shows unavailable THS capability without requesting its quote or domestic minute data', async () => {
  fixtures.thsUsable = false
  await openIndices('/indices?symbol=883421.TI')
  expect(fixtures.thsQuotes).not.toHaveBeenCalled()
  expect(fixtures.pending).toHaveLength(0)
  expect(host.querySelector('main [data-day]')).toBeNull()
  expect(host.querySelector('main')?.textContent).toContain('配置同花顺指数数据源')
})

it('retains the last THS quote after an update fails', async () => {
  await openIndices('/indices?symbol=883421.TI')
  fixtures.thsQuotes.mockRejectedValueOnce(new Error('source unavailable'))
  await act(async () => { await client.invalidateQueries({ queryKey: ['ths-index-quotes'] }) })
  await settle()
  expect(host.querySelector('main')?.textContent).toContain('1281.98')
  expect(host.querySelector('main')?.textContent).toContain('更新失败，显示上次报价')
})

it('keeps the previous THS quote when the source reports missing data', async () => {
  await openIndices('/indices?symbol=883421.TI')
  fixtures.thsQuotes.mockResolvedValueOnce({ rows: [], source: 'fuyao', reason: '数据源暂不可用' })
  await act(async () => { await client.invalidateQueries({ queryKey: ['ths-index-quotes'] }) })
  await settle()
  expect(host.querySelector('main')?.textContent).toContain('1281.98')
  expect(host.querySelector('main')?.textContent).toContain('更新失败，显示上次报价')
})

it('retains THS daily candles when a refresh reports unavailable data', async () => {
  await openIndices('/indices?symbol=883421.TI')
  expect(host.querySelectorAll('main [data-day]')).toHaveLength(3)
  fixtures.thsDailyState = 'unavailable'
  await act(async () => { await client.invalidateQueries({ queryKey: ['ths-index-daily'] }) })
  await settle()
  expect(host.querySelectorAll('main [data-day]')).toHaveLength(3)
  expect(host.querySelector('main')?.textContent).toContain('日K更新失败，显示上次数据')
})

it('shows an empty THS date range without retaining earlier candles', async () => {
  await openIndices('/indices?symbol=883421.TI')
  fixtures.thsDailyState = 'empty'
  await act(async () => { await client.invalidateQueries({ queryKey: ['ths-index-daily'] }) })
  await settle()
  expect(host.querySelector('main [data-day]')).toBeNull()
  expect(host.querySelector('main')?.textContent).toContain('本次未取得日K')
  expect(host.querySelector('main')?.textContent).not.toContain('日K更新失败')
})

it('refreshes THS quotes every ten seconds and stops after leaving the page', async () => {
  vi.useFakeTimers()
  await openIndices('/indices?symbol=883421.TI')
  expect(fixtures.thsQuotes).toHaveBeenCalledOnce()
  await act(async () => { await vi.advanceTimersByTimeAsync(10_000) })
  await settle()
  expect(fixtures.thsQuotes).toHaveBeenCalledTimes(2)
  await act(async () => root.render(null))
  await act(async () => { await vi.advanceTimersByTimeAsync(20_000) })
  expect(fixtures.thsQuotes).toHaveBeenCalledTimes(2)
})

afterEach(async () => {
  await act(async () => root.unmount())
  client.clear()
  host.remove()
  vi.useRealTimers()
})

// React Query batches observer notifications on the next timer tick.
async function settle() {
  for (let i = 0; i < 5; i++) {
    await act(async () => {
      if (vi.isFakeTimers()) await vi.advanceTimersByTimeAsync(0)
      else await new Promise(resolve => setTimeout(resolve, 0))
    })
  }
}

async function resolveDay(day: string, close: number) {
  const index = fixtures.pending.findIndex(call => call.day === day)
  expect(index).toBeGreaterThanOrEqual(0)
  const [call] = fixtures.pending.splice(index, 1)
  await act(async () => call.resolve({
    symbol: call.symbol, date: day, rows: [{ datetime: `${day}T09:35:00`, close }],
  }))
  await settle()
}

async function clickDay(day: string) {
  const button = host.querySelector<HTMLButtonElement>(`[data-day="${day}"]`)
  expect(button).not.toBeNull()
  await act(async () => button!.click())
  await settle()
}

it('keeps selected dates isolated across delayed, out-of-order and cached responses', async () => {
  await act(async () => root.render(
    <MemoryRouter><QueryClientProvider client={client}><Indices /></QueryClientProvider></MemoryRouter>,
  ))
  await settle()
  const chart = () => host.querySelector<HTMLElement>('[data-chart]')
  await resolveDay('2026-09-11', 11)
  expect(chart()?.textContent).toBe('2026-09-11|2026-09-11T09:35:00|11')

  await clickDay('2026-09-10')
  expect(chart()).toBeNull()
  await clickDay('2026-09-09')
  await resolveDay('2026-09-10', 10)
  expect(chart()).toBeNull()
  await resolveDay('2026-09-09', 9)
  expect(chart()?.textContent).toBe('2026-09-09|2026-09-09T09:35:00|9')

  const mount9 = chart()?.dataset.mount
  await clickDay('2026-09-11')
  expect(chart()?.textContent).toBe('2026-09-11|2026-09-11T09:35:00|11')
  expect(chart()?.dataset.mount).not.toBe(mount9)

  const mount11 = chart()?.dataset.mount
  await act(async () => { void client.invalidateQueries({ queryKey: ['index-minute'] }) })
  await settle()
  expect(chart()?.textContent).toBe('2026-09-11|2026-09-11T09:35:00|11')
  await resolveDay('2026-09-11', 12)
  expect(chart()?.textContent).toBe('2026-09-11|2026-09-11T09:35:00|12')
  expect(chart()?.dataset.mount).toBe(mount11)
})
