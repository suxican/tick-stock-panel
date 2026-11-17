// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { KaipanlaSettings } from './KaipanlaSettings'
import extension from './extension'

const calls = vi.hoisted(() => ({ kaipanlaStatus: vi.fn(), kaipanlaSetEnabled: vi.fn() }))
vi.mock('@/lib/api', async importOriginal => ({
  ...await importOriginal<typeof import('@/lib/api')>(), api: calls,
}))

let host: HTMLDivElement
let root: Root
let client: QueryClient

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.resetAllMocks()
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

async function render() {
  await act(async () => {
    root.render(<QueryClientProvider client={client}><KaipanlaSettings /></QueryClientProvider>)
    await new Promise(resolve => setTimeout(resolve, 20))
  })
  await act(async () => {
    await new Promise(resolve => setTimeout(resolve, 20))
  })
}

it('显示风险说明, 点击开关调用启用接口', async () => {
  calls.kaipanlaStatus.mockResolvedValue({ enabled: false, last_success_at: null, last_error: null })
  calls.kaipanlaSetEnabled.mockResolvedValue({ enabled: true })
  await render()
  expect(host.textContent).toContain('非官方接口，仅供个人研究；App 升级可能失效')
  const toggle = host.querySelector('[role="switch"]') as HTMLButtonElement
  expect(toggle.getAttribute('aria-checked')).toBe('false')
  await act(async () => { toggle.click(); await new Promise(resolve => setTimeout(resolve, 10)) })
  expect(calls.kaipanlaSetEnabled).toHaveBeenCalledWith(true)
})

it('显示最近成功时间与最近错误', async () => {
  calls.kaipanlaStatus.mockResolvedValue({
    enabled: true, last_success_at: '2026-11-17T10:30:05+08:00', last_error: 'HTTP 500',
  })
  await render()
  expect(host.textContent).toContain('10:30:05')
  expect(host.textContent).toContain('HTTP 500')
})

it('经 settings.system.section 插槽注册', () => {
  expect(extension.id).toBe('official.kaipanla')
  expect(extension.slots?.[0].name).toBe('settings.system.section')
})
