// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { AbnormalRegulatory } from '@/lib/api'
import { RegulatoryView } from './RegulatoryView'

const fixtures = vi.hoisted(() => ({ payload: null as unknown }))
vi.mock('@/lib/api', () => ({ api: { abnormalRegulatory: vi.fn(async () => fixtures.payload) } }))

const PAYLOAD: AbnormalRegulatory = {
  target_day: '2026-11-23', anomaly_as_of: '2026-11-20', monitor_as_of: '2026-11-20',
  anomaly: [{ symbol: '688033.SH', code: '688033', name: '天宜新材', rule: '10日偏离值-50%', trigger_price: 2.79,
    required_pct: -0.1234, status: 'reachable', elapsed_days: 8, deviation_pct: -0.4512 }],
  monitor: [{ symbol: '605255.SH', code: '605255', name: '天普股份', label: '交易所风险提示', start: '2026-11-19',
    end: '2026-12-02', reason: '股价累计涨幅较大' }],
}

let host: HTMLDivElement
let root: Root
let client: QueryClient
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
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

async function render(payload: AbnormalRegulatory, onOpen = vi.fn()) {
  fixtures.payload = payload
  await act(async () => root.render(<QueryClientProvider client={client}><RegulatoryView onOpenStock={onOpen} /></QueryClientProvider>))
  await act(async () => { await new Promise(r => setTimeout(r, 30)) })
  return onOpen
}

it('lists serious-abnormal forecasts and monitored stocks for the target session', async () => {
  const onOpen = await render(PAYLOAD)
  expect(host.textContent).toContain('适用于 2026-11-23')
  expect(host.textContent).toContain('10日偏离值-50%')
  expect(host.textContent).toContain('2.79')
  expect(host.textContent).toContain('-12.34%')
  expect(host.textContent).toContain('涨跌停内可触及')
  expect(host.textContent).toContain('2026-11-19 至 2026-12-02')
  expect(host.textContent).toContain('股价累计涨幅较大')
  const name = [...host.querySelectorAll('button')].find(b => b.textContent?.includes('天宜新材'))!
  await act(async () => name.click())
  expect(onOpen).toHaveBeenCalledWith('688033.SH', '天宜新材', expect.any(Array))
})

it('collapses a long monitoring reason behind an expand control', async () => {
  const reason = '公司股价累计涨幅较大'.repeat(12)
  await render({ ...PAYLOAD, monitor: [{ ...PAYLOAD.monitor![0], reason }] })
  const details = host.querySelector('details')!
  expect(details.querySelector('summary')!.textContent).toContain('展开')
  expect(details.querySelector('summary')!.textContent!.length).toBeLessThan(reason.length)
  expect(details.textContent).toContain(reason)
})

it('distinguishes an empty list from an unavailable archive', async () => {
  await render({ ...PAYLOAD, anomaly: [], monitor: null })
  expect(host.textContent).toContain('当日无严重异动预测')
  expect(host.textContent).toContain('监管名单留档不可用')
})
