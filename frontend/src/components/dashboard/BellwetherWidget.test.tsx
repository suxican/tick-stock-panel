// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { BellwetherScope, BellwetherSnapshot, UserBellwetherSnapshot } from '@/lib/api'
import { BellwetherWidget } from './BellwetherWidget'

const calls = vi.hoisted(() => ({ get: vi.fn() }))
vi.mock('@/lib/api', () => ({ api: { reviewBellwethers: calls.get } }))

const market: BellwetherSnapshot = {
  scope: 'market', status: 'ready', reason: '', as_of: '2026-11-13', reference_date: '2026-11-12',
  generated_at: '2026-11-13T10:30:00', quote_time: '2026-11-13T10:29:55', live: true, stale: false,
  verdict: { key: 'divergence', label: '分歧', reason: '中军走弱，情绪、龙头仍强', signals: {} },
  verdict_change: { t: '10:35:00', from: 'attack', to: 'divergence' },
  items: [
    { symbol: '600001.SH', name: '大东方', roles: [{ key: 'emotion', label: '情绪', detail: '昨日 4 连板' }],
      boards_prev: 4, boards_today: 5, change_pct: 0.0995, state: 'sealed', signal: 'strong', seal_vol: 100,
      timeline: { first_sealed_at: '09:42:00', broken_count: 1, resealed_count: 1, trend: null, events: [] } },
    { symbol: '600003.SH', name: '芒果超媒', roles: [{ key: 'core', label: '中军', detail: 'IP 中军' }],
      boards_prev: 0, boards_today: 0, change_pct: -0.0459, state: 'weak', signal: 'weak', seal_vol: null },
  ],
  newcomers: [{ symbol: '603706.SH', name: '东方环宇', boards_today: 4, change_pct: 0.0999 }],
  feedback: { limit_up: { count: 85, avg_pct: 0.019, up_ratio: 0.59 }, broken: null },
  ladder: { limit_up: 44, broken: 24, limit_down: 0, max_consecutive: 5, seal_rate: 0.64, promo_rate: 0.21, sealed_corrected: false },
  mainlines: [], notes: [],
}

const holdings: UserBellwetherSnapshot = {
  scope: 'holdings', status: 'ready', reason: '', as_of: '2026-11-13', reference_date: '2026-11-12',
  generated_at: '2026-11-13T10:30:00', quote_time: '2026-11-13T10:29:55', live: true, stale: false, total: 3,
  sectors: [{
    kind: 'concept', name: '共封装光学(CPO)', mainline_rank: 2, member_count: 230, limit_up_prev: 3, avg_pct_prev: 0.01,
    verdict: { key: 'divergence', label: '分歧', reason: '中军走弱', signals: {} },
    items: [{ symbol: '002384.SZ', name: '东山精密', roles: [{ key: 'leader', label: '龙头', detail: '昨日 1 板' }],
      boards_prev: 1, boards_today: 0, change_pct: 0.0505, state: 'strong', signal: 'strong', seal_vol: null }],
    breadth: { count: 18, avg_pct: 0.0096, up_ratio: 0.56, coverage: 1, limit_up: 3, basis: 'active', size: 18 },
    mine: [{ symbol: '600498.SH', name: '烽火通信', change_pct: -0.0109, state: 'flat', relative: 'weaker' }],
  }],
  uncovered: [{ symbol: '688001.SH', name: '博迁新材' }],
  notes: [],
}

let host: HTMLDivElement
let root: Root
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  calls.get.mockReset()
  host = document.createElement('div'); document.body.append(host); root = createRoot(host)
})
afterEach(() => { act(() => root.unmount()); host.remove() })

async function render(props: Partial<Parameters<typeof BellwetherWidget>[0]> = {}) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const onScopeChange = vi.fn()
  const onOpenStock = vi.fn()
  await act(async () => {
    root.render(
      <QueryClientProvider client={qc}>
        <MemoryRouter>
          <BellwetherWidget isLatestDate onScopeChange={onScopeChange} onOpenStock={onOpenStock} {...props} />
        </MemoryRouter>
      </QueryClientProvider>,
    )
  })
  await act(async () => { await new Promise(r => setTimeout(r, 0)) })
  return { onScopeChange, onOpenStock }
}

function button(text: string) {
  return Array.from(host.querySelectorAll('button')).find(b => b.textContent?.includes(text))!
}

it('shows the compact market view and opens stocks with the visible list', async () => {
  calls.get.mockResolvedValue(market)
  const { onOpenStock } = await render()
  expect(calls.get).toHaveBeenCalledWith('market', null)
  const text = host.textContent ?? ''
  expect(text).toContain('分歧中军走弱，情绪、龙头仍强10:35 进攻→分歧')
  expect(text).toContain('大东方+9.95%封板首封 09:42 · 炸 1')
  expect(text).toContain('新晋东方环宇4板')
  expect(text).toContain('昨涨停今日 +1.90% 红盘59% · 封板率 64% · 晋级率 21%')
  // 看板上不放封单与口径说明, 留给复盘页
  expect(text).not.toContain('封单')
  expect(host.querySelector('details')).toBeNull()
  expect(host.querySelector('a[href="/review"]')).not.toBeNull()
  await act(async () => { button('芒果超媒').click() })
  expect(onOpenStock).toHaveBeenCalledWith('600003.SH', '芒果超媒', [
    { symbol: '600001.SH', name: '大东方' }, { symbol: '600003.SH', name: '芒果超媒' },
    { symbol: '603706.SH', name: '东方环宇' },
  ])
})

it('uses the saved scope and reports scope changes to the layout', async () => {
  calls.get.mockImplementation((scope: BellwetherScope) => Promise.resolve(scope === 'holdings' ? holdings : market))
  const { onScopeChange } = await render({ scope: 'holdings' })
  expect(calls.get).toHaveBeenCalledWith('holdings', null)
  const text = host.textContent ?? ''
  expect(text).toContain('共封装光学(CPO)主线2分歧')
  expect(text).toContain('活跃 +0.96% 红56%')
  expect(text).toContain('我的烽火通信-1.09%跑输')
  expect(text).toContain('2 只覆盖 1 个板块 · 1 只未覆盖')
  await act(async () => { button('大盘').click() })
  expect(onScopeChange).toHaveBeenCalledWith('market')
})

it('falls back to market for an unknown saved scope', async () => {
  calls.get.mockResolvedValue(market)
  await render({ scope: 'bogus' })
  expect(calls.get).toHaveBeenCalledWith('market', null)
})

it('does not fetch while the dashboard shows a past date', async () => {
  await render({ isLatestDate: false })
  expect(calls.get).not.toHaveBeenCalled()
  expect(host.textContent).toContain('风向标仅提供当日数据')
})

it('shows unavailable reasons and a retry on failure', async () => {
  calls.get.mockResolvedValue({ ...holdings, status: 'unavailable', reason: '还没有批次提醒持仓', sectors: [], uncovered: [] })
  await render({ scope: 'holdings' })
  expect(host.textContent).toContain('还没有批次提醒持仓')

  act(() => root.unmount()); root = createRoot(host)
  calls.get.mockRejectedValue(new Error('boom'))
  await render()
  expect(host.textContent).toContain('加载失败')
  calls.get.mockResolvedValue(market)
  await act(async () => { button('重试').click() })
  await act(async () => { await new Promise(r => setTimeout(r, 0)) })
  expect(host.textContent).toContain('大东方')
})
