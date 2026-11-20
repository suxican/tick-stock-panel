// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { ApiError, type MeozStrongestVent } from '@/lib/api'
import { meozStrongestVentOptions } from '@/lib/useMeozWidgets'
import { StrongestVentWidget } from './StrongestVentWidget'

const calls = vi.hoisted(() => ({ get: vi.fn() }))
vi.mock('@/lib/api', async importOriginal => {
  const actual = await importOriginal<typeof import('@/lib/api')>()
  return { ...actual, api: { meozStrongestVent: calls.get } }
})

const live: MeozStrongestVent = {
  fetched_at: '2026-11-20T10:35:00+08:00',
  trade_date: null,
  rows: [
    { symbol: '000530.SZ', name: '冰山冷热', strength: 4893, pct_chg: 0.1003, main_net_amount: 967921794,
      main_buy_amount: 1598063994, main_sell_amount: -630142200, signal_at: '2026-11-20T10:06:00+08:00',
      themes: ['PCB', '芯片'] },
    { symbol: '000823.SZ', name: '超声电子', strength: 4181, pct_chg: 0.0998, main_net_amount: -630142200,
      main_buy_amount: null, main_sell_amount: null, signal_at: '2026-11-20T10:01:00+08:00', themes: [] },
  ],
}

let host: HTMLDivElement
let root: Root
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  calls.get.mockReset()
  host = document.createElement('div'); document.body.append(host); root = createRoot(host)
})
afterEach(() => { act(() => root.unmount()); host.remove() })

async function render(props: Partial<Parameters<typeof StrongestVentWidget>[0]> = {}) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const onOpenStock = vi.fn()
  const draw = async (p: typeof props) => {
    await act(async () => {
      root.render(
        <QueryClientProvider client={qc}>
          <StrongestVentWidget tradeDate="2026-11-20" isLatestDate onOpenStock={onOpenStock} {...p} />
        </QueryClientProvider>,
      )
    })
    await act(async () => { await new Promise(r => setTimeout(r, 0)) })
  }
  await draw(props)
  const refetch = async () => {
    await act(async () => { await qc.refetchQueries() })
    await act(async () => { await new Promise(r => setTimeout(r, 0)) })
  }
  return { onOpenStock, rerender: draw, refetch, qc }
}

it('renders rank, name, pct, strength, net amount and theme tags; opens stock on click', async () => {
  calls.get.mockResolvedValue(live)
  const { onOpenStock } = await render()
  expect(calls.get).toHaveBeenCalledWith(undefined)
  const text = host.textContent ?? ''
  expect(text).toContain('冰山冷热')
  expect(text).toContain('+10.03%')
  expect(text).toContain('000530.SZ')            // 代码在名称下一行 (与成交额榜一致)
  expect(text).not.toContain('强度')              // 强度收进悬停提示, 不占主界面
  expect(text).toContain('9.68亿')
  expect(text).toContain('PCB')
  expect(text).toContain('芯片')
  expect(text).toContain('10:06')
  expect(host.querySelectorAll('[data-testid="vent-themes"]').length).toBe(1)
  const tips = Array.from(host.querySelectorAll('[title]')).map(e => e.getAttribute('title') ?? '')
  expect(tips.some(t => t.includes('强度 4893') && t.includes('买入') && t.includes('卖出'))).toBe(true)
  const row = Array.from(host.querySelectorAll('button')).find(b => b.textContent?.includes('冰山冷热'))!
  await act(async () => { row.click() })
  expect(onOpenStock).toHaveBeenCalledWith('000530.SZ', '冰山冷热', [
    { symbol: '000530.SZ', name: '冰山冷热' }, { symbol: '000823.SZ', name: '超声电子' },
  ])
})

it('shows at most two theme tags per row and folds the rest into +N', async () => {
  calls.get.mockResolvedValue({
    ...live,
    rows: [{ ...live.rows[0], themes: ['PCB', '芯片', '并购重组', '水产品'] }],
  })
  await render()
  const themes = host.querySelector('[data-testid="vent-themes"]')!
  expect(themes.textContent).toContain('PCB')
  expect(themes.textContent).toContain('芯片')
  expect(themes.textContent).not.toContain('并购重组')
  expect(themes.textContent).toContain('+2')
  expect(themes.getAttribute('title')).toBe('PCB、芯片、并购重组、水产品')
})

it('shows 暂无信号 for an empty live list and 来源不可用 on failure', async () => {
  calls.get.mockResolvedValue({ ...live, rows: [] })
  await render()
  expect(host.textContent).toContain('暂无信号')

  act(() => root.unmount()); root = createRoot(host)
  calls.get.mockRejectedValue(new ApiError('未配置', 503))
  await render()
  expect(host.textContent).toContain('来源不可用')
  expect(host.textContent).not.toContain('暂无信号')
})

it('history: requests the date, titles 回看, fails to 来源不可用, empty to 该日无数据', async () => {
  calls.get.mockResolvedValue({ ...live, trade_date: '2026-11-19' })
  await render({ tradeDate: '2026-11-19', isLatestDate: false })
  expect(calls.get).toHaveBeenCalledWith('2026-11-19')
  expect(host.textContent).toContain('回看 2026-11-19')
  expect(host.textContent).toContain('冰山冷热')

  act(() => root.unmount()); root = createRoot(host)
  calls.get.mockRejectedValue(new ApiError('x', 502))
  await render({ tradeDate: '2026-11-19', isLatestDate: false })
  expect(host.textContent).toContain('来源不可用')
  expect(host.textContent).not.toContain('冰山冷热')

  act(() => root.unmount()); root = createRoot(host)
  calls.get.mockResolvedValue({ ...live, trade_date: '2026-11-19', rows: [] })
  await render({ tradeDate: '2026-11-19', isLatestDate: false })
  expect(host.textContent).toContain('该日无数据')
})

it('never shows live data after switching to a past date whose load fails', async () => {
  calls.get.mockResolvedValue(live)
  const { rerender } = await render()
  expect(host.textContent).toContain('冰山冷热')
  calls.get.mockRejectedValue(new ApiError('x', 503))
  await rerender({ tradeDate: '2026-11-19', isLatestDate: false })
  expect(host.textContent).toContain('来源不可用')
  expect(host.textContent).not.toContain('冰山冷热')
})

it('after a first failure, a refetch in flight keeps 来源不可用 instead of 加载中', async () => {
  calls.get.mockRejectedValue(new ApiError('未配置', 503))
  const { qc } = await render()
  expect(host.textContent).toContain('来源不可用')
  let release!: (e: unknown) => void
  calls.get.mockReturnValue(new Promise((_, reject) => { release = reject }))
  let pending!: Promise<unknown>
  act(() => { pending = qc.refetchQueries() })
  await act(async () => { await new Promise(r => setTimeout(r, 20)) })
  expect(host.textContent).toContain('来源不可用')
  expect(host.textContent).not.toContain('加载中')
  release(new ApiError('x', 503))
  await pending
})

it('live: keeps last good data with a stale hint when a refresh fails', async () => {
  calls.get.mockResolvedValue(live)
  const { refetch } = await render()
  expect(host.textContent).not.toContain('过期')
  calls.get.mockRejectedValue(new ApiError('x', 503))
  await refetch()
  expect(host.textContent).toContain('冰山冷热')
  expect(host.textContent).toContain('(过期)')
})

it('polls every 60s live and never on a past date', () => {
  const liveOpts = meozStrongestVentOptions('2026-11-20', true)
  const pastOpts = meozStrongestVentOptions('2026-11-19', false)
  expect(liveOpts.refetchInterval).toBe(60_000)
  expect(pastOpts.refetchInterval).toBe(false)
  expect(liveOpts.queryKey).not.toEqual(pastOpts.queryKey)
  expect(meozStrongestVentOptions('2026-11-19', false).queryKey).toEqual(pastOpts.queryKey)
})
