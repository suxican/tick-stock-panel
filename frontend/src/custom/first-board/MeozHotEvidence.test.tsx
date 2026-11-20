// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { ApiError } from '@/lib/api'
import { MeozHotEvidence } from './MeozHotEvidence'

const calls = vi.hoisted(() => ({ hot: vi.fn() }))
vi.mock('@/lib/api', async original => ({
  ...await original<typeof import('@/lib/api')>(),
  api: { meozHotRank: calls.hot },
}))

let host: HTMLDivElement
let root: Root
const hot = {
  fetched_at: '2026-11-20T10:00:03+08:00',
  stocks: {
    ths_normal: { available: true, rows: [] },
    dfcf_renqi: { available: true, rows: [{ symbol: '002242.SZ', rank: 7, name: '九阳股份', pct_chg: 0.01 }] },
    ths_trend: { available: false, rows: [] },
  },
  themes: { available: true, rows: [{ rank: 2, theme_id: '801807', name: '算力', available_at: '2026-11-20T09:58:00+08:00' }] },
}

async function render(boardName: string | null = '算力') {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  await act(async () => {
    root.render(<QueryClientProvider client={client}><MeozHotEvidence symbol="002242.SZ" boardName={boardName} /></QueryClientProvider>)
  })
  for (let i = 0; i < 3; i++) await act(async () => { await new Promise(r => setTimeout(r, 0)) })
}

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  calls.hot.mockReset().mockResolvedValue(hot)
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
})
afterEach(() => { act(() => root.unmount()); host.remove() })

it('区分已上榜、未上榜与来源不可用, 并给出热搜与获取时间', async () => {
  await render()
  const text = host.textContent!
  expect(text).toContain('猫爪东财人气 · 第 7 名')
  expect(text).toContain('猫爪同花顺人气 · 未上榜')
  expect(text).toContain('猫爪同花顺飙升 · 来源不可用')
  expect(text).toContain('算力 · 猫爪热搜第 2 名')
  expect(text).toContain('热榜获取 10:00:03')
})

it('板块名不同名时不显示热搜行', async () => {
  await render('通信设备')
  expect(host.textContent).not.toContain('热搜第')
})

it('整体来源不可用', async () => {
  calls.hot.mockRejectedValue(new ApiError('x', 503))
  await render()
  expect(host.textContent).toContain('来源不可用')
  expect(host.textContent).not.toContain('未上榜')
})
