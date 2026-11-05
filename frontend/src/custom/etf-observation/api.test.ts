// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'

afterEach(() => vi.unstubAllGlobals())

it('uses only GET and keeps historical dates, funds and windows separate', async () => {
  const fetch = vi.fn().mockImplementation(() => Promise.resolve(new Response('{}')))
  vi.stubGlobal('fetch', fetch)
  await api.etfObservation(1)
  await api.etfObservation(20, '2026-09-30')
  await api.etfObservationDetail('510310.SH', 60, '2026-11-04')
  expect(fetch.mock.calls.map(([url, init]) => [url, init.method ?? 'GET'])).toEqual([
    ['/api/etf-observation?window=1', 'GET'],
    ['/api/etf-observation?window=20&as_of=2026-09-30', 'GET'],
    ['/api/etf-observation/detail?symbol=510310.SH&window=60&as_of=2026-11-04', 'GET'],
  ])
  expect(QK.etfObservation(20)).not.toEqual(QK.etfObservation(60))
  expect(QK.etfObservation(20)).not.toEqual(QK.etfObservation(20, '2026-09-30'))
  expect(QK.etfObservationDetail('510310.SH', 60)).not.toEqual(QK.etfObservationDetail('510300.SH', 60))
})
