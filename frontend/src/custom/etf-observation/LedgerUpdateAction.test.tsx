// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { QK } from '@/lib/queryKeys'
import { LedgerUpdateAction } from './LedgerUpdateAction'

const calls = vi.hoisted(() => ({ etfLedgerAppend: vi.fn() }))
vi.mock('@/lib/api', async importOriginal => ({
  ...await importOriginal<typeof import('@/lib/api')>(), api: calls,
}))

let host: HTMLDivElement
let root: Root
let client: QueryClient
const refresh = vi.fn()

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.resetAllMocks()
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
})
afterEach(async () => {
  await act(async () => root.unmount())
  client.clear()
  host.remove()
})

async function render(tableId = 'etf_broad_flow_daily') {
  await act(async () => root.render(
    <QueryClientProvider client={client}>
      <LedgerUpdateAction tableId={tableId} refresh={refresh} />
    </QueryClientProvider>,
  ))
}
async function update() {
  const button = [...host.querySelectorAll('button')].find(b => b.textContent?.includes('更新份额台账'))
  expect(button).toBeDefined()
  await act(async () => { button!.click(); await new Promise(resolve => setTimeout(resolve, 20)) })
}

it('only appears on the broad ETF ledger card', async () => {
  await render('zzshare_limit_reasons')
  expect(host.innerHTML).toBe('')
})

it('reports appended sessions and refreshes the data page and ETF page', async () => {
  calls.etfLedgerAppend.mockResolvedValue({ added: ['2026-11-16'], latest: '2026-11-16', pending: ['2026-11-17'] })
  const invalidate = vi.spyOn(client, 'invalidateQueries')
  await render()
  await update()
  expect(host.querySelector('[role="status"]')?.textContent).toBe('已追加 11-16；11-17 待交易所份额与监管净值发布')
  expect(refresh).toHaveBeenCalledOnce()
  expect(invalidate).toHaveBeenCalledWith({ queryKey: QK.etfObservationAll })
})

it('names the sessions still waiting when nothing new can be published', async () => {
  calls.etfLedgerAppend.mockResolvedValue({ added: [], latest: '2026-11-13', pending: ['2026-11-16', '2026-11-17'] })
  await render()
  await update()
  expect(host.querySelector('[role="status"]')?.textContent).toBe('暂无可追加：11-16、11-17 待交易所份额与监管净值发布')
  expect(refresh).not.toHaveBeenCalled()
})

it('says the ledger is current when no session is pending', async () => {
  calls.etfLedgerAppend.mockResolvedValue({ added: [], latest: '2026-11-17', pending: [] })
  await render()
  await update()
  expect(host.querySelector('[role="status"]')?.textContent).toBe('已是最新（2026-11-17）')
})

it('shows the source failure from the backend', async () => {
  calls.etfLedgerAppend.mockRejectedValue(new Error('数据源 eid.csrc.gov.cn 暂时拒绝访问（HTTP 405），台账未改动，请稍后重试'))
  await render()
  await update()
  expect(host.querySelector('[role="alert"]')?.textContent).toContain('eid.csrc.gov.cn')
  expect(host.querySelector('button')?.disabled).toBe(false)
})
