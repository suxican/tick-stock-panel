// @vitest-environment jsdom
import { act, useState } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { api } from '@/lib/api'
import { storage } from '@/lib/storage'
import { Watchlist } from './Watchlist'

const rows = [
  { symbol: '600000.SH', name: '浦发银行', rt_name: '浦发银行实时名', asset_type: 'stock', close: 10, change_pct: 0.01 },
  { symbol: '000001.SH', name: '上证指数', asset_type: 'index', close: 3000, change_pct: 0 },
  { symbol: '159558.SZ', name: '测试ETF', asset_type: 'etf', close: 0.827, change_pct: 0 },
]

vi.mock('@/lib/api', async importOriginal => {
  const original = await importOriginal<typeof import('@/lib/api')>()
  return { ...original, api: { ...original.api,
    watchlistList: vi.fn(), watchlistGroups: vi.fn(), watchlistEnriched: vi.fn(),
    stockAnalysisLevels: vi.fn(), stockAnalysisReportsList: vi.fn(),
  } }
})
vi.mock('@/lib/useSharedQueries', () => ({
  usePreferences: () => ({ data: {} }),
  useCapabilities: () => ({ data: { capabilities: {} } }),
  useQuoteStatus: () => ({ data: { running: false, is_trading_hours: false } }),
}))
vi.mock('@/lib/watchlist-columns', async importOriginal => {
  const original = await importOriginal<typeof import('@/lib/watchlist-columns')>()
  const columns = original.BUILTIN_COLUMNS.filter(column => column.id === 'builtin:symbol')
  return { ...original, BUILTIN_COLUMNS: columns, loadColumnConfig: async () => columns }
})
vi.mock('@/lib/intraday-auction', () => ({ useAuctionConfig: () => null, useAuctionSeries: () => ({}) }))
vi.mock('@/components/stock-analysis/WatchlistAnalysisDialog', () => ({
  WatchlistAnalysisDialog: ({ symbol, name, onClose }: { symbol: string; name: string; onClose: () => void }) => {
    const [expanded, setExpanded] = useState(false)
    return <section role="dialog" aria-label="关键分析">
      <output>{symbol} {name}</output>
      <button onClick={() => setExpanded(true)}>展开历史</button>
      {expanded && <p>历史已展开</p>}
      <button onClick={onClose}>关闭分析</button>
    </section>
  },
}))
vi.mock('@/components/StockPreviewDialog', () => ({
  StockPreviewDialog: ({ symbol }: { symbol: string | null }) => symbol ? <output data-testid="preview">{symbol}</output> : null,
}))
vi.mock('@/components/DimensionMembersDialog', () => ({
  DimensionMembersDialog: () => null, dimensionKindForSourceField: () => null,
}))
vi.mock('@/components/ColumnCustomizer', () => ({ ColumnCustomizer: () => null }))
vi.mock('@/components/WatchlistImportDialog', () => ({ WatchlistImportDialog: () => null }))
vi.mock('@/components/WatchlistGroups', () => ({ WatchlistGroupBar: () => null, WatchlistGroupPicker: () => null }))
vi.mock('@/components/WatchlistGroupCards', () => ({ WatchlistGroupCards: () => null }))
vi.mock('@/components/WatchlistGroupStatsBar', () => ({ WatchlistGroupStatsBar: () => null }))
vi.mock('@/components/WatchlistAddMenu', () => ({ WatchlistAddMenu: () => null }))
vi.mock('@/extensions/ExtensionSlot', () => ({ ExtensionSlot: () => null }))
vi.mock('@/components/stock-table/MiniCandlestick', () => ({ MiniCandlestick: () => null }))
vi.mock('@/components/stock-table/MiniIntraday', () => ({ MiniIntraday: () => null }))
vi.mock('@/components/screener/ExtSparkChart', () => ({ ExtSparkChart: () => null }))

let host: HTMLDivElement
let root: Root
let client: QueryClient

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.clearAllMocks()
  localStorage.clear()
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} unobserve() {} })
  vi.stubGlobal('fetch', vi.fn(() => { throw new Error('Unexpected network request in Watchlist analysis test') }))
  vi.mocked(api.watchlistList).mockResolvedValue({ symbols: rows.map(row => ({ symbol: row.symbol, group_ids: [] })) } as any)
  vi.mocked(api.watchlistGroups).mockResolvedValue({ groups: [] })
  vi.mocked(api.watchlistEnriched).mockResolvedValue({ rows, as_of: '2026-11-06', elapsed_ms: 0 } as any)
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
})

afterEach(async () => {
  await act(async () => root.unmount())
  client.clear()
  host.remove()
  vi.unstubAllGlobals()
})

async function settle() {
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 20)) })
}

async function render(view: 'table' | 'card') {
  storage.watchlistView.set(view)
  await act(async () => root.render(<MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
    <QueryClientProvider client={client}><Watchlist /></QueryClientProvider>
  </MemoryRouter>))
  await settle()
  await settle()
}

function button(label: string) {
  const result = [...host.querySelectorAll('button')].find(node => node.getAttribute('aria-label') === label || node.textContent === label)
  expect(result, `button ${label}`).toBeTruthy()
  return result!
}

async function click(label: string) {
  await act(async () => button(label).click())
  await settle()
}

it.each(['table', 'card'] as const)('%s opens analysis only on demand for the chosen stock, without opening its preview', async view => {
  await render(view)
  expect(host.querySelector('[role="dialog"]')).toBeNull()
  expect(api.stockAnalysisLevels).not.toHaveBeenCalled()
  expect(api.stockAnalysisReportsList).not.toHaveBeenCalled()

  await click('查看浦发银行实时名关键分析')

  const dialog = host.querySelector('[role="dialog"]')!
  expect(dialog.textContent).toContain('600000.SH 浦发银行实时名')
  expect(host.querySelector('[data-testid="preview"]')).toBeNull()
  expect(fetch).not.toHaveBeenCalled()
})

it.each(['table', 'card'] as const)('%s disables ETF analysis and explains the unsupported asset', async view => {
  await render(view)
  const entry = button('ETF 暂不支持关键价位分析')
  expect(entry.disabled).toBe(true)
  expect(entry.title).toBe('ETF 暂不支持关键价位分析')
  await act(async () => entry.click())
  expect(host.querySelector('[role="dialog"]')).toBeNull()
  expect(host.querySelector('[data-testid="preview"]')).toBeNull()
})

it('closes and remounts analysis with fresh temporary state and the next selected asset', async () => {
  await render('table')
  await click('查看浦发银行实时名关键分析')
  await click('展开历史')
  expect(host.textContent).toContain('历史已展开')
  await click('关闭分析')
  expect(host.querySelector('[role="dialog"]')).toBeNull()

  await click('查看上证指数关键分析')
  expect(host.querySelector('[role="dialog"]')!.textContent).toContain('000001.SH 上证指数')
  expect(host.textContent).not.toContain('历史已展开')
  expect(host.querySelector('[data-testid="preview"]')).toBeNull()
})
