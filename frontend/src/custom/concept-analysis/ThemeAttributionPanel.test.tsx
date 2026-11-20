// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { ApiError } from '@/lib/api'
import { ThemeAttributionPanel } from './ThemeAttributionPanel'

const calls = vi.hoisted(() => ({ reasons: vi.fn(), hot: vi.fn() }))
vi.mock('@/lib/api', async original => ({
  ...await original<typeof import('@/lib/api')>(),
  api: { meozThemeReasons: calls.reasons, meozHotRank: calls.hot },
}))

let host: HTMLDivElement
let root: Root
const hot = {
  fetched_at: '2026-11-20T10:00:00+08:00',
  stocks: {},
  themes: { available: true, rows: [{ rank: 1, theme_id: '801001', name: '芯片', available_at: '2026-11-20T09:55:00+08:00' }] },
}
const reasons = {
  date: '2026-11-20', source: 'live', final: false, unavailable_sources: [], fetched_at: '2026-11-20T10:00:00+08:00',
  rows: [
    { source: 'xgb', theme_id: '1x', name: '养猪', reason: '生猪涨价' },
    { source: 'jygs', theme_id: '2j', name: '养猪', reason: '猪周期' },
    { source: 'xgb', theme_id: '3x', name: '芯片', reason: '涨价潮' },
  ],
}

async function render(onSelect = vi.fn()) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  await act(async () => {
    root.render(<QueryClientProvider client={client}>
      <ThemeAttributionPanel conceptKeys={new Set(['芯片'])} onSelectConcept={onSelect} />
    </QueryClientProvider>)
  })
  for (let i = 0; i < 3; i++) await act(async () => { await new Promise(r => setTimeout(r, 0)) })
  return onSelect
}

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  calls.reasons.mockReset().mockResolvedValue(reasons)
  calls.hot.mockReset().mockResolvedValue(hot)
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
})
afterEach(() => { act(() => root.unmount()); host.remove() })

it('按题材合并两个来源的原因, 同名概念可跳转, 热搜显示', async () => {
  const onSelect = await render()
  expect(host.textContent).toContain('2 个题材')
  expect(host.textContent).toContain('1 芯片')
  expect(host.textContent).toContain('选股宝')
  expect(host.textContent).toContain('韭菜公社')
  const button = [...host.querySelectorAll('button')].find(b => b.textContent === '概念榜同名')!
  act(() => button.click())
  expect(onSelect).toHaveBeenCalledWith('芯片')
  expect([...host.querySelectorAll('button')].length).toBe(1) // 「养猪」无同名概念, 无跳转
})

it('来源失败显示不可用, 不当作无题材', async () => {
  calls.reasons.mockRejectedValue(new ApiError('未就绪', 503))
  calls.hot.mockRejectedValue(new ApiError('x', 502))
  await render()
  expect(host.textContent).toContain('来源不可用')
  expect(host.textContent).toContain('不代表当日没有题材')
})

it('单个来源失败时提示结果不完整', async () => {
  calls.reasons.mockResolvedValue({ ...reasons, unavailable_sources: ['jygs'], rows: reasons.rows.slice(0, 1) })
  await render()
  expect(host.textContent).toContain('韭菜公社不可用')
  expect(host.textContent).toContain('结果不完整')
})
