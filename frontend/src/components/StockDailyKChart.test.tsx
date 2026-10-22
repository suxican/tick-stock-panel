// @vitest-environment jsdom
import { act, type ComponentProps } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { StockDailyKChart } from './StockDailyKChart'

const calls = vi.hoisted(() => ({ klineDaily: vi.fn(), huichunChart: vi.fn(), chart: vi.fn() }))
vi.mock('@/lib/api', () => ({ api: calls }))
vi.mock('./EChartsCandlestick', async importOriginal => ({
  ...await importOriginal<typeof import('./EChartsCandlestick')>(),
  EChartsCandlestick: (props: unknown) => { calls.chart(props); return <div data-chart="daily" /> },
}))

const range = { start: '2026-01-01', end: '2026-10-22' }
const context = {
  symbol: '600000.SH', as_of: '2026-09-30', rule_revision: 2,
  rules: { warmup_bars: 250, rally_threshold: .4, zero_threshold: .02, ma_window: 60, slope_lag: 5 },
  g_date: '2026-04-20', d_date: '2026-07-15', signal_date: '2026-09-30',
}
function response(symbol = '600000.SH', dif = .31) {
  return { symbol, source: 'huichun_a0', rows: ['2026-04-20', '2026-07-15', '2026-09-30'].map(date => ({
    date, open: 10, high: 11, low: 9, close: 10, volume: 100, macd_dif: dif, macd_dea: .3, macd_hist: .02,
  })), huichun: { as_of: '2026-09-30', macd: [10, 20, 9], price_basis: 'as_of_adjusted', price_basis_date: '2026-09-30', latest_valid_date: '2026-09-30',
    factor_status: 'events_present_unverified', invalid_from: null, calendar_source: 'official_exchange_calendar', data_status: 'end_reached',
    missing_observed_days: 0 as number | null, segment_count: 1 as number | null, rules: context.rules, rule_revision: context.rule_revision } }
}

let host: HTMLDivElement
let root: Root
let client: QueryClient
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  Object.values(calls).forEach(call => call.mockReset())
  calls.klineDaily.mockResolvedValue({ ...response('600000.SH', -.7), source: 'enriched' })
  calls.huichunChart.mockResolvedValue(response())
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } })
})
afterEach(async () => {
  await act(async () => root.unmount())
  client.clear()
  host.remove()
})
async function settle() {
  for (let i = 0; i < 5; i++) await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
}
async function render(extra: Record<string, unknown> = {}) {
  const props = { symbol: '600000.SH', dateRange: range, ...extra } as ComponentProps<typeof StockDailyKChart>
  await act(async () => root.render(<QueryClientProvider client={client}><StockDailyKChart {...props} /></QueryClientProvider>))
  await settle()
}
function lastChart() { return calls.chart.mock.calls.at(-1)?.[0] }

it('普通日K继续使用已有指标与查询，不请求回春端点', async () => {
  await render()
  expect(calls.klineDaily).toHaveBeenCalledWith('600000.SH', undefined, range, undefined)
  expect(calls.huichunChart).not.toHaveBeenCalled()
  expect(lastChart().data[0].macd_dif).toBe(-.7)
  expect(lastChart().activeIndicators).toEqual(['vol'])
})

it('回春日K展示后端完整历史计算的10/20/9及G/D/R形态日期', async () => {
  await render({ huichunChart: context })
  expect(calls.huichunChart).toHaveBeenCalledWith({
    symbol: context.symbol, as_of: context.as_of, rules: context.rules, rule_revision: context.rule_revision,
    start_date: range.start, end_date: context.as_of,
  })
  expect(calls.klineDaily).not.toHaveBeenCalled()
  expect(lastChart().data[0].macd_dif).toBe(.31)
  expect(lastChart().activeIndicators).toContain('macd')
  expect(lastChart().markers).toEqual(expect.arrayContaining([
    expect.objectContaining({ date: context.g_date, label: 'G', kind: 'neutral' }),
    expect.objectContaining({ date: context.d_date, label: 'D', kind: 'neutral' }),
    expect.objectContaining({ date: context.signal_date, label: 'R', kind: 'neutral' }),
  ]))
  expect(host.textContent).toContain('MACD 10 / 20 / 9')
  expect(host.textContent).toContain('非成交点')
})

it('专用端点失败时显示原因，不把已有普通日K当作回春指标', async () => {
  await render()
  calls.chart.mockClear()
  calls.klineDaily.mockClear()
  calls.huichunChart.mockRejectedValue(new Error('复权数据不可用'))
  await render({ huichunChart: context })
  expect(host.textContent).toContain('回春日K加载失败：复权数据不可用')
  expect(host.querySelector('[data-chart]')).toBeNull()
  expect(calls.chart).not.toHaveBeenCalled()
  expect(calls.klineDaily).not.toHaveBeenCalled()
})

it('切换回春股票时清除旧曲线和标记，等待新股票数据', async () => {
  await render({ huichunChart: context })
  let complete!: (value: ReturnType<typeof response>) => void
  calls.huichunChart.mockImplementationOnce(() => new Promise(resolve => { complete = resolve }))
  calls.chart.mockClear()
  const next = { ...context, symbol: '000001.SZ', g_date: undefined, d_date: undefined }
  await render({ symbol: next.symbol, huichunChart: next })
  expect(host.querySelector('[data-chart]')).toBeNull()
  expect(calls.chart).not.toHaveBeenCalled()
  await act(async () => complete(response(next.symbol, .8)))
  await settle()
  expect(lastChart().data[0].macd_dif).toBe(.8)
  expect(lastChart().markers.map((marker: { label: string }) => marker.label)).toEqual(['R'])
})

it('旧形态上下文与当前股票不匹配时不显示缓存或退回普通日K', async () => {
  await render({ huichunChart: context })
  calls.huichunChart.mockClear()
  calls.chart.mockClear()
  await render({ symbol: '000001.SZ', huichunChart: context })
  expect(host.textContent).toContain('回春形态与当前股票不匹配')
  expect(host.querySelector('[data-chart]')).toBeNull()
  expect(calls.huichunChart).not.toHaveBeenCalled()
  expect(calls.klineDaily).not.toHaveBeenCalled()
})

it('规则版本变化独立获取序列，等待期间不复用旧规则曲线', async () => {
  await render({ huichunChart: context })
  let complete!: (value: ReturnType<typeof response>) => void
  calls.huichunChart.mockImplementationOnce(() => new Promise(resolve => { complete = resolve }))
  const next = { ...context, rule_revision: 3, rules: { ...context.rules, ma_window: 90 } }
  await render({ huichunChart: next })
  expect(host.querySelector('[data-chart]')).toBeNull()
  const result = response()
  result.huichun.rules = next.rules
  result.huichun.rule_revision = 3
  await act(async () => complete(result))
  await settle()
  expect(calls.huichunChart).toHaveBeenCalledTimes(2)
  expect(lastChart().trendMaPeriod).toBe(90)
  expect(host.textContent).toContain('规则版本 3')
})

it('空数据明确展示，不绘制伪造的零值', async () => {
  const result = response()
  result.rows = []
  result.huichun.data_status = 'no_valid_data'
  calls.huichunChart.mockResolvedValue(result)
  await render({ huichunChart: context })
  expect(host.textContent).toContain('该范围暂无可用的回春日线数据')
  expect(host.querySelector('[data-chart]')).toBeNull()
})

it('因子异常导致数据截止早于形态日时标出真正基准和截断日期', async () => {
  const result = response()
  Object.assign(result.huichun, { price_basis_date: '2026-09-18', latest_valid_date: '2026-09-18', invalid_from: '2026-09-21', data_status: 'truncated', factor_status: 'quarantined' })
  calls.huichunChart.mockResolvedValue(result)
  await render({ huichunChart: context })
  expect(host.textContent).toContain('统一使用 2026-09-18 的复权基准')
  expect(host.textContent).toContain('有效日线仅到 2026-09-18')
  expect(host.textContent).toContain('2026-09-21 起复权数据异常')
})

it('回春接口意外返回另一套指标时拒绝绘制', async () => {
  const result = response()
  result.huichun.macd = [12, 26, 9]
  calls.huichunChart.mockResolvedValue(result)
  await render({ huichunChart: context })
  expect(host.querySelector('[data-chart]')).toBeNull()
  expect(host.textContent).toContain('回春图表数据与所选股票、日期或规则不匹配')
})

it('末日已到达仍展示内部缺口，无法核验时不声称零缺口', async () => {
  const result = response()
  result.huichun.missing_observed_days = 2
  result.huichun.segment_count = 3
  calls.huichunChart.mockResolvedValue(result)
  await render({ huichunChart: context })
  expect(host.textContent).toContain('历史含 2 个缺失或不可用交易日')
  expect(host.textContent).toContain('（3 段）')
  expect(host.textContent).not.toContain('完整日线')
  calls.huichunChart.mockResolvedValue({ ...result, huichun: { ...result.huichun, missing_observed_days: null, segment_count: null } })
  await act(async () => { await client.invalidateQueries({ queryKey: ['huichun', 'chart'] }) })
  await settle()
  expect(host.textContent).toContain('历史缺口覆盖暂无法核验')
  expect(host.textContent).not.toContain('历史含 0 个')
})

it('日期范围晚于形态日时提示调整范围且不发送无效查询', async () => {
  await render({ huichunChart: context, dateRange: { start: '2026-10-08', end: '2026-10-22' } })
  expect(host.textContent).toContain('所选日期范围晚于形态日期')
  expect(host.textContent).not.toContain('数据格式异常')
  expect(calls.huichunChart).not.toHaveBeenCalled()
  expect(calls.klineDaily).not.toHaveBeenCalled()
})
