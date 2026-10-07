// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { Monitor } from './Monitor'

const fixtures = vi.hoisted(() => ({ alerts: vi.fn() }))
vi.mock('@/lib/api', () => ({
  api: {
    alertsList: (...args: unknown[]) => fixtures.alerts(...args),
    alertsClear: vi.fn(),
    monitorRuleDelete: vi.fn(),
    monitorRulesList: async () => ({ rules: [{
      id: 'mode-rule', name: '回春扫描通知', type: 'huichun', enabled: true,
      scope: 'all', asset_type: 'stock', symbols: [], conditions: [], mode_events: ['pending_cross'],
    }] }),
    instrumentNames: async () => ({ names: {} }),
    watchlistGroups: async () => ({ groups: [] }),
    watchlistList: async () => ({ symbols: [] }),
    extDataSchemaAll: async () => ({ items: [] }),
  },
}))
vi.mock('@/lib/useSharedQueries', () => ({
  usePreferences: () => ({ data: { monitor_ext_fields: { concept: null, industry: null } } }),
  useQuoteStatus: () => ({ data: { mode: 'polling', is_trading_hours: false } }),
}))
vi.mock('@/lib/useCustomSignalNames', () => ({ useCustomSignalNames: () => ({}) }))
vi.mock('@/components/StockPreviewDialog', () => ({ StockPreviewDialog: () => null }))
vi.mock('@/components/DimensionMembersDialog', () => ({ DimensionMembersDialog: () => null }))
vi.mock('@/components/monitor/RuleEditor', () => ({
  RuleEditor: ({ preset }: { preset?: { type?: string } }) => <div data-editor>{preset?.type}</div>,
}))

let host: HTMLDivElement
let root: Root
let client: QueryClient
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  fixtures.alerts.mockReset().mockImplementation(async ({ source }: { source?: string }) => {
    const alerts = [
      { ts: 1, source: 'first_board', type: 'sealed', symbol: '600000.SH', name: '首板样本', message: '涨停价观察事件' },
      { ts: 2, source: 'huichun', type: 'a0_confirmed', symbol: '600001.SH', name: '回春样本', message: '扫描确认事件' },
    ].filter(event => !source || source === event.source)
    return { alerts, total: alerts.length }
  })
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity, gcTime: Infinity } } })
})
afterEach(async () => {
  await act(async () => root.unmount())
  client.clear()
  host.remove()
})
async function settle() {
  for (let i = 0; i < 3; i++) await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
}
async function render(path = '/monitor') {
  await act(async () => root.render(
    <MemoryRouter initialEntries={[path]} future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
      <QueryClientProvider client={client}><Monitor /></QueryClientProvider>
    </MemoryRouter>,
  ))
  await settle()
}

it('filters first-board and huichun alerts and presents the configured event summary', async () => {
  await render()
  expect(host.textContent).toContain('涨停价观察')
  expect(host.textContent).toContain('A0 确认')
  expect(host.textContent).toContain('待金叉观察')
  expect(host.textContent).toContain('日线扫描后确认')
  for (const [label, source, visible, hidden] of [
    ['首板模式', 'first_board', '首板样本', '回春样本'],
    ['回春模式', 'huichun', '回春样本', '首板样本'],
  ]) {
    const button = Array.from(host.querySelectorAll('button')).find(item => item.textContent === label)
    expect(button).toBeDefined()
    await act(async () => button!.click())
    await settle()
    expect(fixtures.alerts).toHaveBeenLastCalledWith(expect.objectContaining({ source }))
    expect(host.textContent).toContain(visible)
    expect(host.textContent).not.toContain(hidden)
  }
})

it.each(['first_board', 'huichun'])('opens the %s rule editor from its deep link', async type => {
  await render(`/monitor?new=${type}`)
  expect(host.querySelector('[data-editor]')?.textContent).toBe(type)
})
