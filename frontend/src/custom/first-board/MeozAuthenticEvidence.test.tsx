// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { ApiError } from '@/lib/api'
import { MeozAuthenticEvidence } from './MeozAuthenticEvidence'

const calls = vi.hoisted(() => ({ authentic: vi.fn() }))
vi.mock('@/lib/api', async original => ({
  ...await original<typeof import('@/lib/api')>(),
  api: { meozThemeAuthenticity: calls.authentic },
}))

let host: HTMLDivElement
let root: Root

async function render() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  await act(async () => {
    root.render(<QueryClientProvider client={client}><MeozAuthenticEvidence symbol="688388.SH" /></QueryClientProvider>)
  })
  for (let i = 0; i < 3; i++) await act(async () => { await new Promise(r => setTimeout(r, 0)) })
}

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  calls.authentic.mockReset()
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
})
afterEach(() => { act(() => root.unmount()); host.remove() })

it('列出所在的最正宗精选板块与当日涨跌', async () => {
  calls.authentic.mockResolvedValue({ available: true, themes: [
    { theme_id: '801004k', theme_name: '锂电池', change_pct: -0.0345 },
    { theme_id: '801660k', theme_name: null, change_pct: null },
  ] })
  await render()
  expect(calls.authentic).toHaveBeenCalledWith('688388.SH')
  const text = host.textContent!
  expect(text).toContain('精选板块最正宗 · 锂电池（当日 -3.45%）；801660k（当日涨跌未知）')
  expect(text).toContain('当前名单，仅作参考')
})

it('未列入名单与来源不可用文案不同', async () => {
  calls.authentic.mockResolvedValue({ available: true, themes: [] })
  await render()
  expect(host.textContent).toContain('未列入任何精选板块的最正宗名单')
  act(() => root.unmount())
  root = createRoot(host)
  calls.authentic.mockRejectedValue(new ApiError('x', 503))
  await render()
  expect(host.textContent).toContain('精选板块最正宗 · 来源不可用')
  expect(host.textContent).not.toContain('未列入')
})
