// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { SettingsDataSourcesPanel } from './DataSources'

const calls = vi.hoisted(() => ({
  dataSources: vi.fn(), capabilityMatrix: vi.fn(), updateDataProviders: vi.fn(), resetCapability: vi.fn(),
}))
vi.mock('@/lib/api', async importOriginal => ({
  ...await importOriginal<typeof import('@/lib/api')>(), api: calls,
}))
vi.mock('@/lib/useSharedQueries', () => ({
  usePreferences: () => ({ data: {} }), useCapabilities: () => ({ data: undefined }),
}))
vi.mock('./Keys', () => ({
  TickFlowKeySection: () => null, TierHelpPopover: () => null, useInvalidateTierRelated: () => vi.fn(),
}))
vi.mock('@/extensions/registry', () => ({
  getFrontendExtensionNavigation: () => [],
  getFrontendSlotRegistrations: () => [],
}))

const candidates = [
  { name: 'tdxquant', display: 'TdxQuant' },
  { name: 'tickflow', display: 'TickFlow' },
  { name: 'meoz', display: '猫爪数据' },
]

function depth5(overrides: Record<string, unknown> = {}) {
  return {
    id: 'depth5', label: '五档盘口', desc: '', field: 'depth5_data_provider', default: 'tickflow',
    tf_tier: 'pro', tf_available: true, usable: true, current: 'tdxquant', current_display: 'TdxQuant',
    effective: 'tdxquant', effective_display: 'TdxQuant', candidates, pending: [],
    fallbacks: ['tickflow', 'meoz'],
    chain: [
      { source: 'tdxquant', state: 'healthy', reason: null, until: null },
      { source: 'tickflow', state: 'healthy', reason: null, until: null },
      { source: 'meoz', state: 'healthy', reason: null, until: null },
    ],
    active_source: 'tdxquant', active_display: 'TdxQuant', degraded: false, fallback_reason: null,
    ...overrides,
  }
}

const daily = {
  id: 'daily', label: '日K', desc: '', field: 'daily_data_provider', default: 'tickflow', tf_tier: 'none',
  tf_available: true, usable: true, current: 'tickflow', current_display: 'TickFlow', effective: 'tickflow',
  effective_display: 'TickFlow', candidates: [{ name: 'tickflow', display: 'TickFlow' }], pending: [],
}

let host: HTMLDivElement
let root: Root
let client: QueryClient
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.resetAllMocks()
  calls.dataSources.mockResolvedValue({ builtin: [], custom: [], plugins: [] })
  calls.updateDataProviders.mockResolvedValue({})
  calls.resetCapability.mockResolvedValue({})
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
})
afterEach(async () => {
  await act(async () => root.unmount())
  client.clear()
  host.remove()
})

async function settle() {
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 10)) })
}
async function render(caps: unknown[]) {
  calls.capabilityMatrix.mockResolvedValue({ capabilities: caps })
  await act(async () => root.render(
    <MemoryRouter><QueryClientProvider client={client}><SettingsDataSourcesPanel /></QueryClientProvider></MemoryRouter>,
  ))
  await settle()
}
function card(label: string): HTMLElement {
  const el = [...host.querySelectorAll<HTMLElement>('[data-capability]')].find(e => e.dataset.capability === label)
  expect(el, label).toBeDefined()
  return el!
}
async function click(scope: HTMLElement, label: string) {
  const button = [...scope.querySelectorAll('button')].find(b => b.getAttribute('aria-label') === label || b.textContent?.trim() === label)
  expect(button, label).toBeDefined()
  await act(async () => button!.click())
  await settle()
}

it('按顺序列出兜底源, 下移后保存新顺序', async () => {
  await render([depth5()])
  const el = card('depth5')
  expect(el.textContent).toContain('兜底')
  expect(el.textContent).toMatch(/TickFlow[\s\S]*猫爪数据/)
  await click(el, '下移 TickFlow')
  expect(calls.updateDataProviders).toHaveBeenCalledWith({ depth5_data_provider_fallbacks: ['meoz', 'tickflow'] })
})

it('移除兜底源后保存剩余顺序', async () => {
  await render([depth5()])
  await click(card('depth5'), '移除 猫爪数据')
  expect(calls.updateDataProviders).toHaveBeenCalledWith({ depth5_data_provider_fallbacks: ['tickflow'] })
})

it('可从其余候选中添加兜底源', async () => {
  await render([depth5({ fallbacks: ['meoz'] })])
  const select = card('depth5').querySelector<HTMLSelectElement>('select[aria-label="添加兜底源"]')!
  expect([...select.options].map(o => o.value)).toEqual(['', 'tickflow'])
  await act(async () => {
    select.value = 'tickflow'
    select.dispatchEvent(new Event('change', { bubbles: true }))
  })
  await settle()
  expect(calls.updateDataProviders).toHaveBeenCalledWith({ depth5_data_provider_fallbacks: ['meoz', 'tickflow'] })
})

it('降级时显示兜底提示, 可重试主源', async () => {
  await render([depth5({
    degraded: true, active_source: 'meoz', active_display: '猫爪数据',
    fallback_reason: 'TdxQuant：连续 3 次失败，熔断至 10:35',
    chain: [
      { source: 'tdxquant', state: 'open', reason: '连续 3 次失败，熔断至 10:35', until: '2026-11-18T10:35:00+08:00' },
      { source: 'meoz', state: 'healthy', reason: null, until: null },
    ],
  })])
  const el = card('depth5')
  expect(el.textContent).toContain('当前由 猫爪数据 兜底')
  expect(el.querySelector('[title*="连续 3 次失败"]')).not.toBeNull()
  await click(el, '重试主源')
  expect(calls.resetCapability).toHaveBeenCalledWith('depth5')
})

it('非兜底能力不显示兜底行', async () => {
  await render([daily])
  expect(card('daily').textContent).not.toContain('兜底')
})
