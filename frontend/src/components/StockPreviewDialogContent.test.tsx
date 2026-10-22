// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { StockPreviewDialogContent } from './StockPreviewDialogContent'
import type { HuichunChartContext } from '@/lib/huichun-chart'

const calls = vi.hoisted(() => ({
  watchlistList: vi.fn(), monitorRulesList: vi.fn(), panel: vi.fn(), setFocusSymbol: vi.fn(), clearFocusSymbol: vi.fn(),
}))
vi.mock('@/lib/api', () => ({ api: calls }))
vi.mock('@/lib/useSharedQueries', () => ({ usePreferences: () => ({ data: {} }) }))
vi.mock('@/lib/useCustomSignalNames', () => ({ useCustomSignalNames: () => ({}) }))
vi.mock('@/lib/useQuoteStream', () => ({ setFocusSymbol: calls.setFocusSymbol, clearFocusSymbol: calls.clearFocusSymbol }))
vi.mock('./StockPanel', () => ({
  getDefaultRange: () => ({ start: '2026-04-22', end: '2026-10-22' }),
  StockPanel: (props: unknown) => { calls.panel(props); return <div data-stock-panel="true" /> },
}))
vi.mock('./StockMultiDayIntradayChart', () => ({ StockMultiDayIntradayChart: () => <div data-intraday="true" /> }))
vi.mock('./WatchlistAddMenu', () => ({ WatchlistAddMenu: () => null }))
vi.mock('./monitor/RuleEditor', () => ({ RuleEditor: () => null }))
vi.mock('./stock-analysis/PriceAlertDialog', () => ({ PriceAlertDialog: () => null }))
vi.mock('@/extensions/ExtensionSlot', () => ({ ExtensionSlot: () => null }))
vi.mock('./DatePicker', () => ({ DatePicker: ({ value }: { value: string }) => <input readOnly value={value} /> }))

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
  calls.watchlistList.mockResolvedValue({ symbols: [], groups: [] })
  calls.monitorRulesList.mockResolvedValue({ rules: [] })
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } })
})
afterEach(async () => { await act(async () => root.unmount()); client.clear(); host.remove() })
async function settle() { for (let i = 0; i < 4; i++) await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) }) }
async function render(huichunChart?: HuichunChartContext) {
  await act(async () => root.render(<QueryClientProvider client={client}>
    <StockPreviewDialogContent symbol={huichunChart?.symbol ?? '600000.SH'} name="测试股票" onClose={() => {}} huichunChart={huichunChart} />
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

it('回春预览传递完整上下文，默认范围覆盖G至R并停在形态日', async () => {
  await render(context)
  expect(panel().huichunChart).toEqual(context)
  expect(panel().dateRange).toEqual({ start: context.g_date, end: context.as_of })
  expect(panel().showIntraday).toBe(false)
  expect(panel().visibleBars).toBe('all')
  expect(panel().onPriceDoubleClick).toBeUndefined()
  expect(calls.setFocusSymbol).not.toHaveBeenCalled()
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
  expect(calls.setFocusSymbol).toHaveBeenCalledWith(context.symbol)
})

it('普通预览保留默认范围和分时交互', async () => {
  await render()
  expect(panel().huichunChart).toBeUndefined()
  expect(panel().dateRange).toEqual({ start: '2026-04-22', end: '2026-10-22' })
  expect(panel().showIntraday).toBe(true)
  expect(panel().onPriceDoubleClick).toBeTypeOf('function')
  expect(calls.setFocusSymbol).toHaveBeenCalledWith('600000.SH')
})
