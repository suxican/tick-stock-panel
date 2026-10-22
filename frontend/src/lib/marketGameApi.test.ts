// @vitest-environment jsdom
import { QueryClient } from '@tanstack/react-query'
import { afterEach, expect, it, vi } from 'vitest'
import { api, ApiError } from './api'
import { QK, SSE_INVALIDATE_PREFIXES } from './queryKeys'
import { shouldPersistQuery } from './queryPersist'

const toast = vi.hoisted(() => vi.fn())
vi.mock('@/components/Toast', () => ({ toast }))
afterEach(() => { vi.unstubAllGlobals(); toast.mockClear() })

it('generates a dated frozen plan through the shared client with decimal risk units', async () => {
  const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ id: 'plan-one' })))
  vi.stubGlobal('fetch', fetch)
  const risk = { total_cap: .5, single_cap: .15, sector_cap: .25, risk_per_trade: .005, min_amount: 1e8, max_candidates: 5 }
  await expect(api.marketGameAnalyze({ as_of: '2026-10-20', risk })).resolves.toEqual({ id: 'plan-one' })
  expect(fetch.mock.calls[0][0]).toBe('/api/market-game/analyze')
  expect(fetch.mock.calls[0][1].method).toBe('POST')
  expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({ as_of: '2026-10-20', risk })
})

it('keeps reads separate from explicit observation, model and explanation writes', async () => {
  const fetch = vi.fn().mockImplementation(async () => new Response('{}'))
  vi.stubGlobal('fetch', fetch)
  await api.marketGameReports()
  await api.marketGameReport('plan/one')
  await api.marketGameEvaluation('plan/one')
  await api.marketGameCapital('plan/one')
  await api.marketGameModel('plan/one')
  await api.marketGameRefreshCapital('plan/one')
  await api.marketGameEvaluateModel('plan/one')
  await api.marketGameExplain('plan/one')
  expect(fetch.mock.calls.map(([url, options]) => [url, options.method ?? 'GET'])).toEqual([
    ['/api/market-game/reports', 'GET'],
    ['/api/market-game/reports/plan%2Fone', 'GET'],
    ['/api/market-game/reports/plan%2Fone/evaluation', 'GET'],
    ['/api/market-game/reports/plan%2Fone/capital', 'GET'],
    ['/api/market-game/reports/plan%2Fone/model', 'GET'],
    ['/api/market-game/reports/plan%2Fone/capital', 'POST'],
    ['/api/market-game/reports/plan%2Fone/evaluate-model', 'POST'],
    ['/api/market-game/reports/plan%2Fone/explain', 'POST'],
  ])
})

it('passes unavailable-source failures to the panel without a duplicate toast', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: '分钟数据不可用' }), { status: 503 })))
  await expect(api.marketGameRefreshCapital('plan-one')).rejects.toEqual(new ApiError('分钟数据不可用', 503))
  expect(toast).not.toHaveBeenCalled()
})

it('isolates every report context and keeps frozen decisions outside live SSE and persisted quote caches', async () => {
  const client = new QueryClient()
  for (const key of [QK.marketGameReport, QK.marketGameExplanation, QK.marketGameEvaluation, QK.marketGameCapital, QK.marketGameModel]) {
    client.setQueryData(key('one'), { report_id: 'one' })
    expect(client.getQueryData(key('two'))).toBeUndefined()
    expect(shouldPersistQuery({ queryKey: key('one'), state: { status: 'success' } })).toBe(false)
    expect(SSE_INVALIDATE_PREFIXES).not.toContain(key('one')[0])
  }
  await client.invalidateQueries({ queryKey: QK.marketGameReports })
  expect(client.getQueryState(QK.marketGameReport('one'))?.isInvalidated).toBe(false)
  client.clear()
})
