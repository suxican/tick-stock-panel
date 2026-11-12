// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { BellwetherSnapshot, UserBellwetherSnapshot } from '@/lib/api'
import { BellwetherCard } from './Bellwethers'

const calls = vi.hoisted(() => ({ get: vi.fn(), groups: vi.fn() }))
vi.mock('@/lib/api', () => ({ api: { reviewBellwethers: calls.get, watchlistGroups: calls.groups } }))

const ready: BellwetherSnapshot = {
  scope: 'market', status: 'ready', reason: '', as_of: '2026-11-11', reference_date: '2026-11-10',
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

const mine: UserBellwetherSnapshot = {
  scope: 'holdings', status: 'ready', reason: '', as_of: '2026-11-11', reference_date: '2026-11-10',
  generated_at: '2026-11-11T10:30:00', quote_time: '2026-11-11T10:29:55', live: true, stale: false, total: 3,
  sectors: [{
    kind: 'concept', name: '机器人', mainline_rank: 1, member_count: 30, limit_up_prev: 4, avg_pct_prev: 0.03,
    verdict: { key: 'retreat', label: '退潮', reason: '龙头、板块广度同时走弱', signals: {} },
    items: [{ symbol: '600102.SH', name: '机器人龙', roles: [{ key: 'leader', label: '龙头', detail: '昨日 2 板' }],
      boards_prev: 2, boards_today: 0, change_pct: 0.06, state: 'broken', signal: 'weak', seal_vol: null }],
    breadth: { count: 28, avg_pct: -0.012, up_ratio: 0.3, coverage: 0.93, limit_up: 1 },
    mine: [{ symbol: '600101.SH', name: '我的股', change_pct: 0.01, state: 'flat', relative: 'stronger' }],
  }],
  uncovered: [{ symbol: '600107.SH', name: '无归属' }],
  notes: [],
}

let host: HTMLDivElement
let root: Root
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  calls.get.mockReset(); calls.groups.mockReset()
  localStorage.clear()
  host = document.createElement('div'); document.body.append(host); root = createRoot(host)
})
afterEach(() => { act(() => root.unmount()); host.remove() })

async function flush() {
  await act(async () => { await new Promise(r => setTimeout(r, 0)) })
}

async function render(onOpenStock?: (symbol: string) => void) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  await act(async () => { root.render(<QueryClientProvider client={qc}><BellwetherCard onOpenStock={onOpenStock} /></QueryClientProvider>) })
  await flush()
}

function button(text: string) {
  return Array.from(host.querySelectorAll('button')).find(b => b.textContent?.includes(text))!
}

it('converts fractional units and shows the verdict, rows and feedback', async () => {
  calls.get.mockResolvedValue(ready)
  const open = vi.fn()
  await render(open)
  expect(calls.get).toHaveBeenCalledWith('market', null)
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
  await act(async () => { button('中军股').click() })
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
  await act(async () => { button('重试').click() })
  await flush()
  expect(host.textContent).toContain('分歧')
})

it('switches to holdings and shows my sectors with relative strength', async () => {
  calls.get.mockImplementation((scope: string) => Promise.resolve(scope === 'holdings' ? mine : ready))
  const open = vi.fn()
  await render(open)
  await act(async () => { button('持仓').click() })
  await flush()
  expect(calls.get).toHaveBeenLastCalledWith('holdings', null)
  const text = host.textContent ?? ''
  expect(text).toContain('机器人主线第 1退潮')
  expect(text).toContain('均涨 -1.20% · 红盘 30% · 涨停 1')
  expect(text).toContain('机器人龙+6.00%炸板')
  expect(text).toContain('我的我的股+1.00%跑赢')
  expect(text).toContain('1/3 只未覆盖: 无归属')
  // 大盘结论不能混进持仓视图
  expect(text).not.toContain('中军走弱，情绪、龙头仍强')
  expect(localStorage.getItem('review.bellwether.scope')).toBe('holdings')
  await act(async () => { button('我的股').click() })
  expect(open).toHaveBeenCalledWith('600101.SH')
})

it('filters watchlist by group and shows empty-group reasons', async () => {
  localStorage.setItem('review.bellwether.scope', 'watchlist')
  calls.groups.mockResolvedValue({ groups: [{ id: 'g1', name: '核心', color: 'sky' }] })
  calls.get.mockImplementation((scope: string, group?: string | null) => Promise.resolve(group
    ? { ...mine, scope, status: 'unavailable', reason: '该自选分组没有股票', sectors: [], uncovered: [] }
    : { ...mine, scope }))
  await render()
  expect(calls.get).toHaveBeenCalledWith('watchlist', null)
  const select = host.querySelector('select')!
  await act(async () => {
    select.value = 'g1'
    select.dispatchEvent(new Event('change', { bubbles: true }))
  })
  await flush()
  expect(calls.get).toHaveBeenLastCalledWith('watchlist', 'g1')
  expect(host.textContent).toContain('该自选分组没有股票')
})
