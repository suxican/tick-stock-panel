// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { useQuoteStream } from './useQuoteStream'
import { QK } from './queryKeys'
import { pushAlertToasts } from '@/components/AlertToast'

vi.mock('@/components/AlertToast', () => ({ pushAlertToasts: vi.fn() }))
vi.mock('@/components/Toast', () => ({ toast: vi.fn() }))

class Stream {
  static latest: Stream
  listeners = new Map<string, (event: MessageEvent) => void>()
  onopen: (() => void) | null = null
  onerror: (() => void) | null = null
  close = vi.fn()
  constructor(readonly url: string) { Stream.latest = this }
  addEventListener(name: string, callback: (event: MessageEvent) => void) { this.listeners.set(name, callback) }
  emit(name: string, payload: unknown) { this.listeners.get(name)?.({ data: JSON.stringify(payload) } as MessageEvent) }
}
let host: HTMLDivElement
let root: Root
let client: QueryClient
function Consumer() { useQuoteStream(true, {}); return null }
beforeEach(async () => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.stubGlobal('EventSource', Stream)
  vi.clearAllMocks()
  client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } })
  client.setQueryData(QK.positionTOverview('a'), { account: 'a' })
  client.setQueryData(['unrelated'], { value: 1 })
  host = document.createElement('div'); document.body.append(host); root = createRoot(host)
  await act(async () => root.render(<QueryClientProvider client={client}><Consumer /></QueryClientProvider>))
})
afterEach(async () => { await act(async () => root.unmount()); client.clear(); host.remove(); vi.unstubAllGlobals() })

it('delivers position T events through the existing stream and refreshes its snapshots', async () => {
  const event = { source: 'position_t', kind: 'second_leg_ready', type: 'position_t', account_id: 'a', plan_id: 'p', message: '第二笔条件满足' }
  expect(Stream.latest.url).toBe('/api/intraday/stream')
  await act(async () => Stream.latest.emit('strategy_alert', { alerts: [event] }))
  expect(pushAlertToasts).toHaveBeenCalledWith([event])
  expect(client.getQueryState(QK.positionTOverview('a'))?.isInvalidated).toBe(true)
  expect(client.getQueryState(['unrelated'])?.isInvalidated).toBe(false)
})

it('refreshes position T snapshots on reconnect without replaying old toasts', async () => {
  await act(async () => Stream.latest.onopen?.())
  expect(client.getQueryState(QK.positionTOverview('a'))?.isInvalidated).toBe(true)
  expect(pushAlertToasts).not.toHaveBeenCalled()
})
