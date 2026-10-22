// @vitest-environment jsdom
import { QueryClient } from '@tanstack/react-query'
import { afterEach, expect, it, vi } from 'vitest'
import { api, ApiError } from './api'
import { QK } from './queryKeys'
import { shouldPersistQuery } from './queryPersist'

const toast = vi.hoisted(() => vi.fn())
vi.mock('@/components/Toast', () => ({ toast }))
afterEach(() => { vi.unstubAllGlobals(); toast.mockClear() })

it('uses the dated dashboard endpoint and can explicitly discover the latest source session', async () => {
  const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ date: '2026-10-21', state: 'partial' })))
  vi.stubGlobal('fetch', fetch)
  expect(await api.kaipanlaMarketEmotion('2026-10-21', true)).toMatchObject({ date: '2026-10-21', state: 'partial' })
  expect(fetch.mock.calls[0][0]).toBe('/api/market-recap/kaipanla/market-emotion?date=2026-10-21&force=true')
  fetch.mockResolvedValue(new Response(JSON.stringify({ date: '2026-10-20', state: 'ok' })))
  expect(await api.kaipanlaMarketEmotion()).toMatchObject({ date: '2026-10-20' })
  expect(fetch.mock.calls[1][0]).toBe('/api/market-recap/kaipanla/market-emotion')
})

it('sends the selected stock-list date and first-board broken-list parameters through the shared client', async () => {
  const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ state: 'empty', rows: [] })))
  vi.stubGlobal('fetch', fetch)
  await expect(api.kaipanlaQuery('ext_kpl_broken_limits', '2026-10-20', { PidType: 1 }, true))
    .resolves.toMatchObject({ state: 'empty', rows: [] })
  expect(fetch.mock.calls[0][0]).toBe('/api/market-recap/kaipanla/query/ext_kpl_broken_limits')
  expect(fetch.mock.calls[0][1].method).toBe('POST')
  expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({ date: '2026-10-20', parameters: { PidType: 1 }, force: true })
})

it('passes API failures to the page without duplicate error toasts', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: '数据暂不可用' }), { status: 502 })))
  await expect(api.kaipanlaMarketEmotion('2026-10-21')).rejects.toBeInstanceOf(ApiError)
  expect(toast).not.toHaveBeenCalled()
})

it('isolates dates and minute providers while retaining existing prefix invalidation', async () => {
  const client = new QueryClient()
  client.setQueryData(QK.kaipanlaMarketEmotion(), { date: '2026-10-20' })
  expect(client.getQueryData(QK.kaipanlaMarketEmotion('2026-10-21'))).toBeUndefined()
  const previousProvider = QK.indexMinute('000001.SH', '2026-10-21', 'previous')
  const currentProvider = QK.indexMinute('000001.SH', '2026-10-21', 'current')
  client.setQueryData(previousProvider, { rows: [1] })
  expect(client.getQueryData(currentProvider)).toBeUndefined()
  client.setQueryData(currentProvider, { rows: [2] })
  await client.invalidateQueries({ queryKey: QK.indexMinute('000001.SH', '2026-10-21') })
  expect(client.getQueryState(previousProvider)?.isInvalidated).toBe(true)
  expect(client.getQueryState(currentProvider)?.isInvalidated).toBe(true)
  expect(shouldPersistQuery({ queryKey: QK.kaipanlaMarketEmotion(), state: { status: 'success' } })).toBe(false)
  expect(shouldPersistQuery({ queryKey: QK.kaipanlaStocks('limit_up', '2026-10-21'), state: { status: 'success' } })).toBe(false)
  client.clear()
})
