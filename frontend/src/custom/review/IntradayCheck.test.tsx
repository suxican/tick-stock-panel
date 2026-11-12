// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { RecapIntradayCheck } from '@/lib/api'
import { IntradayCheckStrip } from './IntradayCheck'

const calls = vi.hoisted(() => ({ get: vi.fn(), run: vi.fn() }))
vi.mock('@/lib/api', () => ({ api: { reviewIntradayCheck: calls.get, reviewIntradayCheckRun: calls.run } }))

const ok: RecapIntradayCheck = {
  date: '2026-11-12', generated_at: '2026-11-12T10:02:05', status: 'ok', from_as_of: '2026-11-11',
  amount_30m: 5341.2, prev_amount_30m: 5000,
  leaning: [{ name: '符合预期', primary: true, action: '只做分歧转一致' }],
}

let host: HTMLDivElement
let root: Root
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  calls.get.mockReset(); calls.run.mockReset()
  host = document.createElement('div'); document.body.append(host); root = createRoot(host)
})
afterEach(() => { act(() => root.unmount()); host.remove() })

async function render() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  await act(async () => { root.render(<QueryClientProvider client={qc}><IntradayCheckStrip /></QueryClientProvider>) })
  await act(async () => { await new Promise(r => setTimeout(r, 0)) })
}

it('shows the leaning scenario with its action and turnover change', async () => {
  calls.get.mockResolvedValue({ result: ok })
  await render()
  const text = host.textContent ?? ''
  expect(text).toContain('10:02 · 开盘半小时成交额 5,341亿(较上一交易日 +7%)')
  expect(text).toContain('倾向「符合预期·主判断」: 只做分歧转一致')
  expect(text).toContain('来自 11-11 剧本, 全天条件待收盘')
})

it('shows not-yet-checked state and runs a manual check', async () => {
  calls.get.mockResolvedValue({ result: null })
  calls.run.mockResolvedValue({ result: { ...ok, status: 'data_incomplete', amount_30m: null, leaning: [], message: '开盘分钟数据不完整' } })
  await render()
  expect(host.textContent).toContain('今日尚未核验(10:00 后可用)')
  const btn = Array.from(host.querySelectorAll('button')).find(b => b.textContent?.includes('立即核验'))!
  await act(async () => { btn.click() })
  await act(async () => { await new Promise(r => setTimeout(r, 0)) })
  expect(calls.run).toHaveBeenCalledTimes(1)
  expect(host.textContent).toContain('开盘分钟数据不完整')
})
