// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { BellwetherSnapshot } from '@/lib/api'
import { BellwetherCard } from './Bellwethers'

const calls = vi.hoisted(() => ({ get: vi.fn() }))
vi.mock('@/lib/api', () => ({ api: { reviewBellwethers: calls.get } }))

const ready: BellwetherSnapshot = {
  status: 'ready', reason: '', as_of: '2026-11-11', reference_date: '2026-11-10',
  generated_at: '2026-11-11T10:30:00', quote_time: '2026-11-11T10:29:55', live: true, stale: false,
  verdict: { key: 'divergence', label: '分歧', reason: '中军走弱，情绪、龙头仍强', signals: {} },
  items: [
    { symbol: '600001.SH', name: '龙头', roles: [{ key: 'emotion', label: '情绪', detail: '昨日 5 连板' }],
      boards_prev: 5, boards_today: 6, change_pct: 0.1, state: 'sealed', signal: 'strong', seal_vol: 12345 },
    { symbol: '600003.SH', name: '中军股', roles: [{ key: 'core', label: '中军', detail: '机器人 中军' }],
      boards_prev: 0, boards_today: 0, change_pct: -0.04, state: 'weak', signal: 'weak', seal_vol: null },
  ],
  feedback: { limit_up: { count: 40, avg_pct: 0.0215, up_ratio: 0.64 }, broken: null },
  ladder: { limit_up: 50, broken: 12, limit_down: 1, max_consecutive: 6, seal_rate: 0.806, promo_rate: 0.25, sealed_corrected: false },
  mainlines: [{ kind: 'concept', name: '机器人', rank: 1 }],
  notes: ['概念成分为当前快照'],
}

let host: HTMLDivElement
let root: Root
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  calls.get.mockReset()
  host = document.createElement('div'); document.body.append(host); root = createRoot(host)
})
afterEach(() => { act(() => root.unmount()); host.remove() })

async function render(onOpenStock?: (symbol: string) => void) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  await act(async () => { root.render(<QueryClientProvider client={qc}><BellwetherCard onOpenStock={onOpenStock} /></QueryClientProvider>) })
  await act(async () => { await new Promise(r => setTimeout(r, 0)) })
}

it('converts fractional units and shows the verdict, rows and feedback', async () => {
  calls.get.mockResolvedValue(ready)
  const open = vi.fn()
  await render(open)
  const text = host.textContent ?? ''
  expect(text).toContain('分歧')
  expect(text).toContain('中军走弱，情绪、龙头仍强')
  expect(text).toContain('行情 10:29')
  expect(text).toContain('+10.00%')
  expect(text).toContain('-4.00%')
  expect(text).toContain('昨 5 板 → 今 6 板 · 封单 12,345 手')
  expect(text).toContain('昨日涨停今日 +2.15% · 红盘 64% (40 只)')
  expect(text).toContain('封板率 81%')
  expect(text).not.toContain('昨日炸板今日')
  const row = Array.from(host.querySelectorAll('button')).find(b => b.textContent?.includes('中军股'))!
  await act(async () => { row.click() })
  expect(open).toHaveBeenCalledWith('600003.SH')
})

it('shows the reason when data is unavailable and the empty list state', async () => {
  calls.get.mockResolvedValue({ ...ready, status: 'unavailable', reason: '缺少上一交易日(2026-11-10)的收盘数据', items: [] })
  await render()
  expect(host.textContent).toContain('缺少上一交易日(2026-11-10)的收盘数据')
  expect(host.querySelector('details')).toBeNull()

  act(() => root.unmount()); root = createRoot(host)
  calls.get.mockResolvedValue({ ...ready, items: [], live: false, quote_time: null })
  await render()
  expect(host.textContent).toContain('上一交易日没有 2 板以上的连板股')
  expect(host.textContent).toContain('11-11 非交易时段')
})

it('offers a retry when the request fails', async () => {
  calls.get.mockRejectedValue(new Error('boom'))
  await render()
  expect(host.textContent).toContain('加载失败')
  calls.get.mockResolvedValue(ready)
  const retry = Array.from(host.querySelectorAll('button')).find(b => b.textContent?.includes('重试'))!
  await act(async () => { retry.click() })
  await act(async () => { await new Promise(r => setTimeout(r, 0)) })
  expect(host.textContent).toContain('分歧')
})
