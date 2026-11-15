// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { Lot } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { CloseLotDialog } from './CloseLotDialog'

const mocks = vi.hoisted(() => ({ close: vi.fn() }))
vi.mock('@/lib/api', async importOriginal => ({
  ...(await importOriginal<typeof import('@/lib/api')>()),
  api: { lotClose: (...args: unknown[]) => mocks.close(...args) },
}))
vi.mock('@/lib/cnTime', async importOriginal => ({
  ...(await importOriginal<typeof import('@/lib/cnTime')>()),
  cnToday: () => '2026-11-13',
}))

const lot: Lot = { id: 'lot_a1', symbol: '600519.SH', qty: 100, cost_price: 1680, buy_date: '2026-11-05', target_pct: 10, stop_pct: 5, lead_days: 1 }

let host: HTMLDivElement
let root: Root
let client: QueryClient
let onClose: ReturnType<typeof vi.fn>
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  mocks.close.mockReset()
  onClose = vi.fn()
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
  await act(async () => {
    root.render(<QueryClientProvider client={client}><CloseLotDialog lot={lot} name="贵州茅台" onClose={onClose} /></QueryClientProvider>)
  })
  await settle()
}
function submitButton() {
  return [...document.body.querySelectorAll('button')].find(b => b.textContent?.includes('结清并记录')) as HTMLButtonElement
}
async function typeInto(label: string, value: string) {
  const input = document.body.querySelector(`[aria-label="${label}"]`) as HTMLInputElement | HTMLTextAreaElement
  expect(input).not.toBeNull()
  const proto = input instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype
  await act(async () => {
    Object.getOwnPropertyDescriptor(proto, 'value')!.set!.call(input, value)
    input.dispatchEvent(new Event('input', { bubbles: true }))
  })
}
async function choose(label: string) {
  const radio = [...document.body.querySelectorAll('label')].find(l => l.textContent === label)!.querySelector('input')!
  await act(async () => radio.click())
}

it('shows the lot, stays disabled until price and reason are set, then closes with the entered values', async () => {
  const invalidate = vi.spyOn(client, 'invalidateQueries')
  mocks.close.mockResolvedValue({ ok: true, entry: {} })
  await mount()
  expect(document.body.textContent).toContain('贵州茅台')
  expect(document.body.textContent).toContain('成本 1680.00')
  expect(document.body.textContent).toContain('100 股')
  expect(submitButton().disabled).toBe(true)

  await typeInto('卖出价', '1722.5')
  expect(submitButton().disabled).toBe(true)
  await choose('按计划止盈')
  expect(submitButton().disabled).toBe(false)

  await act(async () => submitButton().click())
  await settle()
  expect(mocks.close).toHaveBeenCalledTimes(1)
  expect(mocks.close).toHaveBeenCalledWith('lot_a1', { sell_price: 1722.5, sell_date: '2026-11-13', reason: 'planned_profit', note: '' })
  expect(onClose).toHaveBeenCalledTimes(1)
  const keys = invalidate.mock.calls.map(c => JSON.stringify((c[0] as { queryKey: unknown }).queryKey))
  for (const key of [QK.lots, QK.monitorRules, QK.holdingAnalysisAll, QK.tradeJournal, ['review-bellwethers']]) {
    expect(keys).toContain(JSON.stringify(key))
  }
})

it('rejects a non-positive price', async () => {
  await mount()
  await choose('计划外交易')
  await typeInto('卖出价', '0')
  expect(submitButton().disabled).toBe(true)
  await typeInto('卖出价', 'abc')
  expect(submitButton().disabled).toBe(true)
})

it('keeps the dialog open and shows the server reason when closing fails', async () => {
  mocks.close.mockRejectedValue(new Error('卖出日不能晚于今天'))
  await mount()
  await typeInto('卖出价', '1700')
  await typeInto('备注', '手滑')
  await choose('提前卖出')
  await act(async () => submitButton().click())
  await settle()
  expect(mocks.close).toHaveBeenCalledWith('lot_a1', { sell_price: 1700, sell_date: '2026-11-13', reason: 'early_exit', note: '手滑' })
  expect(document.body.textContent).toContain('卖出日不能晚于今天')
  expect(onClose).not.toHaveBeenCalled()
  expect(submitButton().disabled).toBe(false)
})
