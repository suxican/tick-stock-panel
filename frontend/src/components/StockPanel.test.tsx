// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { StockPanel } from './StockPanel'
import type { HuichunChartContext } from '@/lib/huichun-chart'

const calls = vi.hoisted(() => ({ klineDaily: vi.fn(), huichunChart: vi.fn(), info: vi.fn(), chart: vi.fn(), intraday: vi.fn(), quoteSubscription: vi.fn() }))
vi.mock('@/lib/api', () => ({ api: calls }))
vi.mock('@/lib/useQuoteSubscription', () => ({ useQuoteSubscription: calls.quoteSubscription }))
vi.mock('@/lib/kline', async importOriginal => ({
  ...await importOriginal<typeof import('@/lib/kline')>(),
  cnToday: () => '2026-09-30',
}))
vi.mock('@/lib/useSharedQueries', () => ({ useCapabilities: () => ({ data: { capabilities: {} } }) }))
vi.mock('@/lib/useFinancials', () => ({ useFinancialMetrics: () => ({}), financialMetricsQueryOptions: vi.fn() }))
vi.mock('@/lib/stock-info-fields', () => ({ loadInfoFields: () => [], saveInfoFields: vi.fn(), buildInfoExtColumnsParam: () => undefined }))
vi.mock('./StockInfoBar', () => ({ StockInfoBar: (props: unknown) => { calls.info(props); return null } }))
vi.mock('./StockIntradayChart', () => ({ StockIntradayChart: (props: unknown) => { calls.intraday(props); return null } }))
vi.mock('./EChartsCandlestick', () => ({
  EChartsCandlestick: (props: unknown) => { calls.chart(props); return <div data-chart="daily" /> },
}))

const context: HuichunChartContext = {
  symbol: '600000.SH', as_of: '2026-09-30', rule_revision: 2,
  rules: { warmup_bars: 250, rally_threshold: .4, zero_threshold: .02, ma_window: 60, slope_lag: 5 },
  signal_date: '2026-09-30',
}
const range = { start: '2026-04-01', end: '2026-09-30' }
let host: HTMLDivElement
let root: Root
let client: QueryClient
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  Object.values(calls).forEach(call => call.mockReset())
  const rows = [{ date: '2026-09-30', open: 10, high: 11, low: 9, close: 10, volume: 100, macd_dif: .2 }]
  calls.klineDaily.mockResolvedValue({ symbol: context.symbol, rows: rows.map(row => ({ ...row, macd_dif: -.9 })) })
  calls.huichunChart.mockResolvedValue({ symbol: context.symbol, name: '历史股票', source: 'huichun_a0', rows,
    huichun: { ...context, macd: [10, 20, 9], price_basis: 'as_of_adjusted', factor_status: 'events_present_unverified' } })
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } })
})
afterEach(async () => { await act(async () => root.unmount()); client.clear(); host.remove() })
async function render(symbol = context.symbol, huichunChart?: HuichunChartContext) {
  await act(async () => root.render(<QueryClientProvider client={client}>
    <StockPanel symbol={symbol} dateRange={range} showIntraday={false} huichunChart={huichunChart} prefetchSymbols={['000001.SZ']} />
  </QueryClientProvider>))
  for (let i = 0; i < 5; i++) await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
}

it('回春信息条和曲线共享专用查询，邻股不沿用当前形态上下文', async () => {
  await render(context.symbol, context)
  expect(calls.huichunChart).toHaveBeenCalledTimes(1)
  expect(calls.klineDaily).not.toHaveBeenCalled()
  expect(calls.info.mock.calls.at(-1)?.[0].rows[0].macd_dif).toBe(.2)
  expect(calls.chart.mock.calls.at(-1)?.[0].data[0].macd_dif).toBe(.2)
  expect(calls.quoteSubscription).toHaveBeenLastCalledWith(context.symbol, false)
})

it('上下文错股时信息条和图表都不显示旧股票数据', async () => {
  await render(context.symbol, context)
  await render('000001.SZ', context)
  expect(calls.info.mock.calls.at(-1)?.[0].rows).toEqual([])
  expect(calls.info.mock.calls.at(-1)?.[0].name).toBeUndefined()
  expect(host.querySelector('[data-chart]')).toBeNull()
  expect(calls.klineDaily).not.toHaveBeenCalled()
})

it('普通当日日K继续订阅报价，历史回春关闭报价订阅', async () => {
  await render()
  expect(calls.quoteSubscription).toHaveBeenLastCalledWith(context.symbol, true)
  await render(context.symbol, context)
  expect(calls.quoteSubscription).toHaveBeenLastCalledWith(context.symbol, false)
})

it('历史股票按指定区间请求日 K，并且只有来源日有行情时才打开该日分时', async () => {
  const history = { start: '2024-03-30', end: '2024-09-30' }
  const bar = { date: '2024-09-27', open: 10, high: 11, low: 9, close: 10, volume: 100 }
  const onVisibleTradeDate = vi.fn()
  const props = { symbol: '600000.SH', dateRange: history, preferredTradeDate: history.end, showIntraday: true, onVisibleTradeDate }
  calls.klineDaily.mockResolvedValue({ symbol: props.symbol, rows: [bar] })
  await act(async () => root.render(<QueryClientProvider client={client}><StockPanel {...props} /></QueryClientProvider>))
  for (let i = 0; i < 5; i++) await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
  expect(calls.klineDaily).toHaveBeenCalledWith(props.symbol, undefined, history, undefined)
  expect(calls.intraday).not.toHaveBeenCalled()
  expect(host.textContent).toContain('2024-09-30 暂无日 K')
  calls.klineDaily.mockResolvedValue({ symbol: props.symbol, rows: [bar, { ...bar, date: history.end }] })
  await act(async () => { await client.invalidateQueries({ queryKey: ['kline', props.symbol] }) })
  for (let i = 0; i < 3; i++) await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
  expect(calls.intraday.mock.calls.at(-1)?.[0].date).toBe(history.end)
  await act(() => calls.chart.mock.calls.at(-1)?.[0].onDateClick('2024-09-27'))
  expect(calls.intraday.mock.calls.at(-1)?.[0].date).toBe('2024-09-27')
  expect(onVisibleTradeDate).toHaveBeenLastCalledWith('2024-09-27')
  for (const symbol of ['000001.SZ', props.symbol]) {
    await act(async () => root.render(<QueryClientProvider client={client}><StockPanel {...props} symbol={symbol} /></QueryClientProvider>))
    for (let i = 0; i < 3; i++) await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
  }
  expect(calls.intraday.mock.calls.at(-1)?.[0].date).toBe(history.end)
  expect(onVisibleTradeDate).toHaveBeenLastCalledWith(history.end)
})
