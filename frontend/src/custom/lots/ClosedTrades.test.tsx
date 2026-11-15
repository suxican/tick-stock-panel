// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { TradeJournalEntry } from '@/lib/api'
import { ClosedTrades } from './ClosedTrades'

const mocks = vi.hoisted(() => ({ list: vi.fn(), remove: vi.fn() }))
vi.mock('@/lib/api', async importOriginal => ({
  ...(await importOriginal<typeof import('@/lib/api')>()),
  api: {
    tradeJournalList: (...args: unknown[]) => mocks.list(...args),
    tradeJournalDelete: (...args: unknown[]) => mocks.remove(...args),
  },
}))

const entry = (over: Partial<TradeJournalEntry>): TradeJournalEntry => ({
  id: 'tj_lot_a1', lot_id: 'lot_a1', symbol: '600519.SH', name: '贵州茅台', qty: 100, cost_price: 1680,
  buy_date: '2026-11-05', sell_price: 1722, sell_date: '2026-11-13', reason: 'planned_profit', note: '',
  had_invalidation: true, buy_phase: 'rally', hold_days: 6, pnl_pct: 2.5, created_at: '2026-11-13T07:30:00+00:00',
  ...over,
})

let host: HTMLDivElement
let root: Root
let client: QueryClient
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  mocks.list.mockReset()
  mocks.remove.mockReset().mockResolvedValue({ ok: true })
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
})
afterEach(async () => {
  await act(async () => root.unmount())
  client.clear()
  host.remove()
})

async function settle() {
  for (let i = 0; i < 3; i++) await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
}
async function mount() {
  await act(async () => { root.render(<QueryClientProvider client={client}><ClosedTrades /></QueryClientProvider>) })
  await settle()
}
async function click(match: (b: HTMLButtonElement) => boolean) {
  const button = [...host.querySelectorAll('button')].find(match)
  expect(button).toBeDefined()
  await act(async () => button!.click())
  await settle()
}

it('renders nothing when there are no closed trades', async () => {
  mocks.list.mockResolvedValue({ entries: [], stats: null })
  await mount()
  expect(host.textContent).toBe('')
})

it('is collapsed by default and lists pnl, holding days and reason when expanded', async () => {
  mocks.list.mockResolvedValue({
    entries: [entry({}), entry({ id: 'tj_lot_b2', name: '平安银行', symbol: '000001.SZ', pnl_pct: -5, hold_days: null, reason: 'late_stop' })],
    stats: null,
  })
  await mount()
  expect(host.textContent).toContain('已结清 2 笔')
  expect(host.textContent).not.toContain('贵州茅台')

  await click(b => b.textContent?.includes('已结清 2 笔') ?? false)
  const text = host.textContent ?? ''
  for (const expected of ['贵州茅台', '+2.50%', '6 天', '按计划止盈', '平安银行', '-5.00%', '拖延未止损', '不含费用']) {
    expect(text).toContain(expected)
  }
  const rows = [...host.querySelectorAll('tbody tr')]
  expect(rows[1].textContent).toContain('—')
})

it('deletes a record only after the second click', async () => {
  mocks.list.mockResolvedValue({ entries: [entry({})], stats: null })
  await mount()
  await click(b => b.textContent?.includes('已结清 1 笔') ?? false)
  await click(b => b.title === '删除这条记录 (不恢复批次)')
  expect(mocks.remove).not.toHaveBeenCalled()
  await click(b => b.title === '再次点击确认删除')
  expect(mocks.remove).toHaveBeenCalledTimes(1)
  expect(mocks.remove.mock.calls[0][0]).toBe('tj_lot_a1')
})

it('shows a retry hint when loading fails', async () => {
  mocks.list.mockRejectedValue(new Error('boom'))
  await mount()
  expect(host.textContent).toContain('已结清记录加载失败')
  mocks.list.mockResolvedValue({ entries: [entry({})], stats: null })
  await click(b => b.textContent === '重试')
  expect(host.textContent).toContain('已结清 1 笔')
})
