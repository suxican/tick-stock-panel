// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { MonitorRule } from '@/lib/api'
import { RuleEditor } from './RuleEditor'

const fixtures = vi.hoisted(() => ({ save: vi.fn(), close: vi.fn() }))
vi.mock('@/lib/api', () => ({
  genRuleId: () => 'new-rule',
  api: {
    monitorRuleOptions: async () => ({
      types: [
        { key: 'signal', label: '信号' }, { key: 'strategy', label: '策略监控' },
        { key: 'first_board', label: '首板模式' }, { key: 'huichun', label: '回春模式' },
      ],
      scopes: [
        { key: 'all', label: '全市场' }, { key: 'symbols', label: '指定标的' },
        { key: 'watchlist_group', label: '自选分组' }, { key: 'sector', label: '板块' },
      ],
      severities: [{ key: 'info', label: '信息' }],
      builtin_signals: [],
    }),
    screenerStrategies: async () => ({ presets: [] }),
    watchlistList: async () => ({ symbols: [] }),
    watchlistGroups: async () => ({ groups: [] }),
    instrumentNames: async () => ({ names: {} }),
    monitorRuleSave: (...args: unknown[]) => fixtures.save(...args),
  },
}))
vi.mock('@/lib/useSharedQueries', () => ({
  usePreferences: () => ({ data: { webhook_default_channels: [] } }),
  useQuoteStatus: () => ({ data: { mode: 'polling' } }),
}))
vi.mock('@/components/screener/SignalPicker', () => ({ SignalPicker: () => <div>信号选择</div> }))

let host: HTMLDivElement
let root: Root
let client: QueryClient
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  fixtures.save.mockReset().mockResolvedValue({})
  fixtures.close.mockReset()
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
async function render(rule: MonitorRule | null = null, preset?: Partial<MonitorRule>) {
  await act(async () => root.render(
    <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}><QueryClientProvider client={client}>
      <RuleEditor rule={rule} preset={preset} onClose={fixtures.close} />
    </QueryClientProvider></MemoryRouter>,
  ))
  await settle()
}
async function click(text: string) {
  const button = Array.from(host.querySelectorAll('button')).find(item => item.textContent === text)
  expect(button).toBeDefined()
  await act(async () => button!.click())
  await settle()
}
function checkedEvents() {
  return Array.from(host.querySelectorAll<HTMLInputElement>('fieldset input:checked')).map(input => input.value)
}

it('creates a first-board rule with stock scope, event defaults and no generic conditions', async () => {
  await render()
  await click('首板模式')
  expect(host.textContent).toContain('自动盯盘')
  expect(host.textContent).toContain('消息通知')
  expect(host.textContent).not.toContain('资产类型')
  expect(host.textContent).not.toContain('信号选择')
  expect(host.querySelector('select')?.value).toBe('all')
  expect(Array.from(host.querySelector('select')!.options).map(option => option.value)).not.toContain('sector')
  expect(checkedEvents()).toEqual(['buy_candidate', 'broken', 'exit_candidate'])
  await click('保存')
  expect(fixtures.save).toHaveBeenCalledWith(expect.objectContaining({
    type: 'first_board', asset_type: 'stock', scope: 'all', name: '首板模式监控',
    mode_events: ['buy_candidate', 'broken', 'exit_candidate'], conditions: [],
  }))
  expect(fixtures.close).toHaveBeenCalledTimes(1)
})

it('resets mode events on type switches and rejects an empty event selection', async () => {
  await render()
  await click('首板模式')
  await click('回春模式')
  expect(checkedEvents()).toEqual(['a0_confirmed'])
  expect(host.textContent).toContain('日线扫描完成后')
  const confirmEvent = host.querySelector<HTMLInputElement>('input[value="a0_confirmed"]')!
  await act(async () => confirmEvent.click())
  await click('保存')
  expect(fixtures.save).not.toHaveBeenCalled()
  expect(host.textContent).toContain('至少选择一个模式事件')
  const pendingEvent = host.querySelector<HTMLInputElement>('input[value="pending_cross"]')!
  await act(async () => pendingEvent.click())
  await click('保存')
  expect(fixtures.save).toHaveBeenCalledWith(expect.objectContaining({ type: 'huichun', mode_events: ['pending_cross'] }))
})

it('preserves explicit events and symbol scope while editing, and shows save failures', async () => {
  fixtures.save.mockRejectedValue(new Error('保存失败，请重试'))
  await render({
    id: 'existing', type: 'huichun', name: '回春自选', enabled: true, asset_type: 'stock',
    scope: 'symbols', symbols: ['600000.SH'], conditions: [], mode_events: ['pending_cross'],
    direction: 'entry', logic: 'or', cooldown_seconds: 1800, severity: 'info', message: '',
  })
  expect(checkedEvents()).toEqual(['pending_cross'])
  await click('保存')
  expect(fixtures.save).toHaveBeenCalledWith(expect.objectContaining({
    id: 'existing', scope: 'symbols', symbols: ['600000.SH'], mode_events: ['pending_cross'],
  }))
  expect(host.textContent).toContain('保存失败，请重试')
  expect(fixtures.close).not.toHaveBeenCalled()
})

it('initializes a mode deep link as a stock rule for the whole market', async () => {
  await render(null, { type: 'huichun' })
  await click('保存')
  expect(fixtures.save).toHaveBeenCalledWith(expect.objectContaining({
    type: 'huichun', asset_type: 'stock', scope: 'all', mode_events: ['a0_confirmed'],
  }))
})
