// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { StockPreviewDialogContent, type Props } from './StockPreviewDialogContent'
import type { HuichunChartContext } from '@/lib/huichun-chart'

const calls = vi.hoisted(() => ({
  watchlistList: vi.fn(), monitorRulesList: vi.fn(), panel: vi.fn(), minutes: vi.fn(), datePicker: vi.fn(),
}))
const clock = vi.hoisted(() => ({ now: Date.parse('2026-10-22T02:00:00Z') }))
vi.mock('@/lib/api', () => ({ api: calls }))
vi.mock('@/lib/useSharedQueries', () => ({ usePreferences: () => ({ data: {} }) }))
vi.mock('@/lib/useCustomSignalNames', () => ({ useCustomSignalNames: () => ({}) }))
vi.mock('@/lib/useQuoteClock', () => ({ useQuoteClock: () => clock.now }))
vi.mock('./StockPanel', () => ({
  StockPanel: (props: unknown) => { calls.panel(props); return <div data-stock-panel="true" /> },
}))
vi.mock('./StockMultiDayIntradayChart', () => ({ StockMultiDayIntradayChart: (props: unknown) => { calls.minutes(props); return <div data-intraday="true" /> } }))
vi.mock('./WatchlistAddMenu', () => ({ WatchlistAddMenu: () => null }))
vi.mock('./monitor/RuleEditor', () => ({ RuleEditor: () => null }))
vi.mock('./stock-analysis/PriceAlertDialog', () => ({ PriceAlertDialog: () => null }))
vi.mock('@/extensions/ExtensionSlot', () => ({ ExtensionSlot: ({ context }: { context: { tradeDate?: string | null } }) => <output data-footer-day={context.tradeDate ?? ''} /> }))
vi.mock('./DatePicker', () => ({ DatePicker: (props: { value: string }) => { calls.datePicker(props); return <input readOnly value={props.value} /> } }))

const context: HuichunChartContext = {
  symbol: '600000.SH', as_of: '2024-09-30', rule_revision: 2,
  rules: { warmup_bars: 250, rally_threshold: .4, zero_threshold: .02, ma_window: 60, slope_lag: 5 },
  g_date: '2023-12-20', d_date: '2024-07-15', signal_date: '2024-09-30',
}
let host: HTMLDivElement
let root: Root
let client: QueryClient
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  Object.values(calls).forEach(call => call.mockReset())
  clock.now = Date.parse('2026-10-22T02:00:00Z')
  calls.watchlistList.mockResolvedValue({ symbols: [], groups: [] })
  calls.monitorRulesList.mockResolvedValue({ rules: [] })
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } })
})
afterEach(async () => { await act(async () => root.unmount()); client.clear(); host.remove() })
async function settle() { for (let i = 0; i < 4; i++) await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) }) }
async function render(huichunChart?: HuichunChartContext, props: Partial<Props> = {}) {
  await act(async () => root.render(<QueryClientProvider client={client}>
    <StockPreviewDialogContent symbol={huichunChart?.symbol ?? '600000.SH'} name="测试股票" onClose={() => {}} huichunChart={huichunChart} {...props} />
  </QueryClientProvider>))
  await settle()
}
async function click(label: string) {
  const button = [...host.querySelectorAll('button')].find(item => item.textContent?.trim() === label)
  expect(button).toBeDefined()
  await act(async () => button!.click())
  await settle()
}
function panel() { return calls.panel.mock.calls.at(-1)?.[0] }
function footerDay() { return host.querySelector('output')?.getAttribute('data-footer-day') }

it('归因优先使用梯队交易日，点击 K 棒后跟随该棒，切股不沿用旧选择', async () => {
  await render(undefined, { tradeDate: '2026-10-19' })
  expect(footerDay()).toBe('2026-10-19')
  await act(() => panel().onVisibleTradeDate('2026-10-16'))
  expect(footerDay()).toBe('2026-10-16')
  await render(undefined, { symbol: '000001.SZ', tradeDate: '2026-10-20' })
  expect(footerDay()).toBe('2026-10-20')
  await render(undefined, { tradeDate: null })
  expect(footerDay()).toBe('')
})

it('普通预览等实际行情日期，切分时及切股时不沿用旧日期', async () => {
  await render()
  expect(footerDay()).toBe('')
  await act(() => panel().onVisibleTradeDate('2026-10-21'))
  expect(footerDay()).toBe('2026-10-21')
  await click('分时')
  expect(footerDay()).toBe('')
})

it('历史梯队初始及快捷范围以来源日为终点，更改范围和切分时按图内实际行情归因', async () => {
  await render(undefined, { tradeDate: '2024-09-30' })
  expect(panel().dateRange).toEqual({ start: '2024-03-30', end: '2024-09-30' })
  expect(panel().preferredTradeDate).toBe('2024-09-30')
  await click('1年')
  expect(panel().dateRange).toEqual({ start: '2023-09-30', end: '2024-09-30' })
  await act(() => calls.datePicker.mock.calls.at(-1)![0].onChange('2024-09-22'))
  expect(panel().dateRange.end).toBe('2024-09-22')
  expect(footerDay()).toBe('')
  await act(() => panel().onVisibleTradeDate('2024-09-20'))
  expect(footerDay()).toBe('2024-09-20')
  await click('分时')
  expect(panel().dateRange.end).toBe('2026-10-22')
  expect(footerDay()).toBe('')
  await act(() => calls.minutes.mock.calls.at(-1)![0].onLatestTradeDate('2026-10-21'))
  expect(footerDay()).toBe('2026-10-21')
})

it('回春预览传递完整上下文，默认范围覆盖G至R并停在形态日', async () => {
  await render(context)
  expect(panel().huichunChart).toEqual(context)
  expect(panel().dateRange).toEqual({ start: context.g_date, end: context.as_of })
  expect(panel().showIntraday).toBe(false)
  expect(panel().visibleBars).toBe('all')
  expect(panel().onPriceDoubleClick).toBeUndefined()
  await click('1年')
  expect(panel().dateRange).toEqual({ start: '2023-09-30', end: context.as_of })
})

it('显式查看普通分时时退出历史日K源并提示口径', async () => {
  await render(context)
  await click('分时')
  expect(panel().infoBarOnly).toBe(true)
  expect(panel().huichunChart).toBeUndefined()
  expect(panel().dateRange.end).toBe('2026-10-22')
  expect(host.textContent).toContain('此处为普通分时行情')
  expect(panel().renderer).toBe('lightweight')
})

it('普通预览保留默认范围和分时交互', async () => {
  await render()
  expect(panel().huichunChart).toBeUndefined()
  expect(panel().dateRange).toEqual({ start: '2026-04-22', end: '2026-10-22' })
  expect(panel().showIntraday).toBe(true)
  expect(panel().onPriceDoubleClick).toBeTypeOf('function')
  expect(panel().renderer).toBe('lightweight')
})

it('普通快捷范围随北京时间换日，手动范围保持不动', async () => {
  await render()
  clock.now = Date.parse('2026-10-22T16:05:00Z')
  await render()
  expect(panel().dateRange).toEqual({ start: '2026-04-23', end: '2026-10-23' })
  await click('1年')
  clock.now = Date.parse('2026-10-23T16:05:00Z')
  await render()
  expect(panel().dateRange).toEqual({ start: '2025-10-24', end: '2026-10-24' })
  await act(() => calls.datePicker.mock.calls.at(-1)![0].onChange('2026-10-20'))
  clock.now = Date.parse('2026-10-24T16:05:00Z')
  await render()
  expect(panel().dateRange).toEqual({ start: '2025-10-24', end: '2026-10-20' })
})

it('换日不推进历史来源日或回春形态日', async () => {
  await render(undefined, { tradeDate: '2024-09-30' })
  await click('1年')
  clock.now = Date.parse('2026-10-22T16:05:00Z')
  await render(undefined, { tradeDate: '2024-09-30' })
  expect(panel().dateRange).toEqual({ start: '2023-09-30', end: '2024-09-30' })
  await render(context)
  clock.now = Date.parse('2026-10-23T16:05:00Z')
  await render(context)
  expect(panel().dateRange).toEqual({ start: context.g_date, end: context.as_of })
})
